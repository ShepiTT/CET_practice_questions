"""Build a source-backed Lazynote paper candidate; append only on explicit apply.

HTML supplies semantic question boundaries and the options in that same paper.
The answer PDF corroborates the listening questions and every transcript
paragraph. Module HTML supplies complete, isolated analysis blocks rather than
PDF text in its unreliable drawing order. No network requests are made here.

Example (candidate only, no database access)::
    python scripts/import_lazynote_paper.py --output .cache/cet-202606-review/paper-candidate.json

The Python API is ``validate(candidate)`` and
``apply(database, candidate, dry_run=False)``. Applying is additive, backed up,
and idempotent. An occupied source with different/partial data is rejected.
"""

import argparse
import bisect
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata

from lxml import html


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / '.cache/cet-202606-review'
TABLES = ('question_group', 'question', 'option', 'user_history', 'wrong_question')
GROUP_FIELDS = ('title', 'passage', 'group_type', 'audio_start', 'transcript')
QUESTION_FIELDS = ('section', 'content', 'q_number', 'correct_answer', 'explanation')
MODULE_SLUGS = {'listening': 'listening', 'cloze': 'part3-section-a',
                'matching': 'part3-section-b', 'reading1': 'part3-section-c',
                'reading2': 'part3-section-c-2'}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def content_digest(candidate):
    return digest({key: candidate[key] for key in ('schema', 'source_file', 'source_url', 'groups', 'evidence')})


def compact(value):
    return re.sub(r'\s+', ' ', value).strip()


def norm(value):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKC', value).casefold())


def cls(node, name):
    return name in node.get('class', '').split()


def select_class(node, name):
    return node.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), $name)]', name=f' {name} ')


def one(nodes, label):
    if len(nodes) != 1:
        raise ValueError(f'Expected one {label}, got {len(nodes)}')
    return nodes[0]


def by_id(root, identity):
    return one(root.xpath('//*[@id=$identity]', identity=identity), identity)


def plain(node):
    """Readable block text, with UI controls and hidden paragraph numbers removed."""
    blocks = {'div', 'p', 'li', 'ul', 'ol', 'aside', 'br'}

    def walk(element):
        if not isinstance(element.tag, str):
            return ''
        if element.tag in {'button', 'svg', 'script', 'style'} or cls(element, 'px-pill') or cls(element, 'exam-pno'):
            return ''
        text = element.text or ''
        for child in element:
            text += walk(child) + (child.tail or '')
        if element.tag in blocks:
            return '\n' + text + '\n'
        if element.tag in {'span', 'strong', 'a'}:
            return ' ' + text + ' '
        return text

    return '\n'.join(line for line in (compact(line) for line in walk(node).splitlines()) if line)


def paragraph(node):
    copy = deepcopy(node)
    for element in copy.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," exam-pno ")]'):
        element.drop_tree()
    return compact(copy.text_content())


def options_from(node):
    choices = [(element.get('data-pdh-letter'), compact(element.text_content()))
               for element in node.xpath('.//*[@data-pdh-letter]')]
    if len(choices) != 4 or {label for label, _ in choices} != set('ABCD') or not all(value for _, value in choices):
        raise ValueError('MCQ must have exactly four unique nonempty A-D choices')
    return dict(choices)


def answer_keys(root):
    result = {}
    for cell in root.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," wp-answers__cell ")]'):
        for number, letter in re.findall(r'\b(\d{1,2})\s+([A-Z])\b', cell.text_content()):
            number = int(number)
            if number in result:
                raise ValueError(f'Duplicate answer number {number}')
            result[number] = letter
    if set(result) != set(range(1, 56)):
        raise ValueError('Reference keys must contain exactly questions 1-55')
    return result


class PdfEvidence:
    def __init__(self, path):
        import pymupdf
        with pymupdf.open(path) as doc:
            self.pages = [page.get_text() for page in doc]
        self.offsets, offset = [], 0
        for page in self.pages:
            self.offsets.append(offset)
            offset += len(page) + 1
        self.text = '\n'.join(self.pages)
        self.normalized = norm(self.text)
        # A transcript paragraph can cross a PDF page footer. Remove those lines,
        # group headers, and bilingual translations only in this fallback index.
        self.english = norm('\n'.join(
            re.sub(r'^\d+\s+', '', line) for line in self.text.splitlines()
            if not re.search(r'[\u4e00-\u9fff]', line)
            and not re.search(r'^Questions \d+ to \d+|https?://', line)))
        self.stems = []
        for marker in re.finditer('【题目】', self.text):
            tail = self.text[marker.end():]
            stem = compact(re.split(r'【|\n\d{1,2}\.\s*A\)', tail, maxsplit=1)[0])
            if not stem.endswith('?'):
                raise ValueError('Incomplete PDF listening question stem')
            self.stems.append({'content': stem, 'page': bisect.bisect_right(self.offsets, marker.start())})
        if len(self.stems) != 25:
            raise ValueError('Answer PDF must contain 25 real listening question stems')
        answer_letters = re.findall(r'【答案】\s*([A-Z])', self.text)
        if len(answer_letters) != 55:
            raise ValueError('Answer PDF must contain exactly 55 answer markers')
        self.keys = dict(enumerate(answer_letters, 1))

    def corroborate(self, text):
        needle = norm(text)
        if len(needle) < 12 or (needle not in self.normalized and needle not in self.english):
            raise ValueError(f'Listening text not corroborated in answer PDF: {text[:100]}')
        prefix = needle[:100]
        pages = [number for number, text in enumerate(self.pages, 1) if prefix in norm(text)]
        return pages[:1]


def analysis_from(unit, expected_answer):
    analysis = one(select_class(unit, 'rp-ana'), f'analysis of {unit.get("id")}')
    raw_answer = compact(one(select_class(analysis, 'rp-ans'), 'module answer').text_content())
    if not re.match(rf'^{re.escape(expected_answer)}(?:$|[)\s])', raw_answer):
        raise ValueError(f'Module answer disagrees with whole paper: {unit.get("id")}')
    rows, headings = [], []
    for row in analysis:
        if not isinstance(row.tag, str) or not cls(row, 'rp-row'):
            continue
        tag = one(select_class(row, 'rp-tag'), 'analysis row tag')
        heading = compact(tag.text_content())
        if heading == '【题目】':
            continue
        copy = deepcopy(row)
        for label in select_class(copy, 'rp-tag'):
            label.drop_tree()
        body = plain(copy)
        if not body:
            raise ValueError(f'Empty analysis field: {unit.get("id")} {heading}')
        headings.append(heading)
        rows.append(f'{heading}\n{body}')
    if '【答案】' not in headings or len(rows) < 3:
        raise ValueError(f'Incomplete analysis: {unit.get("id")}')
    translations = [element for element in unit if isinstance(element.tag, str)
                    and (cls(element, 'lqp-stem') or cls(element, 'lqp-trans-choices'))]
    if translations:
        rows.insert(0, '【翻译】\n' + '\n'.join(plain(element) for element in translations))
    return '\n\n'.join(rows), headings


def build_candidate(paper_html=DEFAULT_CACHE / 'source.html',
                    answers_pdf=DEFAULT_CACHE / '2026-06-CET6-1-answers.pdf',
                    sections_dir=DEFAULT_CACHE / 'import-source-sections',
                    source_file='2026-06-CET6-1.pdf', root=ROOT):
    root = Path(root).resolve()
    match = re.fullmatch(r'(\d{4})-(\d{2})-(CET[46])-([123])\.pdf', source_file)
    if not match:
        raise ValueError('Invalid source filename')
    year, month, level, version = match.groups()
    exam = f'{year}-{month}-{version}'
    source_url = f'https://english-exam.lazynote.cn/{level.lower()}/paper/{exam}/'
    paper_html, answers_pdf, sections_dir = map(Path, (paper_html, answers_pdf, sections_dir))
    source = html.fromstring(paper_html.read_text(encoding='utf-8'))
    modules = {name: html.fromstring((sections_dir / f'{name}.html').read_text(encoding='utf-8'))
               for name in MODULE_SLUGS}
    # A cached module from another exam must never be silently accepted.
    for name, module in [('whole-paper', source), *modules.items()]:
        canonical = module.xpath('//link[@rel="canonical"]/@href')
        if len(canonical) != 1 or exam not in canonical[0].split('/') or f'/{level.lower()}/' not in canonical[0]:
            raise ValueError(f'Wrong exam or missing canonical URL in {name}')
    keys, pdf = answer_keys(source), PdfEvidence(answers_pdf)
    if pdf.keys != keys:
        raise ValueError('Answer PDF and whole paper HTML keys disagree')
    evidence = []

    def add_evidence(path, kind, url=None):
        path = Path(path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError('Evidence must be inside the workspace root')
        evidence.append({'path': path.relative_to(root).as_posix(), 'kind': kind,
                         'sha256': sha256(path), 'url': url})

    add_evidence(paper_html, 'whole-paper-html', source_url)
    add_evidence(answers_pdf, 'answer-pdf')
    for name, slug in MODULE_SLUGS.items():
        url = (f'https://english-exam.lazynote.cn/{level.lower()}/sections/listening/{exam}/' if name == 'listening'
               else f'https://english-exam.lazynote.cn/{level.lower()}/sections/{exam}/{slug}/')
        add_evidence(sections_dir / f'{name}.html', f'{name}-analysis-html', url)

    def question(number, kind, module_name, options=None):
        unit = by_id(modules[module_name], f'q-{number}')
        key = keys[number]
        if kind == 'listening':
            stem = one(unit.xpath('.//*[@data-pdh-stem]'), 'listening stem')
            content = pdf.stems[number - 1]['content']
            if norm(content) != norm(stem.text_content()):
                raise ValueError(f'HTML/PDF listening stem mismatch: {number}')
        elif kind == 'cloze':
            stem = one(select_class(unit, 'rp-cloze-stem'), 'cloze context')
            content = compact(stem.text_content())
            content = re.sub(rf'^{number}\.\s*', '', content)
            content = re.sub(r'__\((\d+)\)__', r'__\1__', content)
        else:
            whole_stem = one(by_id(source, f'qp-{number}').xpath('.//*[@data-pdh-stem]'), 'whole paper stem')
            module_stem = one(unit.xpath('.//*[@data-pdh-stem]'), 'module stem')
            content = compact(whole_stem.text_content())
            if content != compact(module_stem.text_content()):
                raise ValueError(f'Module/whole paper stem mismatch: {number}')
        if kind in {'listening', 'reading'}:
            options = options_from(by_id(source, f'qp-{number}'))
            if options != options_from(by_id(modules[module_name], f'qp-{number}')):
                raise ValueError(f'Module/whole paper option order mismatch: {number}')
        explanation, headings = analysis_from(unit, key)
        section = {'listening': 'Listening Comprehension', 'cloze': 'Reading Section A',
                   'matching': 'Reading Section B', 'reading': 'Reading Section C'}[kind]
        return {'q_number': number, 'section': section, 'content': content,
                'options': options or {}, 'correct_answer': key, 'explanation': explanation,
                'evidence': {'module': module_name, 'module_id': f'q-{number}',
                             'analysis_headings': headings,
                             'pdf_stem_pages': [pdf.stems[number - 1]['page']] if kind == 'listening' else []}}

    groups = []
    pieces = source.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," lt-piece ")]')
    for piece in pieces:
        identity = piece.get('id')
        numbers = [int(value.split('-')[1]) for value in piece.xpath('.//*[starts-with(@id,"qp-")]/@id')]
        if not numbers or numbers != list(range(min(numbers), max(numbers) + 1)):
            raise ValueError('Invalid listening group range')
        module_piece = by_id(modules['listening'], identity)
        paras = select_class(module_piece, 'lt-en')
        paragraph_ids = [node.get('id') for node in paras]
        if paragraph_ids != [f'p-{numbers[0]}-{n}' for n in range(1, len(paras) + 1)]:
            raise ValueError('Transcript paragraph numbers are incomplete')
        transcript_parts = [paragraph(node) for node in paras]
        pages = sorted({page for part in transcript_parts for page in pdf.corroborate(part)})
        start = float(one(piece.xpath('.//*[@data-listen-seek]'), 'audio start').get('data-listen-seek'))
        groups.append({'title': f'Listening: Questions {numbers[0]} to {numbers[-1]}',
                       'group_type': 'listening', 'passage': 'Audio context required.',
                       'audio_start': start, 'transcript': '\n\n'.join(transcript_parts),
                       'evidence': {'module_id': identity, 'pdf_transcript_pages': pages,
                                    'transcript_paragraphs_corroborated_in_pdf': len(paras)},
                       'questions': [question(n, 'listening', 'listening') for n in numbers]})

    bank_nodes = by_id(source, 'choices-part3-section-a').xpath('.//div[span[2]]')
    bank = [(compact(node[0].text_content()).strip(' )'), compact(node[1].text_content())) for node in bank_nodes]
    if len(bank) != 15 or {label for label, _ in bank} != set('ABCDEFGHIJKLMNO'):
        raise ValueError('Expected 15 unique A-O bank entries')
    bank = dict(bank)
    cloze_parts, blanks = [], []
    for node in source.xpath('//*[starts-with(@id,"p-part3-section-a-")]'):
        copy = deepcopy(node)
        for blank in copy.xpath('.//u'):
            number = int(compact(blank.text_content()))
            blanks.append(number)
            blank.text = f'__{number}__'
        cloze_parts.append(compact(copy.text_content()))
    if blanks != list(range(26, 36)):
        raise ValueError('Cloze must contain exactly blanks 26-35')
    groups.append({'title': 'Section A: Banked Cloze', 'group_type': 'cloze',
                   'passage': '\n\n'.join(cloze_parts), 'audio_start': None, 'transcript': None,
                   'questions': [question(n, 'cloze', 'cloze', dict(bank)) for n in range(26, 36)]})

    matching = source.xpath('//*[starts-with(@id,"p-part3-section-b-")]//*[@data-pdh-letter]')
    labels = [node.get('data-pdh-letter') for node in matching]
    if labels != list('ABCDEFGHIJKLM'):
        raise ValueError('Expected all matching paragraphs A-M in order')
    groups.append({'title': 'Section B: Matching', 'group_type': 'matching',
                   'passage': '\n\n'.join(f'{node.get("data-pdh-letter")}) {compact(node.text_content())}' for node in matching),
                   'audio_start': None, 'transcript': None,
                   'questions': [question(n, 'matching', 'matching') for n in range(36, 46)]})
    for first, module, pattern in [(46, 'reading1', r'p-part3-section-c-\d+'),
                                    (51, 'reading2', r'p-part3-section-c-2-\d+')]:
        paragraphs = [node for node in source.xpath('//*[@id]') if re.fullmatch(pattern, node.get('id', ''))]
        if not paragraphs:
            raise ValueError('Reading passage is empty')
        groups.append({'title': f'Section C: Questions {first} to {first + 4}', 'group_type': 'reading',
                       'passage': '\n\n'.join(compact(node.text_content()) for node in paragraphs),
                       'audio_start': None, 'transcript': None,
                       'questions': [question(n, 'reading', module) for n in range(first, first + 5)]})
    candidate = {'schema': 1, 'source_file': source_file, 'source_url': source_url,
                 'evidence': evidence, 'groups': groups,
                 'limitations': ['Publisher-provided reference answers; not certified as official.',
                                 'No new word-by-word audio audit was performed.',
                                 'Analysis HTML preserves semantic boundaries; PDF corroborates listening stems/transcripts.']}
    candidate['content_sha256'] = content_digest(candidate)
    candidate['validation'] = validate(candidate, root=root)
    return candidate


def validate(candidate, *, root=ROOT, check_evidence=True):
    if candidate.get('schema') != 1 or not re.fullmatch(r'\d{4}-\d{2}-CET[46]-[123]\.pdf', candidate.get('source_file', '')):
        raise ValueError('Invalid candidate schema/source_file')
    if candidate.get('content_sha256') != content_digest(candidate):
        raise ValueError('Candidate content digest changed')
    groups, seen, option_count = candidate.get('groups', []), [], 0
    if len(groups) != 11 or [g['group_type'] for g in groups] != ['listening'] * 7 + ['cloze', 'matching', 'reading', 'reading']:
        raise ValueError('A complete paper requires exactly 11 expected groups')
    expected_listening = [(1, 4), (5, 8), (9, 11), (12, 15), (16, 18), (19, 21), (22, 25)]
    for index, group in enumerate(groups):
        kind, questions = group['group_type'], group.get('questions', [])
        nums = [question['q_number'] for question in questions]
        if any(isinstance(n, bool) or not isinstance(n, int) for n in nums):
            raise ValueError('Question numbers must be integers')
        expected = list(range(*((expected_listening[index][0], expected_listening[index][1] + 1) if index < 7
                                else {7: (26, 36), 8: (36, 46), 9: (46, 51), 10: (51, 56)}[index])))
        if nums != expected:
            raise ValueError(f'Question range incomplete or duplicated: group {index + 1}')
        if not isinstance(group.get('title'), str) or not group['title'].strip() or not isinstance(group.get('passage'), str) or not group['passage'].strip():
            raise ValueError('Missing group title/passage')
        if kind == 'listening':
            start = group.get('audio_start')
            if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
                raise ValueError('Invalid listening start')
            transcript = group.get('transcript')
            if not isinstance(transcript, str) or len(transcript) < 100 or re.search(r'[\u4e00-\u9fff]', transcript):
                raise ValueError('Incomplete/non-English listening transcript')
        elif kind == 'cloze':
            if [int(n) for n in re.findall(r'__(\d+)__', group['passage'])] != list(range(26, 36)):
                raise ValueError('Cloze passage blank boundaries are incomplete')
        elif kind == 'matching':
            if re.findall(r'(?m)^([A-Z])\)', group['passage']) != list('ABCDEFGHIJKLM'):
                raise ValueError('Matching paragraph labels must be A-M')
        for question in questions:
            number, options = question['q_number'], question.get('options', {})
            for field in ('content', 'explanation', 'section'):
                value = question.get(field)
                if not isinstance(value, str) or not value.strip() or '\ufffd' in value or 'https://english-exam.' in value or '在线使用效果更好' in value:
                    raise ValueError(f'Incomplete or contaminated {field}: {number}')
            if len(question['explanation']) < 80 or question['explanation'].count('【答案】') != 1:
                raise ValueError(f'Incomplete analysis boundary: {number}')
            if kind == 'listening' and not question['content'].endswith('?'):
                raise ValueError(f'Missing real listening question: {number}')
            expected_labels = set('ABCDEFGHIJKLMNO') if kind == 'cloze' else (set() if kind == 'matching' else set('ABCD'))
            if set(options) != expected_labels or any(not isinstance(text, str) or not text.strip() or '\ufffd' in text for text in options.values()):
                raise ValueError(f'Invalid option labels/text: {number}')
            if kind == 'cloze' and options != questions[0]['options']:
                raise ValueError('Cloze bank differs between blanks')
            allowed = set('ABCDEFGHIJKLM') if kind == 'matching' else set(options)
            if question.get('correct_answer') not in allowed:
                raise ValueError(f'Answer not in current options/paragraphs: {number}')
            seen.append(number)
            option_count += len(options)
    if seen != list(range(1, 56)):
        raise ValueError('Paper must contain exactly questions 1-55')
    evidence = candidate.get('evidence', [])
    if check_evidence:
        root = Path(root).resolve(strict=True)
        if {item['kind'] for item in evidence} != {'whole-paper-html', 'answer-pdf', *[f'{n}-analysis-html' for n in MODULE_SLUGS]}:
            raise ValueError('Missing source evidence')
        for item in evidence:
            path = (root / item['path']).resolve(strict=True)
            if not path.is_relative_to(root) or sha256(path) != item['sha256']:
                raise ValueError('Source evidence changed or left workspace')
    return {'questions': 55, 'groups': 11, 'options': option_count,
            'listening_questions': 25, 'cloze_questions': 10,
            'matching_questions': 10, 'reading_questions': 10,
            'all_answers_and_explanations_present': True}


def _snapshot(conn, limits=None):
    values = {}
    for table in TABLES:
        clause, args = (' WHERE id<=?', (limits[table],)) if limits else ('', ())
        rows = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"{clause} ORDER BY id', args)]
        values[table] = {'rows': len(rows), 'sha256': digest(rows)}
    return values


def _existing_matches(conn, candidate):
    source = candidate['source_file']
    actual = conn.execute('SELECT * FROM question_group WHERE source_file=? ORDER BY id', (source,)).fetchall()
    own_questions = conn.execute('SELECT count(*) FROM question WHERE source_file=?', (source,)).fetchone()[0]
    if not actual and not own_questions:
        return False
    if len(actual) != 11 or own_questions != 55:
        raise ValueError('Existing source is partial or conflicts; it will not be overwritten')
    for row, expected in zip(actual, candidate['groups']):
        if any(row[field] != expected.get(field) for field in GROUP_FIELDS):
            raise ValueError('Existing source group differs; it will not be overwritten')
        questions = conn.execute('SELECT * FROM question WHERE group_id=? ORDER BY q_number,id', (row['id'],)).fetchall()
        if len(questions) != len(expected['questions']):
            raise ValueError('Existing source question range differs')
        for question, expected_question in zip(questions, expected['questions']):
            opts = dict(conn.execute('SELECT label,text FROM option WHERE question_id=? ORDER BY label', (question['id'],)))
            if question['source_file'] != source or any(question[field] != expected_question[field] for field in QUESTION_FIELDS) or opts != expected_question['options']:
                raise ValueError('Existing source question differs; it will not be overwritten')
    return True


def apply(database, candidate, *, dry_run=True, root=ROOT):
    """Append a complete reviewed candidate; default is a read-only dry run."""
    validation = validate(candidate, root=root)
    database = Path(database).resolve(strict=True)
    conn = sqlite3.connect(database.as_uri() + ('?mode=ro' if dry_run else '?mode=rw'), uri=True)
    conn.row_factory = sqlite3.Row
    result = {'source_file': candidate['source_file'], 'dry_run': dry_run, 'backup': None,
              'inserted_questions': 0, 'inserted_groups': 0, 'inserted_options': 0,
              'validation': validation}
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN' if dry_run else 'BEGIN IMMEDIATE')
        limits = {table: conn.execute(f'SELECT coalesce(max(id),0) FROM "{table}"').fetchone()[0] for table in TABLES}
        before = _snapshot(conn, limits)
        schema = tuple(tuple(row) for row in conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
        exists = _existing_matches(conn, candidate)
        result.update(already_present=exists, pending_questions=0 if exists else 55)
        if not dry_run and not exists:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = database.with_name(database.name + f'.bak-{stamp}-lazynote-import')
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as reader:
                with sqlite3.connect(backup) as target:
                    reader.backup(target)
            result['backup'] = str(backup)

            def authorize(action, table, column, database_name, trigger):
                if action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table not in {'question_group', 'question', 'option'}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            for group in candidate['groups']:
                cursor = conn.execute('INSERT INTO question_group(source_file,title,passage,group_type,audio_start,transcript) VALUES(?,?,?,?,?,?)',
                                      (candidate['source_file'], *(group.get(field) for field in GROUP_FIELDS)))
                group_id = cursor.lastrowid
                for question in group['questions']:
                    cursor = conn.execute('INSERT INTO question(group_id,source_file,section,content,q_number,correct_answer,explanation) VALUES(?,?,?,?,?,?,?)',
                                          (group_id, candidate['source_file'], *(question[field] for field in QUESTION_FIELDS)))
                    conn.executemany('INSERT INTO option(question_id,label,text) VALUES(?,?,?)',
                                     [(cursor.lastrowid, label, text) for label, text in sorted(question['options'].items())])
            conn.set_authorizer(None)
            if not _existing_matches(conn, candidate):
                raise RuntimeError('Inserted source readback failed')
            after = _snapshot(conn)
            expected = {'question_group': 11, 'question': 55, 'option': validation['options'], 'user_history': 0, 'wrong_question': 0}
            if any(after[table]['rows'] != before[table]['rows'] + expected[table] for table in TABLES):
                raise RuntimeError('Unexpected inserted rows, possibly from a trigger')
            result.update(inserted_questions=55, inserted_groups=11, inserted_options=validation['options'])
        if _snapshot(conn, limits) != before:
            raise RuntimeError('Existing rows changed; rollback')
        if tuple(tuple(row) for row in conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')) != schema:
            raise RuntimeError('Schema changed; rollback')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Database integrity check failed')
        conn.commit()
        result['existing_rows_unchanged'] = True
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, default=DEFAULT_CACHE / 'source.html')
    parser.add_argument('--answers-pdf', type=Path, default=DEFAULT_CACHE / '2026-06-CET6-1-answers.pdf')
    parser.add_argument('--sections-dir', type=Path, default=DEFAULT_CACHE / 'import-source-sections')
    parser.add_argument('--source-file', default='2026-06-CET6-1.pdf')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--database', type=Path, help='Optional dry-run target, unless --apply is explicit')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.database:
        parser.error('--apply requires an explicit --database')
    output = args.output.resolve()
    if output.suffix.lower() != '.json' or (args.database and output == args.database.resolve()):
        parser.error('Candidate output must be a JSON file separate from the database')
    candidate = build_candidate(args.html, args.answers_pdf, args.sections_dir, args.source_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    result = {'candidate': str(output), 'content_sha256': candidate['content_sha256'], 'validation': candidate['validation']}
    if args.database:
        result['database'] = apply(args.database, candidate, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
