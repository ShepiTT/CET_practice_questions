"""Propose/fill missing keys only, using explicit verdicts from cached answer PDFs.

Every inserted key retains its source and evidence in a JSON audit. Existing keys
are immutable here. Run without --apply to review the proposal first.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3

import answers

ROOT = Path(__file__).resolve().parent


def normalize_evidence(text):
    # OCR sometimes reads the 1 in numbered question headings 41/51 as l or I.
    text = re.sub(r'(?m)^(\s*[1-5])[lI](\s*[.．、]\s*(?=[【（(A-Z]))', r'\g<1>1\2', text)
    text = re.sub(r'(?m)^\s*l\.\s*(?=(?:What|Why|How|Where|Who|When|Which)\b)', '1. ', text)
    # Remove a recognisable answer-book footer before joining wrapped Chinese.
    text = re.sub(r'(?m)^\s*\d{4}[.．]\d{1,2}\s*[/／].*第\s*\d\s*套.*$', '', text)
    return re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', text)


VERDICTS = [
    r'(?:正确答案|答案)\s*(?:应为|应该是|为|是|选)\s*([A-P])',
    r'(?m)^\s*[【\[（(]?答案(?:解析)?[】\]）)]?\s*([A-P])',
    r'(?:故|因此|所以)\s*([A-P])\s*(?:为|是)\s*(?:答案选项|正确答案)',
    r'选项\s*([A-P])\s*(?:为|是|即为)\s*正确答案',
    r'(?:故|因此|所以|本题)\s*(?:本题)?\s*(?:应)?选(?:项|择)?\s*([A-P])(?=\s*(?:[)）]?[。，；;]|项?[为是]正确|$))',
    r'(?m)^\s*([A-P])\s*[）)】]\s*[【\[]\s*精析',
]


def explicit_answer(block, options, cloze=False):
    block = normalize_evidence(block)
    if cloze:
        # Use the bank word, never an OCR-prone standalone I/D/O label.
        words = {label for label, word in options.items() if word and re.search(
            rf'(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])', block, re.I)}
        return (next(iter(words)), 'unique_bank_word') if len(words) == 1 else (None, 'ambiguous_bank_words')
    labels = {m.group(1) for pattern in VERDICTS for m in re.finditer(pattern, block)}
    if len(labels) != 1:
        return None, 'missing_or_conflicting_verdict'
    label = next(iter(labels))
    return (label, 'explicit_verdict') if label in options else (None, 'not_offered')


def proposal(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT q.*,g.group_type,g.passage FROM question q JOIN question_group g ON g.id=q.group_id '
                        'WHERE q.correct_answer IS NULL OR trim(q.correct_answer)="" ORDER BY q.source_file,q.q_number').fetchall()
    cache, proposed, unresolved = {}, [], []
    for q in rows:
        source = q['source_file']
        if source not in cache:
            path = ROOT / 'data/ocr/answers' / (Path(source).stem + '.txt')
            text = normalize_evidence(path.read_text(encoding='utf-8')) if path.exists() else ''
            cache[source] = answers.parse_answer_key(text) if text else {}
            # Older keys omit the 答案详解 heading for their listening blocks.
            # Explicit question ranges still bind each numbered answer to its group.
            for start, end, body in answers.listening_blocks(text):
                entries = answers._question_blocks(body.splitlines())
                for number, evidence in entries.items():
                    if start <= number <= end:
                        cache[source][number] = {'explanation': evidence}
        entry = cache[source].get(q['q_number'])
        if not entry:
            unresolved.append({'source': source, 'number': q['q_number'], 'reason': 'no_numbered_block'})
            continue
        options = dict(conn.execute('SELECT label,text FROM option WHERE question_id=?', (q['id'],)))
        if q['group_type'] == 'matching':
            labels = set(re.findall(r'(?:^|\s)([A-Z])\s*[)）]', q['passage'] or ''))
            options = {chr(i): '' for i in range(ord('A'), ord(max(labels) if labels else 'O')+1)}
        answer, reason = explicit_answer(entry['explanation'], options, q['group_type'] == 'cloze')
        if answer:
            proposed.append({'id': q['id'], 'source': source, 'number': q['q_number'], 'answer': answer,
                             'method': reason, 'evidence': entry['explanation']})
        else:
            unresolved.append({'source': source, 'number': q['q_number'], 'reason': reason})
    return {'proposed': proposed, 'unresolved': unresolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = ROOT / 'instance/cet4_v2.db'
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as conn:
        report = proposal(conn)
        if args.apply and report['proposed']:
            backup = path.with_name(path.name + f'.bak-{stamp}-answers')
            with sqlite3.connect(backup) as dest:
                conn.backup(dest)
            report['backup'] = str(backup)
    if args.apply:
        with sqlite3.connect(path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for item in report['proposed']:
                cursor = conn.execute('UPDATE question SET correct_answer=?, explanation=CASE WHEN explanation IS NULL OR trim(explanation)="" THEN ? ELSE explanation END '
                                      'WHERE id=? AND source_file=? AND q_number=? AND (correct_answer IS NULL OR trim(correct_answer)="")',
                                      (item['answer'], item['evidence'], item['id'], item['source'], item['number']))
                if cursor.rowcount != 1:
                    raise RuntimeError('Question changed since review; transaction cancelled')
            conn.commit()
    report['applied'] = args.apply
    audit = ROOT / 'instance' / f'answer-completion-{stamp}.json'
    audit.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(report["proposed"])} proposed; {len(report["unresolved"])} unresolved; applied={args.apply}; audit={audit}')


if __name__ == '__main__':
    main()
