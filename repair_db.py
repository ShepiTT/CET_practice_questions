"""One-off repair for an existing cet4_v2.db. Safe to run more than once.

1. drop option rows whose question no longer exists
2. collapse duplicate (question_id, label) option rows
3. rebuild every Section A word bank from its PDF (where the parser yields the full bank, or
   more of it than the database has)
4. delete the fake "SHOW_ANSWER_REQUEST" attempts that the old "show answers" button
   recorded, and the wrong-question entries that only exist because of them
5. add the unique index on option(question_id, label) so 2 cannot happen again

Run:  python repair_db.py
"""
import os
import shutil
from datetime import datetime

from sqlalchemy import text

from app import app, db
from models import QuestionGroup, Option, UserHistory, WrongQuestion
from parser import parse_cet_pdf_v2, BANK_LABELS

REVEAL_SENTINEL = 'SHOW_ANSWER_REQUEST'


def delete_orphan_options():
    return db.session.execute(text(
        "DELETE FROM option WHERE question_id NOT IN (SELECT id FROM question)")).rowcount


def dedupe_options():
    """Keep the lowest id for each (question_id, label)."""
    return db.session.execute(text(
        "DELETE FROM option WHERE id NOT IN (SELECT MIN(id) FROM option GROUP BY question_id, label)")).rowcount


def rebuild_cloze_banks(data_dir):
    """Replace the word bank of each cloze group with a fresh parse of its PDF.

    Returns (rebuilt_files, skipped) where skipped is [(file, labels_the_parser_gave)].
    """
    rebuilt, skipped = [], []
    for group in QuestionGroup.query.filter_by(group_type='cloze').all():
        pdf = os.path.join(data_dir, group.source_file)
        bank = None
        if os.path.exists(pdf):
            for g in parse_cet_pdf_v2(pdf):
                if g['type'] == 'cloze' and g['questions']:
                    bank = g['questions'][0]['options']
        labels = ''.join(o['label'] for o in bank) if bank else ''
        stored = {o.label for q in group.questions[:1] for o in q.options}
        # Rebuild when the fresh parse is the full bank, or strictly more of it than we have.
        improves = len(labels) == len(set(labels)) and set(labels) > stored
        if labels != BANK_LABELS and not improves:
            skipped.append((group.source_file, labels))
            continue
        for q in group.questions:
            q.options = []
            db.session.flush()  # delete the old rows before inserting the same labels again
            q.options = [Option(label=o['label'], text=o['text']) for o in bank]
        rebuilt.append(group.source_file)
    db.session.flush()
    return rebuilt, skipped


def purge_reveal_history():
    """Returns (fake_attempts_removed, bogus_wrong_entries_removed)."""
    fake = UserHistory.query.filter_by(user_answer=REVEAL_SENTINEL).delete()
    # A wrong-question entry is only legitimate while a real wrong attempt exists for it.
    really_wrong = db.select(UserHistory.question_id).where(UserHistory.is_correct.is_(False))
    bogus = WrongQuestion.query.filter(~WrongQuestion.question_id.in_(really_wrong)).delete(
        synchronize_session=False)
    return fake, bogus


def ensure_unique_index():
    db.session.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_option_question_label ON option (question_id, label)"))


def main():
    with app.app_context():
        db_path = db.engine.url.database
        if db_path and os.path.exists(db_path):
            backup = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy2(db_path, backup)
            print(f"backup written to {backup}")

        print(f"orphan options removed:      {delete_orphan_options()}")
        print(f"duplicate options removed:   {dedupe_options()}")
        rebuilt, skipped = rebuild_cloze_banks(os.path.join(app.root_path, 'data'))
        print(f"word banks rebuilt from PDF: {len(rebuilt)}")
        for source_file, labels in skipped:
            print(f"  ! {source_file}: parser gave labels {labels!r}, left as-is")
        fake, bogus = purge_reveal_history()
        print(f"fake reveal attempts removed:         {fake}")
        print(f"bogus wrong-question entries removed: {bogus}")
        ensure_unique_index()
        db.session.commit()

        print("\nnow in the database:")
        print(f"  attempts:        {UserHistory.query.count()}")
        print(f"  wrong questions: {WrongQuestion.query.count()}")
        print(f"  options:         {Option.query.count()}")


if __name__ == '__main__':
    main()
