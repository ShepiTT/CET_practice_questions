"""OCR for scanned PDFs (RapidOCR on CPU, ~6 s per page).

Results are cached under data/ocr/ mirroring the PDF's path, so every page is recognised once:
    data/2020-12-CET4-1.pdf          -> data/ocr/2020-12-CET4-1.txt  (+ .json with the raw boxes)
    data/answers/2024-06-CET4-1.pdf  -> data/ocr/answers/2024-06-CET4-1.txt

The .json keeps every detected line with its box, so the reading order can be rebuilt
later (relayout) without running OCR again. Answer keys are typeset in two columns, so
their pages are read left column first; exam papers are single-column and read row by row.
"""
import json
import os
import re
import sys

import numpy as np
import pdfplumber

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CACHE_DIR = os.path.join(DATA_DIR, 'ocr')

_engine = None


def _ocr():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def cache_path(pdf_path, ext='.txt'):
    rel = os.path.relpath(os.path.abspath(pdf_path), DATA_DIR)
    if rel.startswith('..'):
        rel = os.path.basename(pdf_path)
    return os.path.join(CACHE_DIR, os.path.splitext(rel)[0] + ext)


CJK = re.compile(r'[一-鿿]')


def is_answer_key(pdf_path):
    """Answer keys live in data/answers/. They are Chinese documents, and typeset in two
    columns; the exam papers are neither."""
    return os.path.basename(os.path.dirname(os.path.abspath(pdf_path))) == 'answers'


def is_two_column(pdf_path):
    return is_answer_key(pdf_path)


def _rows(items, row_tolerance=12):
    """[(y, x, text)] -> lines, grouping boxes whose centres sit on the same row."""
    rows, current, current_y = [], [], None
    for y, x, text in sorted(items):
        if current and abs(y - current_y) > row_tolerance:
            rows.append(current)
            current = []
        if not current:
            current_y = y
        current.append((x, text))
    if current:
        rows.append(current)
    return [' '.join(t for _x, t in sorted(row)) for row in rows]


def lines_in_reading_order(result, width=None, two_column=False):
    """RapidOCR returns (box, text, score) per detected line; rebuild the page top-to-bottom,
    left-to-right so two-column option rows ('A) ...  C) ...') stay on one line.

    An answer key is NOT uniformly two-column: headings, 参考范文 and 听力原文 run the full
    width while the 答案详解 blocks are two-column, and a whole-page verdict gets it wrong
    either way (a row-wise read glues the right column onto the left column's lines, which
    silently corrupts the answer sentences).

    So split the page into horizontal BANDS instead: a line that crosses the middle is
    full-width and acts as its own band and as a separator; the runs between them are read
    left column first, then right. A band with content on only one side reads normally."""
    items = []
    for box, text, _score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append((sum(ys) / len(ys), min(xs), max(xs), text))
    if not (two_column and width):
        return _rows([(y, x, t) for y, x, _x2, t in items])

    mid = width / 2
    tol = width * 0.02
    rows, band = [], []

    def flush():
        if not band:
            return
        left = [it for it in band if it[2] <= mid + tol]
        right = [it for it in band if it[2] > mid + tol]
        if left and right:
            rows.extend(_rows([(y, x, t) for y, x, _x2, t in left]))
            rows.extend(_rows([(y, x, t) for y, x, _x2, t in right]))
        else:
            rows.extend(_rows([(y, x, t) for y, x, _x2, t in band]))
        band.clear()

    for item in sorted(items):
        _y, x0, x1, _t = item
        if x0 < mid - tol and x1 > mid + tol:       # crosses the middle: full width
            flush()
            rows.extend(_rows([(item[0], item[1], item[3])]))
        else:
            band.append(item)
    flush()
    return rows


def ocr_page(page, resolution=200):
    """Raw RapidOCR result for one page plus the rendered width: ([box, text, score], width)."""
    image = np.array(page.to_image(resolution=resolution).original.convert('RGB'))
    result, _elapsed = _ocr()(image)
    raw = [[[[float(x), float(y)] for x, y in box], text, float(score)] for box, text, score in (result or [])]
    return raw, image.shape[1]


def _render(pages, two_column):
    return '\n'.join('\n'.join(lines_in_reading_order(raw, width, two_column)) for raw, width in pages) + '\n'


def ocr_pdf(pdf_path, force=False, log=None):
    """Full text of a PDF via OCR, cached. `log` (optional) receives progress lines."""
    cached = cache_path(pdf_path)
    if not force and os.path.exists(cached):
        with open(cached, encoding='utf-8') as f:
            return f.read()
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            if log:
                log(f'  OCR {os.path.basename(pdf_path)} page {i}/{len(pdf.pages)}')
            pages.append(ocr_page(page))
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cache_path(pdf_path, '.json'), 'w', encoding='utf-8') as f:
        json.dump([{'width': width, 'lines': raw} for raw, width in pages], f, ensure_ascii=False)
    text = _render(pages, is_two_column(pdf_path))
    with open(cached, 'w', encoding='utf-8') as f:
        f.write(text)
    return text


def relayout(pdf_path):
    """Rebuild the cached .txt from the cached raw boxes with the current reading-order rules.
    Returns the text, or None when there is no .json for this PDF."""
    raw_path = cache_path(pdf_path, '.json')
    if not os.path.exists(raw_path):
        return None
    with open(raw_path, encoding='utf-8') as f:
        pages = [(page['lines'], page['width']) for page in json.load(f)]
    text = _render(pages, is_two_column(pdf_path))
    with open(cache_path(pdf_path), 'w', encoding='utf-8') as f:
        f.write(text)
    return text


def cached_text(pdf_path):
    cached = cache_path(pdf_path)
    if not os.path.exists(cached):
        return None
    with open(cached, encoding='utf-8') as f:
        return f.read()


def has_text_layer(pdf_path, min_chars=500, min_cjk_ratio=0.02):
    """Whether this PDF carries text we can actually READ, not merely a lot of characters.

    Four answer keys embed a 50k-character text layer in a custom font encoding. It extracts
    as mojibake — roughly 0.1% Chinese in a Chinese document, and not one of the markers the
    parser keys on. Counting characters called that "has text" and skipped OCR, so those
    papers silently ended up with no answers at all. For an answer key, demand that the
    layer actually contains Chinese; exam papers are English, so a character count is
    the right test for them."""
    with pdfplumber.open(pdf_path) as pdf:
        text = ''.join(page.extract_text() or '' for page in pdf.pages)
    if len(text) < min_chars:
        return False
    if not is_answer_key(pdf_path):
        return True
    return len(CJK.findall(text)) >= min_cjk_ratio * len(text)


def scanned_pdfs_without_cache():
    for folder in (DATA_DIR, os.path.join(DATA_DIR, 'answers')):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.endswith('.pdf') and cached_text(path) is None and not has_text_layer(path):
                yield path


def go_easy(cores=4):
    """Keep OCR from taking over the machine: below-normal priority, pinned to `cores` CPUs.
    (onnxruntime otherwise spins up one thread per core.) Windows only; a no-op elsewhere."""
    if sys.platform != 'win32':
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetCurrentProcess()
    kernel32.SetPriorityClass(handle, 0x00004000)            # BELOW_NORMAL_PRIORITY_CLASS
    kernel32.SetProcessAffinityMask(handle, (1 << cores) - 1)


if __name__ == '__main__':
    # `python ocr.py` OCRs every scanned PDF under data/ that has no cache yet;
    # `python ocr.py a.pdf b.pdf` does exactly those files;
    # `python ocr.py --relayout a.pdf ...` only rebuilds their text from the cached boxes.
    # `--cores N` (default 4) limits how much of the machine OCR may use.
    args = sys.argv[1:]
    cores = 4
    if '--cores' in args:
        i = args.index('--cores')
        cores = int(args[i + 1])
        del args[i:i + 2]
    go_easy(cores)
    if args and args[0] == '--relayout':
        for path in args[1:]:
            print(f'{path}: {"relaid out" if relayout(path) is not None else "no cached boxes"}')
        sys.exit()
    paths = args or list(scanned_pdfs_without_cache())
    for path in paths:
        ocr_pdf(path, log=print)
        print(f'cached -> {cache_path(path)}')
