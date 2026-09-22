"""Fill answers, explanations and listening transcripts from the OCR'd 答案解析 keys.

    python import_answers.py                     # every paper with a key under data/ocr/answers/
    python import_answers.py 2024-06-CET4-1.pdf  # just these papers
    python import_answers.py --check             # compare with the database, change nothing

Answers already in the database are never overwritten; disagreements are listed instead.
Run `python ocr.py` first to OCR the answer PDFs (data/answers/*.pdf) into the cache.
"""
import os
import re
import sys

from app import app, db, ensure_column
from models import Question, QuestionGroup
import answers
import ocr
import parser


def key_text(source_file):
    """This paper's answer key as text: the OCR cache, or the PDF's own text layer when it
    has a readable one (a few keys are born digital and never need OCR at all)."""
    path = os.path.join(ocr.DATA_DIR, 'answers', source_file)
    cached = ocr.cached_text(path)
    if cached:
        return cached
    if os.path.exists(path) and ocr.has_text_layer(path):
        return parser.load_text(path)
    return None


PARAGRAPH_LETTERS = set('ABCDEFGHIJKLMNO')
LISTENING_TITLE = re.compile(r'Listening: Questions (\d+) to (\d+)')


def letter_from_bank_word(question, explanation):
    """For a banked-cloze question, identify the answer by the BANK WORD the explanation
    names rather than by the printed letter.

    The keys set "I)" and "D)" almost identically and OCR swaps them constantly, which put
    a wrong letter on a question whose real answer was right there in the text. OCR does not
    confuse two different English words. Returns None unless exactly one bank word appears,
    so an ambiguous block falls back to the letter."""
    bank = {o.label: o.text for o in question.options}
    if len(bank) < 10:
        return None                      # not a word bank
    found = {label for label, word in bank.items()
             if word and re.search(rf'(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])',
                                   explanation, re.IGNORECASE)}
    return found.pop() if len(found) == 1 else None


def offered_by(question):
    """The letters this question actually offers. Paragraph-matching questions carry no
    options of their own — their answer is a paragraph letter, so allow A-O there."""
    labels = {o.label for o in question.options}
    return labels or PARAGRAPH_LETTERS


def apply_key(source_file, text, check_only=False):
    key = answers.parse_answer_key(text)
    stats = {'filled': 0, 'agree': 0, 'disagree': [], 'unknown': [], 'explained': 0,
             'transcripts': 0, 'rejected': [], 'by_word': []}
    for q in Question.query.filter_by(source_file=source_file).all():
        entry = key.get(q.q_number)
        if entry and entry['explanation'] and not q.explanation and not check_only:
            q.explanation = entry['explanation']
            stats['explained'] += 1
        answer = entry['answer'] if entry else None
        if entry:
            by_word = letter_from_bank_word(q, entry['explanation'])
            if by_word:
                if answer and by_word != answer:
                    stats['by_word'].append((q.q_number, answer, by_word))
                answer = by_word
        # A letter the question does not offer cannot be its answer: it is an OCR misread
        # (the keys print "I)" and "D)" almost identically). Storing it would mark the
        # genuinely correct choice wrong, so drop it and leave the answer unset.
        if answer and answer not in offered_by(q):
            stats['rejected'].append((q.q_number, answer, ''.join(sorted(offered_by(q)))))
            answer = None
        if not answer:
            stats['unknown'].append(q.q_number)
        elif q.correct_answer:
            if q.correct_answer.strip().upper() == answer:
                stats['agree'] += 1
            else:
                stats['disagree'].append((q.q_number, q.correct_answer, answer))
        elif not check_only:
            q.correct_answer = answer
            stats['filled'] += 1
    if not check_only:
        # Match the transcripts to the listening groups this paper actually has, in paper
        # order: CET-4 and CET-6 group their listening differently.
        spans = sorted(
            (int(m.group(1)), int(m.group(2)))
            for m in (LISTENING_TITLE.match(g.title or '') for g in QuestionGroup.query.filter_by(
                source_file=source_file, group_type='listening').all())
            if m)
        for (start, end), transcript in answers.parse_transcripts(text, spans).items():
            group = QuestionGroup.query.filter_by(
                source_file=source_file, title=f'Listening: Questions {start} to {end}').first()
            if group and not group.transcript:
                group.transcript = transcript
                stats['transcripts'] += 1
    return stats


def main(argv):
    check_only = '--check' in argv
    papers = [a for a in argv if not a.startswith('--')]
    if not papers:
        # Every answer PDF, not just the OCR'd ones: a key with a readable text layer
        # needs no cache entry at all.
        folder = os.path.join(ocr.DATA_DIR, 'answers')
        papers = sorted(f for f in os.listdir(folder) if f.endswith('.pdf')) \
            if os.path.isdir(folder) else []
    totals = {'filled': 0, 'agree': 0, 'disagree': 0, 'unknown': 0, 'explained': 0, 'transcripts': 0}
    with app.app_context():
        ensure_column('question_group', 'transcript', 'TEXT')
        for source_file in papers:
            if not Question.query.filter_by(source_file=source_file).first():
                print(f'{source_file}: not in the database, skipped')
                continue
            text = key_text(source_file)
            if not text:
                print(f'{source_file}: no OCR text for its answer key, skipped')
                continue
            s = apply_key(source_file, text, check_only)
            print(f"{source_file}: +{s['filled']} answers, {s['agree']} agree, "
                  f"{len(s['disagree'])} disagree, {len(s['rejected'])} rejected, "
                  f"{len(s['unknown'])} unknown, "
                  f"+{s['explained']} explanations, +{s['transcripts']} transcripts")
            for n, have, found in s['disagree']:
                print(f'    ! Q{n}: database says {have}, key says {found}')
            for n, letter, offered in s['rejected']:
                print(f'    x Q{n}: key says {letter}, but this question only offers {offered}')
            if s['unknown']:
                print(f"    ? no answer found for Q{', '.join(map(str, s['unknown']))}")
            for k in totals:
                v = s[k]
                totals[k] += len(v) if isinstance(v, list) else v
        if check_only:
            db.session.rollback()
        else:
            db.session.commit()
    print('\ntotal:', ', '.join(f'{k} {v}' for k, v in totals.items()), '(nothing written)' if check_only else '')


if __name__ == '__main__':
    main(sys.argv[1:])
