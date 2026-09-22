from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory
from models import db, QuestionGroup, Question, Option, UserHistory, WrongQuestion
from parser import parse_cet_pdf_v2, clean_noise
from sqlalchemy import func, text
import os
import re
import math

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('CET4_DATABASE_URI', 'sqlite:///cet4_v2.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)
app.jinja_env.filters['clean_exam_text'] = clean_noise

AUDIO_DIR = os.path.join(app.root_path, 'data', 'audio')

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
                groups_data = parse_cet_pdf_v2(os.path.join(data_dir, filename))
            except Exception as e:
                print(f"Error parsing {filename}: {e}")
                continue
            if not groups_data:
                print(f"  nothing found in {filename} (scanned PDF without a text layer?), skipped")
                continue
            import_paper(filename, groups_data)
        db.session.commit()
        print("Database sync complete.")

# Paper names carry their exam level: '2024-12-CET6-1.pdf'.
LEVEL_RE = re.compile(r'-CET(\d)-')
LEVEL_NAMES = {'4': '英语四级 CET-4', '6': '英语六级 CET-6'}


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

    return render_template('index.html', levels=levels, stats=stats)

@app.route('/practice/<filename>')
def practice(filename):
    # Get all groups for this file
    groups = QuestionGroup.query.filter_by(source_file=filename).all()
    
    # Sort groups by the minimum question number in each group
    # This ensures Section A (26-35) comes before Section B (36-45), etc.
    groups.sort(key=lambda g: min([q.q_number for q in g.questions]) if g.questions else 999)
    
    return render_template('practice.html', groups=groups, filename=filename, mode='normal',
                           audio_files=audio_for(groups))

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
    all_groups = [g for g in all_groups if g.group_type != 'listening' or has_audio[g.source_file]]
    if not all_groups:
        return redirect(url_for('index'))
    
    sample_size = min(len(all_groups), 5)
    random_groups = random.sample(all_groups, sample_size)
    
    return render_template('practice.html', groups=random_groups, filename="随机刷题模式", mode='random',
                           audio_files=has_audio, level=level)

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
