from app import app, db
from models import Question, QuestionGroup

with app.app_context():
    # Use db directly
    files = [f[0] for f in db.session.query(Question.source_file).distinct().all()]
    print(f"Total files in DB: {len(files)}")
    
    for f in files:
        q_nums = [q.q_number for q in Question.query.filter_by(source_file=f).filter(Question.q_number >= 26, Question.q_number <= 35).all()]
        if len(q_nums) < 10:
            print(f"File {f} is MISSING cloze questions! Found only: {sorted(q_nums)}")
        else:
            print(f"File {f} has all 10 cloze questions.")
