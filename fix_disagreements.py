"""Correct database answers that the answer key contradicts — but only on strong evidence.

The database's original answers were loaded with a very loose importer (one of its patterns
was "a number followed by a capital letter"), so whole papers drifted out of alignment. The
keys disagree with 74 of them. Neither side is automatically right: OCR reads the key's "I)"
as "D)" often enough that the key is sometimes the wrong one.

So a disagreement is only acted on when a second, independent signal agrees with the key:

  banked cloze  the explanation names one of the 15 bank WORDS. OCR confuses I) and D);
                it does not confuse two different English words.
  everything    the explanation states the answer in prose ("故答案为B）", "【答案】D"), and
  else          that statement names exactly the letter extracted.

Anything weaker is left alone and listed, so a human can settle it.

    python fix_disagreements.py            # report only, change nothing
    python fix_disagreements.py --apply    # back up, overwrite the strong ones, write an audit
"""
import csv
import os
import re
import shutil
import sys
from datetime import datetime

from app import app, db
from models import Question
import answers
import import_answers
import ocr

# "故答案为B）" / "因此答案是C" / "【答案】D" / "【答案解析】A"
EXPLICIT = re.compile(
    r'(?:故|因此|所以|由此确定|可知)?\s*答案\s*(?:为|是|应为)\s*([A-O])'
    r'|[【〖\[(（]\s*答\s*案\s*(?:解\s*析)?\s*[】〗\])）]\s*([A-O])')


def evidence_for(question, entry, letter):
    """Why we would believe the key over the database, or '' when we would not."""
    by_word = import_answers.letter_from_bank_word(question, entry['explanation'])
    if by_word:
        return 'bank word' if by_word == letter else ''
    stated = {a or b for a, b in EXPLICIT.findall(entry['explanation'])}
    if stated == {letter}:
        return 'stated in prose'
    return ''


def survey():
    rows = []
    for source_file in sorted({r[0] for r in db.session.query(Question.source_file).distinct()}):
        text = ocr.cached_text(os.path.join(ocr.DATA_DIR, 'answers', source_file))
        if not text:
            continue
        key = answers.parse_answer_key(text)
        for q in Question.query.filter_by(source_file=source_file).all():
            entry = key.get(q.q_number)
            if not entry or not q.correct_answer:
                continue
            letter = import_answers.letter_from_bank_word(q, entry['explanation']) or entry['answer']
            if not letter or letter not in import_answers.offered_by(q):
                continue
            if q.correct_answer.strip().upper() == letter:
                continue
            rows.append({
                'file': source_file, 'q': q.q_number, 'id': q.id,
                'database': q.correct_answer.strip().upper(), 'key': letter,
                'evidence': evidence_for(q, entry, letter),
            })
    return rows


def main(argv):
    apply = '--apply' in argv
    with app.app_context():
        rows = survey()
        strong = [r for r in rows if r['evidence']]
        weak = [r for r in rows if not r['evidence']]
        print(f"disagreements: {len(rows)}")
        print(f"  strong evidence for the key (would change): {len(strong)}")
        print(f"  inconclusive (left alone)                 : {len(weak)}")
        by_file = {}
        for r in strong:
            by_file.setdefault(r['file'], []).append(r)
        for f, rs in sorted(by_file.items()):
            print(f"    {f:24} {len(rs):>3}  " + ', '.join(f"Q{r['q']} {r['database']}->{r['key']}" for r in rs[:6])
                  + (' …' if len(rs) > 6 else ''))
        if not apply:
            print("\nnothing written (pass --apply to change the database)")
            return

        path = db.engine.url.database
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(path, f"{path}.bak-{stamp}")
        audit = os.path.join(os.path.dirname(path), f'answer-corrections-{stamp}.csv')
        with open(audit, 'w', newline='', encoding='utf-8-sig') as fh:
            writer = csv.DictWriter(fh, fieldnames=['file', 'q', 'id', 'database', 'key', 'evidence'])
            writer.writeheader()
            writer.writerows(strong)
        for r in strong:
            db.session.get(Question, r['id']).correct_answer = r['key']
        db.session.commit()
        print(f"\nbackup : {os.path.basename(path)}.bak-{stamp}")
        print(f"audit  : {os.path.basename(audit)}  ({len(strong)} rows)")
        print(f"changed: {len(strong)} answers")


if __name__ == '__main__':
    main(sys.argv[1:])
