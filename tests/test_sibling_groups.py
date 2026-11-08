"""Tests for sibling group content reuse logic in app.py."""
import pytest

from app import (
    app, db, parse_paper_filename, find_sibling_paper,
    clone_group_from_donor, fill_missing_groups, import_paper,
)
from models import QuestionGroup, Question, Option


class TestParsePaperFilename:
    def test_standard_paper(self):
        assert parse_paper_filename('2024-12-CET4-1.pdf') == ('2024', '12', 'CET4', '1')
        assert parse_paper_filename('2019-06-CET6-3.pdf') == ('2019', '06', 'CET6', '3')

    def test_merged_paper(self):
        assert parse_paper_filename('2022-09-CET4-2-3.pdf') == ('2022', '09', 'CET4', '2-3')
        assert parse_paper_filename('2023-03-CET6-2-3.pdf') == ('2023', '03', 'CET6', '2-3')

    def test_invalid(self):
        assert parse_paper_filename('invalid.pdf') is None
        assert parse_paper_filename('2024-12-CET5-1.pdf') is None
        assert parse_paper_filename('') is None
        assert parse_paper_filename('2024-12-CET4-1.txt') is None


class TestFindSiblingPaper:
    def test_set3_prefers_set2(self):
        available = {'2024-12-CET4-1.pdf', '2024-12-CET4-2.pdf'}
        assert find_sibling_paper('2024-12-CET4-3.pdf', available) == '2024-12-CET4-2.pdf'

    def test_set3_falls_back_to_set1(self):
        available = {'2024-12-CET4-1.pdf'}
        assert find_sibling_paper('2024-12-CET4-3.pdf', available) == '2024-12-CET4-1.pdf'

    def test_set3_no_sibling(self):
        available = set()
        assert find_sibling_paper('2024-12-CET4-3.pdf', available) is None

    def test_merged_uses_set1(self):
        available = {'2022-09-CET4-1.pdf', '2022-09-CET4-2.pdf'}
        assert find_sibling_paper('2022-09-CET4-2-3.pdf', available) == '2022-09-CET4-1.pdf'

    def test_merged_no_set1(self):
        available = {'2022-09-CET4-2.pdf'}
        assert find_sibling_paper('2022-09-CET4-2-3.pdf', available) is None

    def test_set2_falls_back_to_set1(self):
        available = {'2024-12-CET4-1.pdf'}
        assert find_sibling_paper('2024-12-CET4-2.pdf', available) == '2024-12-CET4-1.pdf'

    def test_set1_has_no_donor(self):
        available = {'2024-12-CET4-2.pdf', '2024-12-CET4-3.pdf'}
        assert find_sibling_paper('2024-12-CET4-1.pdf', available) is None

    def test_different_session_not_matched(self):
        available = {'2024-06-CET4-2.pdf'}
        assert find_sibling_paper('2024-12-CET4-3.pdf', available) is None

    def test_different_level_not_matched(self):
        available = {'2024-12-CET6-2.pdf'}
        assert find_sibling_paper('2024-12-CET4-3.pdf', available) is None


class TestCloneGroupFromDonor:
    @pytest.fixture
    def donor_group(self, client):
        """Create a donor group with questions and options."""
        group = QuestionGroup(
            source_file='2024-12-CET4-1.pdf',
            title='Listening: Questions 1 to 3',
            passage='Audio context required.',
            group_type='listening',
            audio_start=10.5,
            transcript='This is the transcript.',
        )
        db.session.add(group)
        db.session.flush()

        for q_num in range(1, 4):
            q = Question(
                group_id=group.id,
                source_file='2024-12-CET4-1.pdf',
                section='Listening',
                q_number=q_num,
                content=f'Question {q_num}?',
                correct_answer='A',
                explanation=f'Explanation for Q{q_num}',
            )
            db.session.add(q)
            db.session.flush()

            for label in 'ABCD':
                opt = Option(
                    question_id=q.id,
                    label=label,
                    text=f'Option {label}',
                )
                db.session.add(opt)

        db.session.commit()
        return group

    def test_clones_group_structure(self, client, donor_group):
        cloned = clone_group_from_donor(donor_group, '2024-12-CET4-3.pdf')
        db.session.commit()

        assert cloned.source_file == '2024-12-CET4-3.pdf'
        assert cloned.title == donor_group.title
        assert cloned.passage == donor_group.passage
        assert cloned.group_type == donor_group.group_type
        assert cloned.audio_start == donor_group.audio_start
        assert cloned.transcript == donor_group.transcript
        assert cloned.id != donor_group.id

    def test_clones_questions(self, client, donor_group):
        cloned = clone_group_from_donor(donor_group, '2024-12-CET4-3.pdf')
        db.session.commit()

        assert len(cloned.questions) == 3
        for q in cloned.questions:
            assert q.source_file == '2024-12-CET4-3.pdf'
            assert q.group_id == cloned.id
            assert q.correct_answer == 'A'

    def test_clones_options(self, client, donor_group):
        cloned = clone_group_from_donor(donor_group, '2024-12-CET4-3.pdf')
        db.session.commit()

        for q in cloned.questions:
            assert len(q.options) == 4
            labels = {opt.label for opt in q.options}
            assert labels == {'A', 'B', 'C', 'D'}


class TestFillMissingGroups:
    def make_group(self, source_file, group_type, with_questions=True):
        """Helper to create a group with optional questions."""
        group = QuestionGroup(
            source_file=source_file,
            title=f'{group_type} group',
            passage='Test passage',
            group_type=group_type,
        )
        db.session.add(group)
        db.session.flush()

        if with_questions:
            q = Question(
                group_id=group.id,
                source_file=source_file,
                section='Test',
                q_number=1,
                content='Test question?',
            )
            db.session.add(q)
            db.session.flush()

            for label in 'ABCD':
                opt = Option(question_id=q.id, label=label, text=f'Option {label}')
                db.session.add(opt)

        db.session.commit()
        return group

    def test_fills_missing_listening(self, client):
        self.make_group('2024-12-CET4-1.pdf', 'listening')
        self.make_group('2024-12-CET4-1.pdf', 'reading')
        self.make_group('2024-12-CET4-3.pdf', 'reading')

        cloned = fill_missing_groups()
        db.session.commit()

        assert ('2024-12-CET4-3.pdf', 'listening', '2024-12-CET4-1.pdf') in cloned

        groups = QuestionGroup.query.filter_by(
            source_file='2024-12-CET4-3.pdf', group_type='listening'
        ).all()
        assert len(groups) == 1

    def test_fills_multiple_missing_types(self, client):
        for gtype in ('listening', 'cloze', 'matching', 'reading'):
            self.make_group('2024-12-CET4-1.pdf', gtype)
        self.make_group('2024-12-CET4-3.pdf', 'reading')

        cloned = fill_missing_groups()
        db.session.commit()

        cloned_types = {c[1] for c in cloned if c[0] == '2024-12-CET4-3.pdf'}
        assert 'listening' in cloned_types
        assert 'cloze' in cloned_types
        assert 'matching' in cloned_types
        assert 'reading' not in cloned_types

    def test_does_not_overwrite_existing_groups(self, client):
        self.make_group('2024-12-CET4-1.pdf', 'listening')
        original = self.make_group('2024-12-CET4-3.pdf', 'listening')
        self.make_group('2024-12-CET4-3.pdf', 'reading')

        cloned = fill_missing_groups()
        db.session.commit()

        assert not any(c[1] == 'listening' for c in cloned if c[0] == '2024-12-CET4-3.pdf')

        groups = QuestionGroup.query.filter_by(
            source_file='2024-12-CET4-3.pdf', group_type='listening'
        ).all()
        assert len(groups) == 1
        assert groups[0].id == original.id

    def test_prefers_set2_for_set3(self, client):
        self.make_group('2024-12-CET4-1.pdf', 'listening')
        self.make_group('2024-12-CET4-2.pdf', 'listening')
        self.make_group('2024-12-CET4-3.pdf', 'reading')

        cloned = fill_missing_groups()
        db.session.commit()

        listening_clone = [c for c in cloned if c[0] == '2024-12-CET4-3.pdf' and c[1] == 'listening']
        assert len(listening_clone) == 1
        assert listening_clone[0][2] == '2024-12-CET4-2.pdf'

    def test_merged_paper_uses_set1(self, client):
        self.make_group('2022-09-CET4-1.pdf', 'listening')
        self.make_group('2022-09-CET4-1.pdf', 'cloze')
        self.make_group('2022-09-CET4-2-3.pdf', 'reading')

        cloned = fill_missing_groups()
        db.session.commit()

        cloned_from = {(c[0], c[1]): c[2] for c in cloned}
        assert cloned_from.get(('2022-09-CET4-2-3.pdf', 'listening')) == '2022-09-CET4-1.pdf'
        assert cloned_from.get(('2022-09-CET4-2-3.pdf', 'cloze')) == '2022-09-CET4-1.pdf'

    def test_skips_donor_without_questions(self, client):
        self.make_group('2024-12-CET4-1.pdf', 'listening', with_questions=False)
        self.make_group('2024-12-CET4-3.pdf', 'reading')

        cloned = fill_missing_groups()
        db.session.commit()

        assert not any(c[1] == 'listening' for c in cloned)

    def test_no_sibling_available(self, client):
        self.make_group('2024-12-CET4-1.pdf', 'listening')

        cloned = fill_missing_groups()

        assert cloned == []

    def test_all_groups_present(self, client):
        for gtype in ('listening', 'cloze', 'matching', 'reading'):
            self.make_group('2024-12-CET4-1.pdf', gtype)
            self.make_group('2024-12-CET4-3.pdf', gtype)

        cloned = fill_missing_groups()

        assert cloned == []
