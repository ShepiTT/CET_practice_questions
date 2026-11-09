"""A new paper must keep source letter identity and every existing learner row."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from lxml import html
import pytest

from scripts import import_lazynote_paper as importer


def seal(candidate):
    candidate['content_sha256'] = importer.content_digest(candidate)
    return candidate


@pytest.fixture
def candidate(tmp_path):
    groups = []
    ranges = [(1, 4), (5, 8), (9, 11), (12, 15), (16, 18), (19, 21), (22, 25),
              (26, 35), (36, 45), (46, 50), (51, 55)]
    for index, (first, last) in enumerate(ranges):
        kind = ['listening'] * 7 + ['cloze', 'matching', 'reading', 'reading']
        kind = kind[index]
        passage = ('Audio context required.' if kind == 'listening' else
                   '\n\n'.join(f'{letter}) Paragraph {letter} has its own source facts.' for letter in 'ABCDEFGHIJKLM') if kind == 'matching' else
                   'A passage containing ' + ' '.join(f'__{n}__' for n in range(26, 36)) if kind == 'cloze' else
                   'A complete reading passage about a clearly defined research topic. ' * 8)
        group = {'title': f'{kind}: Questions {first} to {last}', 'group_type': kind,
                 'passage': passage, 'audio_start': first * 40.0 if kind == 'listening' else None,
                 'transcript': 'An English recording discusses the reasons and evidence for the proposed conclusion. ' * 5 if kind == 'listening' else None,
                 'questions': []}
        for number in range(first, last + 1):
            labels = 'ABCDEFGHIJKLMNO' if kind == 'cloze' else '' if kind == 'matching' else 'ABCD'
            group['questions'].append({'q_number': number, 'section': kind,
                                      'content': f'What conclusion is supported by question {number}?' if kind != 'cloze' else f'Fill __{number}__ in this context.',
                                      'options': {letter: f'Word {letter}' if kind == 'cloze' else f'Question {number} has option {letter}.' for letter in labels},
                                      'correct_answer': 'A',
                                      'explanation': '【答案】\nA\n\n【定位】\nThe source gives the relevant evidence.\n\n【分析】\n这是当前题目的完整来源解析，说明正确选项与原文信息的对应关系，并且解释其他选项为什么不能成立。'})
        groups.append(group)
    evidence = []
    for kind in ['whole-paper-html', 'answer-pdf', *[f'{name}-analysis-html' for name in importer.MODULE_SLUGS]]:
        path = tmp_path / (kind + '.txt')
        path.write_text(f'Unchanged source evidence for {kind}.', encoding='utf-8')
        evidence.append({'path': path.name, 'kind': kind, 'sha256': importer.sha256(path), 'url': None})
    return seal({'schema': 1, 'source_file': '2026-06-CET6-1.pdf', 'source_url': 'https://example.invalid/paper/',
                 'groups': groups, 'evidence': evidence})


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'questions.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE question_group(id INTEGER PRIMARY KEY, source_file TEXT, title TEXT,
              passage TEXT, group_type TEXT, audio_start REAL, transcript TEXT);
            CREATE TABLE question(id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES question_group(id),
              source_file TEXT, section TEXT, content TEXT, q_number INTEGER, correct_answer TEXT, explanation TEXT);
            CREATE TABLE option(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id),
              label TEXT, text TEXT, UNIQUE(question_id,label));
            CREATE TABLE user_history(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id),
              user_answer TEXT, is_correct INTEGER, timestamp TEXT);
            CREATE TABLE wrong_question(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id), added_at TEXT);
            INSERT INTO question_group VALUES(1,'2025-12-CET6-1.pdf','Existing group','Existing passage','reading',NULL,NULL);
            INSERT INTO question VALUES(1,1,'2025-12-CET6-1.pdf','Reading','Existing learner question',46,'B','Existing explanation');
            INSERT INTO option VALUES(1,1,'A','Old A'),(2,1,'B','Old B');
            INSERT INTO user_history VALUES(1,1,'A',0,'2026-11-09');
            INSERT INTO wrong_question VALUES(1,1,'2026-11-09');
        ''')
    return path


def rows(path):
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in importer.TABLES}


def test_dry_run_is_byte_for_byte_read_only(database, candidate, tmp_path):
    before = importer.sha256(database)
    result = importer.apply(database, candidate, root=tmp_path)
    assert result['pending_questions'] == 55
    assert result['inserted_questions'] == 0
    assert result['backup'] is None
    assert importer.sha256(database) == before
    assert not list(tmp_path.glob('*.bak-*'))


def test_append_readback_preserves_old_rows_and_is_idempotent(database, candidate, tmp_path):
    before = rows(database)
    result = importer.apply(database, candidate, dry_run=False, root=tmp_path)
    assert (result['inserted_groups'], result['inserted_questions'], result['inserted_options']) == (11, 55, 290)
    assert rows(result['backup']) == before
    after = rows(database)
    for table, old in before.items():
        assert after[table][:len(old)] == old
    assert after['user_history'] == before['user_history']
    assert after['wrong_question'] == before['wrong_question']
    digest = importer.sha256(database)
    second = importer.apply(database, candidate, dry_run=False, root=tmp_path)
    assert second['already_present'] is True
    assert second['pending_questions'] == 0
    assert second['backup'] is None
    assert importer.sha256(database) == digest


def test_existing_source_partial_is_never_overwritten(database, candidate, tmp_path):
    with sqlite3.connect(database) as conn:
        conn.execute('INSERT INTO question_group(source_file,title) VALUES(?,?)', (candidate['source_file'], 'Partial import'))
    before = importer.sha256(database)
    with pytest.raises(ValueError, match='partial or conflicts'):
        importer.apply(database, candidate, dry_run=False, root=tmp_path)
    assert importer.sha256(database) == before
    assert not list(tmp_path.glob('*.bak-*'))


def test_existing_full_source_conflict_is_never_overwritten(database, candidate, tmp_path):
    importer.apply(database, candidate, dry_run=False, root=tmp_path)
    changed = deepcopy(candidate)
    changed['groups'][0]['questions'][0]['correct_answer'] = 'B'
    seal(changed)
    before = importer.sha256(database)
    with pytest.raises(ValueError, match='differs'):
        importer.apply(database, changed, dry_run=False, root=tmp_path)
    assert importer.sha256(database) == before


@pytest.mark.parametrize('trigger', [
    "CREATE TRIGGER malicious AFTER INSERT ON question BEGIN UPDATE user_history SET user_answer='D' WHERE id=1; END;",
    "CREATE TRIGGER extras AFTER INSERT ON question_group BEGIN INSERT INTO question(group_id,source_file,q_number) VALUES(1,'2025-12-CET6-1.pdf',99); END;",
])
def test_trigger_cannot_modify_or_inject_rows(database, candidate, tmp_path, trigger):
    with sqlite3.connect(database) as conn:
        conn.execute(trigger)
    before = rows(database)
    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        importer.apply(database, candidate, dry_run=False, root=tmp_path)
    assert rows(database) == before


def test_changed_evidence_is_rejected_before_backup(database, candidate, tmp_path):
    (tmp_path / candidate['evidence'][0]['path']).write_text('Changed', encoding='utf-8')
    before = importer.sha256(database)
    with pytest.raises(ValueError, match='evidence changed'):
        importer.apply(database, candidate, dry_run=False, root=tmp_path)
    assert importer.sha256(database) == before
    assert not list(tmp_path.glob('*.bak-*'))


@pytest.mark.parametrize('mutation,message', [
    (lambda c: c['groups'][0]['questions'].pop(), 'range incomplete'),
    (lambda c: c['groups'][0]['questions'][1].update(q_number=1), 'range incomplete'),
    (lambda c: c['groups'][0]['questions'][0].update(content='Question 1'), 'real listening question'),
    (lambda c: c['groups'][0]['questions'][0].update(explanation='【答案】A'), 'analysis boundary'),
    (lambda c: c['groups'][0]['questions'][0].update(correct_answer='O'), 'Answer not'),
    (lambda c: c['groups'][0]['questions'][0]['options'].pop('D'), 'option labels'),
    (lambda c: c['groups'][0].update(audio_start=float('nan')), 'listening start'),
    (lambda c: c['groups'][0].update(transcript='Chinese 原文'), 'transcript'),
    (lambda c: c['groups'][7].update(passage='Missing blanks'), 'blank boundaries'),
    (lambda c: c['groups'][7]['questions'][1]['options'].update(A='Different bank word'), 'bank differs'),
    (lambda c: c['groups'][8].update(passage='A) No other paragraphs'), 'paragraph labels'),
    (lambda c: c['groups'][10]['questions'][-1].update(explanation='【答案】A 在线使用效果更好'), 'contaminated'),
])
def test_incomplete_or_contaminated_candidate_is_rejected(candidate, tmp_path, mutation, message):
    mutation(candidate)
    seal(candidate)
    with pytest.raises(ValueError, match=message):
        importer.validate(candidate, root=tmp_path)


def test_candidate_digest_detects_unreviewed_field_edit(candidate, tmp_path):
    candidate['groups'][0]['questions'][0]['options']['A'] = 'Changed without review'
    with pytest.raises(ValueError, match='digest changed'):
        importer.validate(candidate, root=tmp_path)


def analysis_node(number, answer='A', final='A complete final exclusion explanation.'):
    return html.fromstring(f'''<div id="q-{number}"><div id="qp-{number}">NEXT OPTIONS MUST STAY OUT</div>
      <div class="rp-ana"><div class="rp-row rp-row--answer"><span class="rp-tag">【答案】</span><strong class="rp-ans">{answer}</strong></div>
      <div class="rp-row"><span class="rp-tag">【定位】</span><p>Original <strong>important</strong> source sentence.<button>播放</button><a class="px-pill">P4</a></p></div>
      <div class="rp-row"><span class="rp-tag">【排除】</span><ul><li>B) Wrong claim.</li><li>D) {final}</li></ul></div></div>
      <div id="q-{number + 1}">NEXT QUESTION MUST STAY OUT</div></div>''')


def test_semantic_analysis_boundary_keeps_complete_last_exclusion_and_removes_controls():
    text, headings = importer.analysis_from(analysis_node(1), 'A')
    assert headings == ['【答案】', '【定位】', '【排除】']
    assert 'Original important source sentence.' in text
    assert 'D) A complete final exclusion explanation.' in text
    assert 'NEXT' not in text
    assert '播放' not in text and 'P4' not in text
    assert 'B) Wrong claim.\nD)' in text


def test_semantic_analysis_refuses_cross_version_letter():
    with pytest.raises(ValueError, match='disagrees'):
        importer.analysis_from(analysis_node(1, answer='D'), 'A')


def test_answer_parser_requires_unique_complete_1_to_55():
    body = '<div class="wp-answers__cell">' + ' '.join(f'{n} A' for n in range(1, 56)) + '</div>'
    assert len(importer.answer_keys(html.fromstring(body))) == 55
    with pytest.raises(ValueError, match='Duplicate'):
        importer.answer_keys(html.fromstring(body.replace('55 A', '54 B')))
    with pytest.raises(ValueError, match='exactly'):
        importer.answer_keys(html.fromstring(body.replace('55 A', '')))


def test_pdf_parser_uses_real_stems_and_corroborates_across_page_header(monkeypatch):
    import pymupdf
    first = '\n'.join(f'【题目】What does source question {n} ask?\n{n}. A) A choice.\n【答案】A' for n in range(1, 26))
    second = '\n'.join('【答案】A' for n in range(26, 56))
    first += '\nA paragraph starts with a sufficiently long original recording phrase, so\nQuestions 22 to 25 are based on the recording you have just heard.\n'
    second = 'we can read the remainder after the page break.\n' + second

    class Page:
        def __init__(self, text): self.text = text
        def get_text(self): return self.text

    class Doc(list):
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(pymupdf, 'open', lambda path: Doc([Page(first), Page(second)]))
    pdf = importer.PdfEvidence('unused.pdf')
    assert len(pdf.stems) == 25
    assert pdf.stems[24] == {'content': 'What does source question 25 ask?', 'page': 1}
    assert len(pdf.keys) == 55
    pdf.corroborate('A paragraph starts with a sufficiently long original recording phrase, so we can read the remainder after the page break.')
    with pytest.raises(ValueError, match='not corroborated'):
        pdf.corroborate('A completely unsupported recording sentence was invented.')


def test_candidate_only_cli_never_accesses_database(monkeypatch, tmp_path, candidate, capsys):
    output = tmp_path / 'candidate.json'
    candidate['validation'] = importer.validate(candidate, root=tmp_path)
    monkeypatch.setattr(importer, 'build_candidate', lambda *args: candidate)
    monkeypatch.setattr(importer, 'apply', lambda *args, **kwargs: pytest.fail('Unexpected DB access'))
    monkeypatch.setattr('sys.argv', ['import_lazynote_paper.py', '--output', str(output)])
    importer.main()
    assert json.loads(output.read_text(encoding='utf-8')) == candidate
    assert 'candidate' in capsys.readouterr().out


def test_apply_cli_requires_explicit_database(monkeypatch, tmp_path):
    monkeypatch.setattr('sys.argv', ['import_lazynote_paper.py', '--output', str(tmp_path / 'candidate.json'), '--apply'])
    with pytest.raises(SystemExit):
        importer.main()
