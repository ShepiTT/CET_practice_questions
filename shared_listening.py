"""Resolve explicitly reviewed shared recordings without copying exam answers."""

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

from sqlalchemy.orm import selectinload

from models import Question, QuestionGroup


PAPER = re.compile(r'^(\d{4}-\d{2}-CET[46])-(\d(?:-\d)?)\.pdf$')


def load_references(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    references = data.get('references')
    if not isinstance(references, dict):
        raise ValueError('Shared listening configuration requires a references mapping')
    return references


@lru_cache(maxsize=128)
def _file_hash(path, size, modified_ns, changed_ns):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    stat = Path(path).stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (size, modified_ns, changed_ns):
        raise OSError('Recording changed during validation')
    return h.hexdigest()


def recording_hash(path):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return _file_hash(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def rows_hash(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def reference_for(filename, references, audio_dir):
    """Return None for ordinary papers, or an enabled/failed reviewed reference.

    Group and question IDs remain those of the named source paper. No target
    paper A-D order is inferred, and this function never writes the database.
    """
    if filename not in references:
        return None
    entry = references[filename]
    source = entry.get('source_file') if isinstance(entry, dict) else None
    result = {'enabled': False, 'source_file': source, 'question_count': 0, 'groups': []}

    def failed(message):
        result['error'] = message
        return result

    target_match = PAPER.fullmatch(filename)
    source_match = PAPER.fullmatch(source) if isinstance(source, str) else None
    if (not target_match or not source_match or target_match[1] != source_match[1]
            or target_match[2] not in ('3', '2-3') or source_match[2] not in ('1', '2')
            or source in references):
        return failed('共享来源与本卷的考试场次或等级不一致，请核对来源。')
    expected = entry.get('audio_sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        return failed('共享录音缺少已核验的来源记录。')
    try:
        hashes = [recording_hash(Path(audio_dir)/Path(name).with_suffix('.mp3'))
                  for name in (filename, source)]
    except OSError:
        return failed('共享录音暂不可用，请检查两份来源卷的音频文件。')
    if any(value != expected for value in hashes):
        return failed('共享录音校验未通过，请重新核对本卷和来源卷的录音。')
    if entry.get('omission_pdf_sha256'):
        try:
            if recording_hash(Path(audio_dir).parent/filename) != entry['omission_pdf_sha256']:
                return failed('本卷原 PDF 已变化，需重新核对省略听力说明。')
        except OSError:
            return failed('本卷省略说明暂不可用，请检查原 PDF。')

    groups = (QuestionGroup.query.filter_by(source_file=source, group_type='listening')
              .options(selectinload(QuestionGroup.questions).selectinload(Question.options)).all())
    groups.sort(key=lambda g: min((q.q_number for q in g.questions if isinstance(q.q_number, int)), default=999))
    questions = sorted((q for g in groups for q in g.questions), key=lambda q: (q.q_number or 0, q.id))
    if [q.q_number for q in questions] != list(range(1, 26)):
        return failed('共享来源的听力题号不完整，需先核齐第 1–25 题。')
    question_rows, option_rows = [], []
    for group in groups:
        if (not group.questions or not (group.transcript or '').strip()
                or group.audio_start is None or not math.isfinite(group.audio_start) or group.audio_start < 0):
            return failed('共享来源的原文或播放起点不完整，需先核对材料。')
        title = re.search(r'Questions (\d+) to (\d+)', group.title or '')
        if not title or [q.q_number for q in group.questions] != list(range(int(title[1]), int(title[2])+1)):
            return failed('共享来源的题组范围与实际题号不一致。')
    for question in questions:
        options = sorted(question.options, key=lambda o: (o.label, o.id))
        if (question.source_file != source or not (question.content or '').strip()
                or not (question.explanation or '').strip() or question.correct_answer not in tuple('ABCD')
                or [o.label for o in options] != list('ABCD')
                or any(not o.text.strip() for o in options)):
            return failed('共享来源的选项、答案或解析不完整，请先完成核验。')
        question_rows.append({column.name: getattr(question, column.name) for column in question.__table__.columns})
        option_rows.extend({column.name: getattr(option, column.name) for column in option.__table__.columns}
                           for option in options)
    for name, rows in (('question_rows_sha256', question_rows), ('option_rows_sha256', option_rows)):
        if entry.get(name) and entry[name] != rows_hash(rows):
            return failed('共享来源的题目内容已变化，需重新核对选项和答案。')
    result.update(enabled=True, question_count=25, groups=groups, error=None)
    return result
