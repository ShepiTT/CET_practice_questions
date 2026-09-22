"""Add five word-bank entries verified visually against the original exam PDFs.

Dry run: python repair_missing_banks.py
Apply (creates an SQLite backup first): python repair_missing_banks.py --apply
Existing words and standard answers are never changed.
"""
import argparse
from datetime import datetime
from pathlib import Path
import sqlite3


# PDF page numbers are one-based. These are transcription corrections, not guesses.
VERIFIED = [
    ('2021-06-CET4-3.pdf', 1, 'D', 'desperate'),
    ('2021-12-CET4-2.pdf', 4, 'O', 'systematically'),
    ('2021-12-CET4-3.pdf', 2, 'O', 'typically'),
    ('2022-12-CET4-1.pdf', 4, 'I', 'narrow'),
    ('2023-03-CET4-1.pdf', 4, 'O', 'alike'),
]


def prepare(conn):
    inserts = []
    for source, page, label, word in VERIFIED:
        questions = conn.execute(
            'SELECT q.id,q.q_number FROM question q JOIN question_group g ON g.id=q.group_id '
            'WHERE q.source_file=? AND g.group_type=? ORDER BY q.q_number', (source, 'cloze')).fetchall()
        if [number for _, number in questions] != list(range(26, 36)):
            raise ValueError(f'{source}: expected exactly questions 26-35')
        for q_id, number in questions:
            options = dict(conn.execute('SELECT label,text FROM option WHERE question_id=?', (q_id,)))
            if label in options:
                if options[label] != word:
                    raise ValueError(f'{source} Q{number}: existing word conflicts; refusing overwrite')
                continue
            if set(options) != set('ABCDEFGHIJKLMNO') - {label}:
                raise ValueError(f'{source} Q{number}: unexpected bank; refusing partial repair')
            inserts.append((q_id, label, word))
        print(f'{source} p.{page}: {label}) {word}')
    return inserts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path(__file__).resolve().parent / 'instance' / 'cet4_v2.db'
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as source:
        inserts = prepare(source)
        print(f'{len(inserts)} missing option rows')
        if not args.apply or not inserts:
            return
        backup = path.with_name(path.name + '.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S-banks'))
        with sqlite3.connect(backup) as target:
            source.backup(target)
        print(f'Backup: {backup}')
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        inserts = prepare(conn)
        conn.executemany('INSERT INTO option(question_id,label,text) VALUES(?,?,?)', inserts)
        assert not prepare(conn), 'Repair did not complete'
        conn.commit()
    print(f'Applied {len(inserts)} rows; standard answers unchanged')


if __name__ == '__main__':
    main()
