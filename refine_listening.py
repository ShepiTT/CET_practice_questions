"""Re-transcribe focused clips when batched ASR omitted an opening sentence."""
import argparse
import json
from pathlib import Path
from statistics import median
from difflib import SequenceMatcher

from resource_limits import limit_resources
from gpu_runtime import load_cuda_libraries
from align_listening import ROOT, tokens, align
from fetch_reference_keys import read_reference


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audit',type=Path)
    args=parser.parse_args()
    limit_resources([0,2,4,6])
    load_cuda_libraries()
    from faster_whisper import WhisperModel
    model=WhisperModel(str(ROOT/'.cache/models/base.en'),device='cuda',compute_type='float16',cpu_threads=4,num_workers=1,local_files_only=True)
    report=json.loads(args.audit.read_text(encoding='utf-8'))
    for item in report['unresolved']:
        if not item.get('transcript'):
            continue
        source=item['source']
        target=ROOT/'.cache/asr-refined'/f'{item["id"]}.json'
        if target.exists():
            continue
        asr=json.loads((ROOT/'.cache/asr'/(Path(source).stem+'.json')).read_text(encoding='utf-8'))
        evidence=item['reference'] or {}
        expected=evidence.get('reference_start')
        if expected is None:
            try:
                root,_,_=read_reference(source)
                seeks=root.xpath(f'//*[@id="lt-{item["span"][0]}"]//*[@data-listen-seek]')
                expected=float(seeks[0].get('data-listen-seek')) if seeks else None
            except Exception:
                pass
        offsets=[i['start']-i['reference']['reference_start'] for i in report['proposed'] if i['source']==source and i['reference'] and i['reference'].get('reference_start') is not None]
        if expected is not None:
            expected += median(offsets) if offsets else 0
        else:
            # Find a later sentence to bound the retry, never to assign a timestamp.
            script=tokens(item['transcript'])
            words=[w for segment in asr['segments'] for w in segment['words']]
            flat=[(t,w['start']) for w in words for t in tokens(w['word'])]
            candidates=[]
            for k in (10,20,30,40):
                needle=script[k:k+20]
                if len(needle)<20:continue
                for i,(word,time) in enumerate(flat):
                    if word!=needle[0]:continue
                    score=SequenceMatcher(None,needle,[v[0] for v in flat[i:i+20]],autojunk=False).ratio()
                    if score>=.8:candidates.append((score,time-k*.45))
            expected=max(candidates)[1] if candidates else None
        if expected is None:
            print(f'NO WINDOW {source} {item["span"]}',flush=True)
            continue
        start=max(0,expected-15)
        end=min(asr['duration'],expected+45)
        audio=ROOT/'data/audio'/(Path(source).stem+'.mp3')
        segments,_=model.transcribe(str(audio),language='en',beam_size=1,word_timestamps=True,
                                   vad_filter=False,condition_on_previous_text=False,clip_timestamps=f'{start},{end}')
        words=[{'word':w.word,'start':w.start,'end':w.end} for segment in segments for w in (segment.words or [])]
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps({'sha256':asr['sha256'],'window':[start,end],'words':words},ensure_ascii=False),encoding='utf-8')
        match=align(item['transcript'],words)
        print(f'REFINED {source} {item["span"]}: '+(f'{match["start"]:.2f}s score={match["score"]:.3f}' if match else 'unresolved'),flush=True)


if __name__=='__main__':main()
