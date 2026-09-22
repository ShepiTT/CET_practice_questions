"""Create timestamped local listening transcripts; no database writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from resource_limits import limit_resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('papers', nargs='*')
    parser.add_argument('--cpus', default='16,17,18,19')
    parser.add_argument('--device', default='cuda', choices=['cuda', 'cpu'])
    args = parser.parse_args()
    ids = [int(x) for x in args.cpus.split(',')]
    limit_resources(ids)
    from gpu_runtime import load_cuda_libraries
    load_cuda_libraries()
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    root = Path(__file__).resolve().parent
    model = WhisperModel(str(root / '.cache/models/base.en'), device=args.device,
                         compute_type='float16' if args.device == 'cuda' else 'int8',
                         cpu_threads=len(ids), num_workers=1, local_files_only=True)
    pipeline = BatchedInferencePipeline(model=model)
    conn = sqlite3.connect(f'file:{root / "instance/cet4_v2.db"}?mode=ro', uri=True)
    papers = args.papers or [row[0] for row in conn.execute(
        'SELECT DISTINCT source_file FROM question_group WHERE group_type="listening" ORDER BY source_file')]
    for name in papers:
        audio = root / 'data/audio' / (Path(name).stem + '.mp3')
        if not audio.exists():
            print(f'MISSING {audio.name}', flush=True)
            continue
        target = root / '.cache/asr' / (audio.stem + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(audio.read_bytes()).hexdigest()
        if target.exists() and json.loads(target.read_text(encoding='utf-8')).get('sha256') == fingerprint:
            continue
        started = time.monotonic()
        segments, info = pipeline.transcribe(str(audio), language='en', beam_size=3,
                                             word_timestamps=True, batch_size=4, vad_filter=True)
        record = {'source': name, 'sha256': fingerprint, 'model': 'Systran/faster-whisper-base.en',
                  'duration': info.duration, 'segments': []}
        print(f'FILE {name}: {info.duration:.1f}s of audio', flush=True)
        for segment in segments:
            record['segments'].append({'start': segment.start, 'end': segment.end, 'text': segment.text,
                                       'words': [{'word': w.word, 'start': w.start, 'end': w.end, 'probability': w.probability}
                                                 for w in (segment.words or [])]})
            if len(record['segments']) % 30 == 0:
                print(f'PROGRESS {name}: {segment.end:.0f}/{info.duration:.0f}s', flush=True)
        target.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        print(f'DONE {name}: {len(record["segments"])} segments, {time.monotonic()-started:.1f}s processing', flush=True)


if __name__ == '__main__':
    main()
