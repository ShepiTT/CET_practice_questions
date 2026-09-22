"""Repair truncated OCR text using independently matched reference paper content.

Only groups/questions with missing keys are considered. Review JSON is produced
without --apply. Existing answer keys and user history are never modified.
"""
import argparse
from datetime import datetime
import json
import re
import sqlite3

from fetch_reference_keys import ROOT, norm, similarity, passage_prefix_matches, read_reference


def proposals(conn):
    conn.row_factory = sqlite3.Row
    changes = []
    groups = conn.execute('SELECT DISTINCT g.* FROM question_group g JOIN question q ON q.group_id=g.id WHERE q.correct_answer IS NULL OR trim(q.correct_answer)=""').fetchall()
    for group in groups:
        source = group['source_file']
        root, keys, evidence = read_reference(source)
        questions = conn.execute('SELECT * FROM question WHERE group_id=?', (group['id'],)).fetchall()
        def change(table, row, field, new):
            old = row[field]
            if old != new:
                changes.append({'table':table,'id':row['id'],'field':field,'old':old,'new':new,'source':source,'reference':evidence})
        if group['group_type'] == 'matching':
            pars = root.xpath('//*[starts-with(@id,"p-part3-section-b-")]//*[@data-pdh-letter]')
            stems = {q['id']:root.xpath(f'//*[@id="qp-{q["q_number"]}"]//*[@data-pdh-stem]') for q in questions}
            matching_stems = sum(bool(stems[q['id']]) and similarity(q['content'],stems[q['id']][0].text_content()) >= .94 for q in questions)
            anchors = sum(passage_prefix_matches(p.text_content(),group['passage']) for p in pars)
            if len(pars) < 10 or matching_stems < 5 or anchors < 3:
                continue
            passage = '\n\n'.join(f'{p.get("data-pdh-letter")}) {p.text_content().strip()}' for p in pars)
            change('question_group', group, 'passage', passage)
            for q in questions:
                if not q['correct_answer'] and stems[q['id']]:
                    new = stems[q['id']][0].text_content().strip()
                    a,b = norm(q['content']),norm(new)
                    if min(len(a),len(b)) >= 40 and (a.startswith(b) or b.startswith(a) or similarity(a,b)>=.94):
                        change('question',q,'content',new)
        else:
            for q in questions:
                if q['correct_answer']:
                    continue
                nodes = root.xpath(f'//*[@id="qp-{q["q_number"]}"]')
                if not nodes:
                    continue
                refs = {e.get('data-pdh-letter'):e.text_content().strip() for e in nodes[0].xpath('.//*[@data-pdh-letter]')}
                options = conn.execute('SELECT * FROM option WHERE question_id=?',(q['id'],)).fetchall()
                agree = sum(o['label'] in refs and similarity(o['text'],refs[o['label']])>=.94 for o in options)
                stem = nodes[0].xpath('.//*[@data-pdh-stem]')
                stem_agree = bool(stem) and similarity(q['content'],stem[0].text_content())>=.98
                if len(refs)!=4 or len(options)!=4 or not (agree>=3 or (agree>=2 and stem_agree)):
                    continue
                for option in options:
                    new = refs[option['label']]
                    old = option['text']
                    # Fix recognizable appended OCR spill, not arbitrary different wording.
                    if norm(old).startswith(norm(new)) and len(norm(old)) > max(len(norm(new))+8, len(norm(new))*1.25) and new.endswith(('.', '?', '!')):
                        change('option',option,'text',new)
                    elif q['id']==1482 and option['label']=='D' and agree==2 and stem_agree:
                        # Reviewed: q53's D was replaced by q54/55 text in the OCR cache.
                        change('option',option,'text',new)
    return changes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    db=ROOT/'instance/cet4_v2.db'
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    with sqlite3.connect(f'{db.as_uri()}?mode=ro',uri=True) as conn:
        changes=proposals(conn)
        report={'changes':changes,'applied':args.apply}
        if args.apply:
            backup=db.with_name(db.name+f'.bak-{stamp}-ocr-text')
            with sqlite3.connect(backup) as dest:
                conn.backup(dest)
            report['backup']=str(backup)
    if args.apply:
        with sqlite3.connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for item in changes:
                cursor=conn.execute(f'UPDATE {item["table"]} SET {item["field"]}=? WHERE id=? AND {item["field"]} IS ?', (item['new'],item['id'],item['old']))
                if cursor.rowcount!=1:
                    raise RuntimeError('Data changed during review; rollback')
    path=ROOT/'instance'/f'ocr-text-repairs-{stamp}.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'AUDIT {path}; {len(changes)} changes; applied={args.apply}')


if __name__=='__main__':
    main()
