from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import re

db = SQLAlchemy()

class QuestionGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_file = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(200)) # e.g., "Questions 16 to 18"
    passage = db.Column(db.Text) # The passage or paragraph text
    group_type = db.Column(db.String(50)) # listening, cloze, matching, reading
    audio_start = db.Column(db.Float) # listening only: seconds into the paper's MP3 where this group starts
    transcript = db.Column(db.Text) # listening only: the recording's script, from the answer key
    questions = db.relationship('Question', backref='group', lazy=True, cascade="all, delete-orphan")

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('question_group.id'))
    source_file = db.Column(db.String(100), nullable=False)
    section = db.Column(db.String(100))
    content = db.Column(db.Text, nullable=False)
    q_number = db.Column(db.Integer)
    correct_answer = db.Column(db.String(10)) # A, B, C... or Paragraph Letter
    explanation = db.Column(db.Text) # New field for detailed explanation
    options = db.relationship('Option', backref='question', lazy=True, cascade="all, delete-orphan",
                              order_by='Option.label')

    @property
    def allowed_answers(self):
        if self.group and self.group.group_type == 'matching':
            labels = set(re.findall(r'(?:^|\s)[\[（(]?([A-Z])\s*[)）\]]', self.group.passage or ''))
            # Exam paragraphs run consecutively from A. OCR sometimes drops an
            # internal marker (especially I); do not reject its valid answers.
            last = max(labels) if labels else 'O'
            return [chr(code) for code in range(ord('A'), ord(last) + 1)]
        return sorted({option.label for option in self.options})

    def to_dict(self):
        return {
            'id': self.id,
            'q_number': self.q_number,
            'content': self.content,
            'correct_answer': self.correct_answer,
            'explanation': self.explanation,
            'options': [opt.to_dict() for opt in self.options]
        }

class Option(db.Model):
    # One row per label per question; the word bank / A-D choices must never repeat.
    __table_args__ = (db.UniqueConstraint('question_id', 'label', name='uq_option_question_label'),)
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('question.id'), nullable=False)
    label = db.Column(db.String(5), nullable=False) # A, B, C...
    text = db.Column(db.Text, nullable=False)

    def to_dict(self):
        return {
            'label': self.label,
            'text': self.text
        }

class UserHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('question.id'), nullable=False)
    user_answer = db.Column(db.String(10))
    is_correct = db.Column(db.Boolean)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class WrongQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('question.id'), unique=True, nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    question = db.relationship('Question', backref='wrong_entry')
