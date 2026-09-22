"""Resume OCR of missing answer-key caches, without modifying the question database."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from resource_limits import limit_resources


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('papers', nargs='*')
    args.add_argument('--cpus', default='0,2,4,6')
    args = args.parse_args()
    cpus = [int(x) for x in args.cpus.split(',')]
    limit_resources(cpus)
    import onnxruntime as ort
    import rapidocr_onnxruntime.utils as ort_utils
    import cv2
    import pdfplumber
    import ocr
    cv2.setNumThreads(1)

    def options():
        result = ort.SessionOptions()
        result.intra_op_num_threads = len(cpus)
        result.inter_op_num_threads = 1
        return result
    ort_utils.SessionOptions = options
    root = Path(__file__).resolve().parent
    conn = sqlite3.connect(f'file:{root / "instance/cet4_v2.db"}?mode=ro', uri=True)
    papers = args.papers or [row[0] for row in conn.execute(
        'SELECT DISTINCT source_file FROM question WHERE correct_answer IS NULL OR trim(correct_answer)="" ORDER BY source_file')]
    for name in papers:
        source = root / 'data/answers' / name
        if not source.exists() or Path(ocr.cache_path(str(source))).exists():
            continue
        fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
        partial = root / '.cache/answer-pages' / (source.stem + '.json')
        partial.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(partial.read_text(encoding='utf-8')) if partial.exists() else {}
        pages = state.get('pages', []) if state.get('sha256') == fingerprint else []
        with pdfplumber.open(source) as pdf:
            print(f'FILE {name}: {len(pdf.pages)} pages; resuming at {len(pages)+1}', flush=True)
            for i in range(len(pages), len(pdf.pages)):
                started = time.monotonic()
                raw, width = ocr.ocr_page(pdf.pages[i])
                pages.append({'width': width, 'lines': raw})
                temporary = partial.with_suffix('.tmp')
                temporary.write_text(json.dumps({'sha256': fingerprint, 'pages': pages}, ensure_ascii=False), encoding='utf-8')
                temporary.replace(partial)
                print(f'PAGE {name} {i+1}/{len(pdf.pages)} {time.monotonic()-started:.1f}s', flush=True)
        Path(ocr.cache_path(str(source), '.json')).write_text(json.dumps(pages, ensure_ascii=False), encoding='utf-8')
        text = ocr._render([(p['lines'], p['width']) for p in pages], ocr.is_two_column(str(source)))
        Path(ocr.cache_path(str(source))).write_text(text, encoding='utf-8')
        print(f'DONE {name}', flush=True)


if __name__ == '__main__':
    main()
