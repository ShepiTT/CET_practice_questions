from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory
from models import db, QuestionGroup, Question, Option, UserHistory, WrongQuestion
from parser import parse_cet_pdf_v2, clean_noise
from sqlalchemy import func, text
from sqlalchemy.orm import selectinload
import os
import re
import math
from shared_listening import load_references, reference_for

app = Flask(__name__)

PAPER_RE = re.compile(r'^(\d{4})-(\d{2})-(CET[46])-([\d-]+)\.pdf$')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('CET4_DATABASE_URI', 'sqlite:///cet4_v2.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)
app.jinja_env.filters['clean_exam_text'] = clean_noise

AUDIO_DIR = os.path.join(app.root_path, 'data', 'audio')
app.config['SHARED_LISTENING_REFERENCES'] = load_references(
    os.path.join(app.root_path, 'data', 'shared_listening.json'))


def shared_listening_reference(filename):
    return reference_for(filename, app.config.get('SHARED_LISTENING_REFERENCES', {}), AUDIO_DIR)


def effective_practice_groups(filename):
    """Use a complete named listening source while retaining this paper's reading."""
    reference = shared_listening_reference(filename)
    query = QuestionGroup.query.filter_by(source_file=filename).options(
        selectinload(QuestionGroup.questions).selectinload(Question.options))
    if reference is not None:
        # A configured omitted paper never falls back to its old partial copies.
        query = query.filter(QuestionGroup.group_type != 'listening')
    groups = query.all()
    if reference and reference['enabled']:
        groups.extend(reference['groups'])
    groups.sort(key=lambda g: min((q.q_number for q in g.questions), default=999))
    return groups, reference

def ensure_column(table, column, ddl):
    """create_all() never alters existing tables; add a column that older databases lack."""
    columns = [row[1] for row in db.session.execute(text(f'PRAGMA table_info({table})'))]
    if column not in columns:
        db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
        db.session.commit()

def audio_for(groups):
    """{source_file: mp3 filename or None} for every paper among `groups`."""
    available = set(os.listdir(AUDIO_DIR)) if os.path.isdir(AUDIO_DIR) else set()
    result = {}
    for g in groups:
        mp3 = os.path.splitext(g.source_file)[0] + '.mp3'
        result[g.source_file] = mp3 if mp3 in available else None
    return result

@app.template_filter('mmss')
def mmss(seconds):
    seconds = int(seconds or 0)
    return f'{seconds // 60}:{seconds % 60:02d}'

def parse_paper_filename(filename):
    """Parse '2024-12-CET4-1.pdf' into (year, month, level, set_id) or None."""
    m = PAPER_RE.match(filename)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def find_sibling_paper(filename, available_papers):
    """Return the filename of a sibling paper to clone content from, or None.

    Rules (same as audio reuse):
      - For set 3, prefer set 2 if present, else set 1
      - For merged sets like '2-3', use set 1
      - For any other missing set, try set 1 as fallback
    """
    parsed = parse_paper_filename(filename)
    if not parsed:
        return None
    year, month, level, set_id = parsed

    def sibling(sid):
        return f'{year}-{month}-{level}-{sid}.pdf'

    if set_id == '3':
        for candidate_id in ('2', '1'):
            candidate = sibling(candidate_id)
            if candidate in available_papers:
                return candidate
    elif '-' in set_id:
        candidate = sibling('1')
        if candidate in available_papers:
            return candidate
    else:
        if set_id != '1':
            candidate = sibling('1')
            if candidate in available_papers:
                return candidate
    return None


GROUP_TYPES = ('listening', 'cloze', 'matching', 'reading')


def clone_group_from_donor(donor_group, target_filename):
    """Clone a QuestionGroup and its Questions/Options to target_filename."""
    new_group = QuestionGroup(
        source_file=target_filename,
        title=donor_group.title,
        passage=donor_group.passage,
        group_type=donor_group.group_type,
        audio_start=donor_group.audio_start,
        transcript=donor_group.transcript,
    )
    db.session.add(new_group)
    db.session.flush()

    for donor_q in donor_group.questions:
        new_q = Question(
            group_id=new_group.id,
            source_file=target_filename,
            section=donor_q.section,
            q_number=donor_q.q_number,
            content=donor_q.content,
            correct_answer=donor_q.correct_answer,
            explanation=donor_q.explanation,
        )
        db.session.add(new_q)
        db.session.flush()

        for donor_opt in donor_q.options:
            new_opt = Option(
                question_id=new_q.id,
                label=donor_opt.label,
                text=donor_opt.text,
            )
            db.session.add(new_opt)

    return new_group


def fill_missing_groups():
    """Clone missing group types from sibling papers.

    For papers that only print writing/translation and reference set 1 or 2 for
    the rest, clone listening/cloze/matching/reading groups from the sibling.
    Does not overwrite groups that already exist.

    Returns list of (target_file, group_type, donor_file) tuples for what was cloned.
    """
    all_papers = set(
        r[0] for r in db.session.query(Question.source_file).distinct().all()
    )

    paper_groups = {}
    for r in db.session.query(QuestionGroup.source_file, QuestionGroup.group_type).all():
        paper_groups.setdefault(r[0], set()).add(r[1])

    cloned = []

    for target_file in sorted(all_papers):
        existing_types = paper_groups.get(target_file, set())
        missing_types = [t for t in GROUP_TYPES if t not in existing_types]
        if not missing_types:
            continue

        donor_file = find_sibling_paper(target_file, all_papers)
        if not donor_file:
            continue

        donor_groups = QuestionGroup.query.filter_by(source_file=donor_file).all()
        donor_by_type = {g.group_type: g for g in donor_groups}

        for group_type in missing_types:
            if group_type == 'listening' and target_file in app.config.get('SHARED_LISTENING_REFERENCES', {}):
                continue
            if group_type not in donor_by_type:
                continue
            donor_group = donor_by_type[group_type]
            if not donor_group.questions:
                continue

            if QuestionGroup.query.filter_by(
                source_file=target_file, group_type=group_type
            ).first():
                continue

            clone_group_from_donor(donor_group, target_file)
            cloned.append((target_file, group_type, donor_file))
            paper_groups.setdefault(target_file, set()).add(group_type)

    return cloned


def import_paper(filename, groups_data):
    """Insert the groups and questions parsed from one PDF (caller commits)."""
    for g_data in groups_data:
        # Try to find existing group
        existing_group = QuestionGroup.query.filter_by(source_file=filename, title=g_data['title']).first()

        if existing_group:
            # Update existing group content but keep ID
            existing_group.passage = g_data['passage']
            existing_group.group_type = g_data['type']
            target_group = existing_group
        else:
            new_group = QuestionGroup(
                source_file=filename,
                title=g_data['title'],
                passage=g_data['passage'],
                group_type=g_data['type']
            )
            db.session.add(new_group)
            db.session.flush()
            target_group = new_group

        for q_data in g_data['questions']:
            # Try to find existing question
            existing_q = Question.query.filter_by(source_file=filename, q_number=q_data['q_number']).first()

            if existing_q:
                # Update content but KEEP correct_answer and explanation
                existing_q.content = q_data['content']
                existing_q.section = q_data['section']
                existing_q.group_id = target_group.id

                # Replace options: flush the orphan deletes first, otherwise the
                # new rows would hit the (question_id, label) unique constraint.
                existing_q.options = []
                db.session.flush()
                existing_q.options = [
                    Option(label=opt_data['label'], text=opt_data['text'])
                    for opt_data in q_data['options']
                ]
            else:
                new_q = Question(
                    group_id=target_group.id,
                    source_file=filename,
                    section=q_data['section'],
                    q_number=q_data['q_number'],
                    content=q_data['content']
                )
                db.session.add(new_q)
                db.session.flush()

                for opt_data in q_data['options']:
                    new_opt = Option(
                        question_id=new_q.id,
                        label=opt_data['label'],
                        text=opt_data['text']
                    )
                    db.session.add(new_opt)

def init_db():
    with app.app_context():
        db.create_all()
        ensure_column('question_group', 'audio_start', 'FLOAT')
        ensure_column('question_group', 'transcript', 'TEXT')
        print("Ensuring database tables exist...")
        
        data_dir = os.path.join(app.root_path, 'data')
        for filename in sorted(os.listdir(data_dir)):
            if not filename.endswith('.pdf'):
                continue
            # A paper already in the database is never re-parsed.
            if Question.query.filter_by(source_file=filename).first():
                continue
            print(f"Processing new file: {filename}...")
            try:
                groups_data = parse_cet_pdf_v2(os.path.join(data_dir, filename), allow_ocr=True)
            except Exception as e:
                print(f"Error parsing {filename}: {e}")
                continue
            if not groups_data:
                print(f"  nothing found in {filename} (scanned PDF without OCR cache?), skipped")
                continue
            import_paper(filename, groups_data)
        db.session.commit()

        cloned = fill_missing_groups()
        if cloned:
            print(f'\nFilled {len(cloned)} missing group(s) from sibling papers:')
            for target, gtype, donor in cloned:
                print(f'  {target} {gtype} <- {donor}')
            db.session.commit()

        print("Database sync complete.")

# Paper names carry their exam level: '2024-12-CET6-1.pdf'.
LEVEL_RE = re.compile(r'-CET(\d)-')
LEVEL_NAMES = {'4': '英语四级 CET-4', '6': '英语六级 CET-6'}
SPECIAL_TYPES = {
    'listening': {'label': '听力', 'description': '听录音，按同一段材料集中作答。'},
    'cloze': {'label': '选词填空', 'description': '结合上下文，从词库中选出合适的单词。'},
    'matching': {'label': '段落匹配', 'description': '定位关键信息，练习长篇阅读与同义替换。'},
    'reading': {'label': '仔细阅读', 'description': '围绕一篇文章，练习细节、推断与主旨题。'},
}


def special_inventory():
    """Nonempty groups with the same audio eligibility used by sampling."""
    rows = (db.session.query(QuestionGroup.id, QuestionGroup.source_file,
                             QuestionGroup.group_type, func.count(Question.id).label('question_count'))
            .join(Question, Question.group_id == QuestionGroup.id)
            .filter(QuestionGroup.group_type.in_(SPECIAL_TYPES))
            .group_by(QuestionGroup.id, QuestionGroup.source_file, QuestionGroup.group_type)
            .order_by(QuestionGroup.id).all())
    audio_files = audio_for([row for row in rows if row.group_type == 'listening'])
    aliases = app.config.get('SHARED_LISTENING_REFERENCES', {})
    return [row for row in rows if row.group_type != 'listening'
            or (row.source_file not in aliases and audio_files.get(row.source_file))]


def special_context(pool=None, level='', count=3):
    if pool is None:
        pool = special_inventory()
    counts = {scope: {kind: {'groups': 0, 'questions': 0} for kind in SPECIAL_TYPES}
              for scope in ('all', '4', '6')}
    for row in pool:
        scopes = ['all']
        if level_of(row.source_file) in LEVEL_NAMES:
            scopes.append(level_of(row.source_file))
        for scope in scopes:
            counts[scope][row.group_type]['groups'] += 1
            counts[scope][row.group_type]['questions'] += row.question_count
    return {'special_types': SPECIAL_TYPES, 'special_counts': counts,
            'special_level': level, 'special_count': count}


def level_of(source_file):
    """'4' or '6' from the file name, or None when the name does not say."""
    m = LEVEL_RE.search(source_file or '')
    return m.group(1) if m else None

@app.route('/')
def index():
    # Get unique source files and sort them by year descending
    files = db.session.query(QuestionGroup.source_file).distinct().all()
    file_list = sorted([f[0] for f in files], reverse=True)

    per_paper = dict(db.session.query(Question.source_file, func.count(Question.id))
                     .group_by(Question.source_file).all())
    grouped = {}
    for f in file_list:
        grouped.setdefault(level_of(f) or '其他', []).append(f)
    levels = [{
        'key': key,
        'label': LEVEL_NAMES.get(key, key),
        'papers': papers,
        'questions': sum(per_paper.get(p, 0) for p in papers),
    } for key, papers in sorted(grouped.items())]

    # Statistics
    total_q_count = Question.query.count()
    # Count unique questions answered (UserHistory stores each attempt, so we need distinct question_id)
    answered_q_count = db.session.query(func.count(func.distinct(UserHistory.question_id))).scalar()
    unanswered_q_count = total_q_count - answered_q_count

    stats = {
        'total': total_q_count,
        'answered': answered_q_count,
        'unanswered': unanswered_q_count
    }

    return render_template('index.html', levels=levels, stats=stats,
                           shared_listening_sources=app.config.get('SHARED_LISTENING_REFERENCES', {}),
                           **special_context())

@app.route('/practice/<filename>')
def practice(filename):
    groups, shared_reference = effective_practice_groups(filename)
    return render_template('practice.html', groups=groups, filename=filename, mode='normal',
                           audio_files=audio_for(groups), shared_reference=shared_reference)

@app.route('/random_practice')
def random_practice():
    import random
    # Randomly pick 5 groups from all available groups
    all_groups = QuestionGroup.query.all()
    level = request.args.get('level')
    if level:
        all_groups = [g for g in all_groups if level_of(g.source_file) == level]
    # Listening without its recording is guesswork; keep those groups out of the random pool.
    has_audio = audio_for(all_groups)
    aliases = app.config.get('SHARED_LISTENING_REFERENCES', {})
    all_groups = [g for g in all_groups if g.group_type != 'listening'
                  or (g.source_file not in aliases and has_audio[g.source_file])]
    if not all_groups:
        return redirect(url_for('index'))
    
    sample_size = min(len(all_groups), 5)
    random_groups = random.sample(all_groups, sample_size)
    
    return render_template('practice.html', groups=random_groups, filename="随机刷题模式", mode='random',
                           audio_files=has_audio, level=level)


@app.route('/special_practice')
def special_practice():
    import random
    kind = request.args.get('type', '')
    level = request.args.get('level', '')
    raw_count = request.args.get('count', '3')
    valid_count = bool(re.fullmatch(r'[1-9]|10', raw_count))
    error = None
    if kind and kind not in SPECIAL_TYPES:
        error = '请选择有效的专项题型。'
    elif level not in ('', '4', '6'):
        error = '请选择全部、英语四级或英语六级。'
    elif not valid_count:
        error = '每次练习的题组数应为 1 到 10 的整数。'
    count = int(raw_count) if valid_count else 3
    pool = special_inventory()
    context = special_context(pool, level if level in ('', '4', '6') else '', count)
    if error or not kind:
        return render_template('special_practice.html', error=error, **context), 400 if error else 200

    eligible = [row for row in pool if row.group_type == kind
                and (not level or level_of(row.source_file) == level)]
    chosen = random.sample(eligible, min(count, len(eligible)))
    by_id = {group.id: group for group in QuestionGroup.query.filter(
        QuestionGroup.id.in_([row.id for row in chosen])).options(
            selectinload(QuestionGroup.questions).selectinload(Question.options)).all()} if chosen else {}
    groups = [by_id[row.id] for row in chosen]
    return render_template(
        'practice.html', groups=groups, filename=SPECIAL_TYPES[kind]['label'] + '专项练习',
        mode='special', level=level, special_type=kind, count=count,
        level_label=LEVEL_NAMES.get(level, '全部等级'), available_groups=len(eligible),
        question_count=sum(len(group.questions) for group in groups),
        audio_files=audio_for(groups), **context)

@app.route('/audio/<path:filename>')
def audio(filename):
    # conditional=True answers Range requests, so the player can seek.
    return send_from_directory(AUDIO_DIR, filename, conditional=True)

@app.route('/set_audio_start', methods=['POST'])
def set_audio_start():
    data = json_object()
    group = db.session.get(QuestionGroup, positive_id(data.get('group_id'))) if data else None
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    seconds = data.get('seconds')
    if seconds is not None:
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid seconds'}), 400
        if not math.isfinite(seconds) or seconds < 0 or isinstance(data.get('seconds'), bool):
            return jsonify({'error': 'Invalid seconds'}), 400
    group.audio_start = seconds
    db.session.commit()
    return jsonify({'status': 'success', 'audio_start': group.audio_start})

# Validate against each question's offered choices, including matching beyond O.
def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9]\d*', str(value)):
        return None
    return int(value)


def normalize_answer(value):
    return value.strip().upper() if isinstance(value, str) else None

@app.route('/answer/<int:q_id>')
def get_answer(q_id):
    """Read-only reveal for the "show answers" buttons: never records an attempt."""
    question = db.session.get(Question, q_id)
    if not question:
        return jsonify({'error': 'Question not found'}), 404
    return jsonify({
        'correct_answer': question.correct_answer,
        'explanation': question.explanation
    })

@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    data = json_object()
    q_id = positive_id(data.get('question_id'))
    user_answer = normalize_answer(data.get('answer'))
    if not q_id or not user_answer:
        return jsonify({'error': '请输入有效题号和答案'}), 400
    
    question = db.session.get(Question, q_id)
    if not question:
        return jsonify({'error': 'Question not found'}), 404

    if user_answer not in question.allowed_answers:
        return jsonify({'error': '请选择本题提供的选项或段落字母'}), 400
    
    # Without a key there is nothing to be right or wrong about. Grading it anyway told the
    # user every answer was wrong and filed the question in the wrong-question list.
    if normalize_answer(question.correct_answer) not in question.allowed_answers:
        return jsonify({
            'unanswered': True,
            'is_correct': None,
            'correct_answer': None,
            'explanation': question.explanation
        })

    is_correct = user_answer == question.correct_answer.strip().upper()

    history = UserHistory(
        question_id=q_id,
        user_answer=user_answer,
        is_correct=is_correct
    )
    db.session.add(history)

    if not is_correct:
        exists = WrongQuestion.query.filter_by(question_id=q_id).first()
        if not exists:
            wrong = WrongQuestion(question_id=q_id)
            db.session.add(wrong)

    db.session.commit()

    return jsonify({
        'is_correct': is_correct,
        'correct_answer': question.correct_answer,
        'explanation': question.explanation
    })

@app.route('/admin')
def admin():
    files = db.session.query(QuestionGroup.source_file).distinct().all()
    files = [f[0] for f in files]
    selected_file = request.args.get('file', files[0] if files else None)
    
    questions = []
    if selected_file:
        questions = Question.query.filter_by(source_file=selected_file).order_by(Question.q_number).all()
        
    return render_template('admin.html', questions=questions, files=files, selected_file=selected_file)

@app.route('/set_answer', methods=['POST'])
def set_answer():
    data = json_object()
    q_id = positive_id(data.get('question_id'))
    answer = normalize_answer(data.get('answer'))
    if not q_id or answer is None:
        return jsonify({'error': '请输入有效题号和答案'}), 400
    
    question = db.session.get(Question, q_id)
    if question:
        if answer and answer not in question.allowed_answers:
            return jsonify({'error': '标准答案必须属于本题选项或段落字母'}), 400
        question.correct_answer = answer
        db.session.commit()
        return jsonify({'status': 'success'})
    return jsonify({'error': 'Not found'}), 404

@app.route('/batch_set_answers', methods=['POST'])
def batch_set_answers():
    data = json_object()
    filename = data.get('filename')
    answers = data.get('answers')
    
    if not isinstance(filename, str) or not isinstance(answers, list) or not answers:
        return jsonify({'error': 'Missing data'}), 400
        
    updated_count = 0
    not_found = []
    changes = []
    seen = set()
    
    # Debug: log the received filename
    print(f"Batch setting answers for: {filename}")
    
    for item in answers:
        if not isinstance(item, dict):
            return jsonify({'error': '答案条目格式无效，未保存任何修改'}), 400
        q_num = positive_id(item.get('q_number'))
        ans = normalize_answer(item.get('answer', ''))
        expl = item.get('explanation')
        if not q_num or q_num in seen or ans is None or (expl is not None and not isinstance(expl, str)):
            return jsonify({'error': '存在重复题号或无效条目，未保存任何修改'}), 400
        seen.add(q_num)
        
        # Use filter and update to be more direct
        question = Question.query.filter_by(source_file=filename, q_number=q_num).first()
        if question:
            if ans and ans not in question.allowed_answers:
                return jsonify({'error': f'第 {q_num} 题答案不在本题选项中，未保存任何修改'}), 400
            changes.append((question, ans, expl))
        else:
            not_found.append(q_num)
            
    for question, ans, expl in changes:
        if ans:
            question.correct_answer = ans
        if expl:
            question.explanation = expl
        updated_count += 1
    db.session.commit()
    print(f"Updated {updated_count} questions. Not found: {not_found}")
    return jsonify({
        'status': 'success', 
        'updated_count': updated_count,
        'not_found': not_found
    })

@app.route('/wrong_questions')
def wrong_questions_list():
    # Get all question IDs that are in the wrong question list
    wrong_q_ids = [w.question_id for w in WrongQuestion.query.all()]
    
    # Get unique group IDs for these wrong questions
    group_ids = db.session.query(Question.group_id).filter(Question.id.in_(wrong_q_ids)).distinct().all()
    group_ids = [g[0] for g in group_ids if g[0] is not None]
    
    # Fetch the full group objects
    wrong_groups = QuestionGroup.query.filter(QuestionGroup.id.in_(group_ids)).all()
    
    return render_template('wrong_questions.html', groups=wrong_groups, wrong_q_ids=wrong_q_ids,
                           audio_files=audio_for(wrong_groups))

@app.route('/remove_wrong', methods=['POST'])
def remove_wrong():
    q_id = request.json.get('question_id')
    wrong = WrongQuestion.query.filter_by(question_id=q_id).first()
    if wrong:
        db.session.delete(wrong)
        db.session.commit()
        return jsonify({'status': 'success'})
    return jsonify({'error': 'Not found'}), 404

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
