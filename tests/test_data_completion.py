from complete_answer_keys import explicit_answer, normalize_evidence
from answers import parse_answer_key


def test_exclusion_is_not_a_positive_verdict():
    result, _ = explicit_answer('这与C表述一致，故选C。故选项B可排除。', dict.fromkeys('ABCD', ''))
    assert result == 'C'
    assert explicit_answer('故选项B可排除。', dict.fromkeys('ABCD', ''))[0] is None


def test_conflicting_merged_explanations_are_rejected():
    assert explicit_answer('故答案为A。第二题答案为B。', dict.fromkeys('ABCD', ''))[0] is None


def test_cloze_uses_word_not_ocr_letter():
    assert explicit_answer('D）desperate，故答案为D。', {'A': 'roughly', 'I': 'desperate'}, True)[0] == 'I'
    assert explicit_answer('roughly和desperate均为选项', {'A': 'roughly', 'I': 'desperate'}, True)[0] is None


def test_normalization_only_repairs_numbered_headings():
    assert normalize_evidence('4l.【定位】故答\n案为C。') == '41.【定位】故答案为C。'
    assert normalize_evidence('I) First paragraph') == 'I) First paragraph'


def test_time_and_percentage_do_not_create_false_questions():
    key = parse_answer_key('答案详解\n1. What happened?\n9.30左右发生事故。\n50.5%受到影响，故答案为B。\n2. Why?\n故答案为C。')
    assert set(key) == {1, 2}
    assert key[1]['answer'] == 'B'
    assert key[2]['answer'] == 'C'
