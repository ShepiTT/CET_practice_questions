from flask import Flask, render_template, request, jsonify, redirect, url_for
from models import db, QuestionGroup, Question, Option, UserHistory, WrongQuestion
from parser import parse_cet_pdf_v2
from sqlalchemy import func
import os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///cet4_v2.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

def init_db():
    with app.app_context():
        db.create_all()
        print("Ensuring database tables exist...")
        
        data_dir = os.path.join(app.root_path, 'data')
        for filename in os.listdir(data_dir):
            if filename.endswith('.pdf'):
                # Check if this file has already been processed to save time
                file_exists = Question.query.filter_by(source_file=filename).first()
                if file_exists:
                    # print(f"Skipping {filename} (already in database).")
                    continue
                
                print(f"Processing new file: {filename}...")
                file_path = os.path.join(data_dir, filename)
                try:
                    groups_data = parse_cet_pdf_v2(file_path)
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
                                
                                # Update options (Clear and re-add)
                                Option.query.filter_by(question_id=existing_q.id).delete()
                                for opt_data in q_data['options']:
                                    new_opt = Option(
                                        question_id=existing_q.id,
                                        label=opt_data['label'],
                                        text=opt_data['text']
                                    )
                                    db.session.add(new_opt)
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
                except Exception as e:
                    print(f"Error parsing {filename}: {e}")
        db.session.commit()
        print("Database sync complete.")

@app.route('/')
def index():
    # Get unique source files and sort them by year descending
    files = db.session.query(QuestionGroup.source_file).distinct().all()
    file_list = sorted([f[0] for f in files], reverse=True)
    
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
    
    return render_template('index.html', files=file_list, stats=stats)

@app.route('/practice/<filename>')
def practice(filename):
    # Get all groups for this file
    groups = QuestionGroup.query.filter_by(source_file=filename).all()
    
    # Sort groups by the minimum question number in each group
    # This ensures Section A (26-35) comes before Section B (36-45), etc.
    groups.sort(key=lambda g: min([q.q_number for q in g.questions]) if g.questions else 999)
    
    return render_template('practice.html', groups=groups, filename=filename, mode='normal')

@app.route('/random_practice')
def random_practice():
    import random
    # Randomly pick 5 groups from all available groups
    all_groups = QuestionGroup.query.all()
    if not all_groups:
        return redirect(url_for('index'))
    
    sample_size = min(len(all_groups), 5)
    random_groups = random.sample(all_groups, sample_size)
    
    return render_template('practice.html', groups=random_groups, filename="随机刷题模式", mode='random')

@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    data = request.json
    q_id = data.get('question_id')
    user_answer = data.get('answer').strip().upper()
    
    question = Question.query.get(q_id)
    if not question:
        return jsonify({'error': 'Question not found'}), 404
    
    is_correct = (user_answer == question.correct_answer.upper()) if question.correct_answer else False
    
    history = UserHistory(
        question_id=q_id,
        user_answer=user_answer,
        is_correct=is_correct
    )
    db.session.add(history)
    
    if not is_correct and question.correct_answer:
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
    data = request.json
    q_id = data.get('question_id')
    answer = data.get('answer').strip().upper()
    
    question = Question.query.get(q_id)
    if question:
        question.correct_answer = answer
        db.session.commit()
        return jsonify({'status': 'success'})
    return jsonify({'error': 'Not found'}), 404

@app.route('/batch_set_answers', methods=['POST'])
def batch_set_answers():
    data = request.json
    filename = data.get('filename')
    answers = data.get('answers')
    
    if not filename or not answers:
        return jsonify({'error': 'Missing data'}), 400
        
    updated_count = 0
    not_found = []
    
    # Debug: log the received filename
    print(f"Batch setting answers for: {filename}")
    
    for item in answers:
        q_num = item.get('q_number')
        ans = item.get('answer').strip().upper() if item.get('answer') else None
        expl = item.get('explanation')
        
        # Use filter and update to be more direct
        question = Question.query.filter_by(source_file=filename, q_number=q_num).first()
        if question:
            if ans:
                question.correct_answer = ans
            if expl:
                question.explanation = expl
            updated_count += 1
        else:
            not_found.append(q_num)
            
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
    
    return render_template('wrong_questions.html', groups=wrong_groups, wrong_q_ids=wrong_q_ids)

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
