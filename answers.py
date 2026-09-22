"""Pull answers, explanations and listening transcripts out of an OCR'd 答案解析 PDF.

The CET通 answer keys are all scanned, so the input is the OCR text cached by ocr.py
(data/ocr/answers/<paper>.txt). Layout per paper:

    Part II Listening Comprehension
    News Report One
    ·听力原文·          <- transcript of that news report / conversation / passage
    ...
    ·答案详解·          <- one block per question:  "1. Why did ... ?  ... 故选项A为正确答案。"
    ...
    Part III Reading Comprehension
    Section A ... ·答案详解·   "26.D）detailed（adj. ...）"
    Section B ... ·答案详解·   "题干译文... 答案解析E。由题干关键信息..."
    Section C ... ·答案详解·   "46. What ... ? ... 选项B与文章内容一致，因此为正确答案。"

Everything is heuristic: an answer is only reported when exactly one of the known
phrasings matches, so a missing answer is preferred over a wrong one.
"""
import re

from parser import normalize_ocr

DOTTED_MARK = re.compile(r'^\s*·\s*(.+?)\s*·\s*$')      # ·答案详解· / ·听力原文· / ·概览· ...

# The keys come in two typesettings: 2023-12 onwards dot their section headings (·答案详解·),
# everything older prints them bare on their own line. Miss the bare form and a whole paper
# yields nothing. Every heading has to be recognised, not just the two we read: an
# unrecognised one does not close the preceding block, so its prose gets appended to the
# last question and can be mined for a letter.
SECTION_NAMES = (
    '答案详解', '听力原文及参考译文', '听力原文', '参考译文与难点注释', '参考译文',
    '全文翻译', '概览', '话题词汇', '同义表达', '参考范文&点评', '参考范文', '范文译文',
    '亮点词汇', '难词译注', '写作句型', '干扰项注释', '译点精析', '审题', '定位解析',
    '客观题答案速查', '真题详解', '写作思路', '拓展表达', '扫码看视频', '名师点评',
)


def section_name(line):
    """The section this line announces, or None. Tolerates OCR junk glued to the front
    ('文答案详解', '园参考译文') but not a sentence that merely ends with the words."""
    m = DOTTED_MARK.match(line)
    text = m.group(1) if m else line.strip().strip('·').strip()
    for name in SECTION_NAMES:
        if text.endswith(name) and len(text) <= len(name) + 3:
            return name
    return None
# "1. Why", "49，当我们" (Chinese comma), "13Whatis" / "37题干译文" (separator lost)
QUESTION_LINE = re.compile(r'^\s*(\d{1,2})\s*(?:[.,:、．，]|(?=[A-Z（(题]))\s*(.*)$')
OPTION_A_LINE = re.compile(r'^\s*A\s*[）)]', re.M)

# Phrasings that name the correct option, most explicit first. {L} is the letter class.
ANSWER_PHRASES = [
    # The most explicit typesetting of all (2019-12 and friends): a labelled 【答案】D line.
    # OCR renders the brackets as 【】, 〖〗, [] or () often enough to be worth allowing.
    r'[【〖\[(（]\s*答\s*案\s*(?:解\s*析)?\s*[】〗\])）]\s*({L})',
    # "D）【精析】事实细节题。…" — the letter heads the line and is immediately followed by a
    # bracketed label, which an option line ("A) Cultural bias.") never is.
    r'(?m)^\s*({L})\s*[）)】]\s*[【〖\[]\s*(?:精|解)\s*析',
    r'选项\s*({L})\s*(?:为|是|即为)\s*正确答案',
    r'答案\s*(?:为|是|应为|应该是|选)\s*({L})',
    r'正确答案\s*(?:为|是)\s*({L})',
    r'({L})\s*(?:项|选项)?\s*(?:为|是)\s*正确答案',
    r'选项\s*({L})\s*与\s*(?:文章|原文|短文)\s*(?:内容|意思)?\s*(?:一致|相符|相同|吻合)',
    r'(?:本题|此题)\s*(?:应|故)?\s*选\s*({L})',
    r'(?:故|因此|所以|应)\s*选\s*(?:项)?\s*({L})',
    r'选项\s*({L})\s*(?:正确|符合|为正确)',
    r'可知\s*选项\s*({L})',
    r'(?:与|和)\s*选项\s*({L})\s*(?:表达的)?(?:意思|内容)?\s*一致',
    r'(?:故|因此|所以)\s*({L})\s*(?:项)?\s*(?:正确|为正确)',
    r'选项\s*({L})[^A-O]{0,80}?(?:故|因此|所以)\s*(?:为|是)\s*(?:正确)?答案',
]
LETTER_FIRST = re.compile(r'^\s*([A-O])\s*[）)]*\s*(?=[a-z])')   # "26.D）detailed", "31.Dinvolves", "L)）partly"
# Older keys put the Section A letter at the head of the line UNDER the question number:
#   26.【考点】动词辨析题。
#   L）【语法判断】空格前有连词and…
# and OCR renders the bracket as ）, ) or 】.
LETTER_OWN_LINE = re.compile(r'(?m)^\s*([A-O])\s*[）)】]')
# Section B keys: "答案解析E。", "客案解析F。" (OCR for 答), "警案解J。", "案解析I："
MATCHING_KEY = re.compile(r'解析?\s*([A-O])\s*[。．.:：，,]')


def _sections(lines):
    """Yield (section name, [lines]) for every section heading, in order."""
    marker, buf = None, []
    for line in lines:
        name = section_name(line)
        if name:
            if marker is not None:
                yield marker, buf
            marker, buf = name, []
        elif marker is not None:
            buf.append(line)
    if marker is not None:
        yield marker, buf


def _split_merged(blocks):
    """OCR sometimes drops a question's header line, so its block lands inside the previous
    one. When a number is skipped and the block before it holds two 'A) ...' option lines,
    split it at the second one."""
    numbers = sorted(blocks)
    for n, following in zip(numbers, numbers[1:]):
        if following != n + 2:
            continue
        starts = [m.start() for m in OPTION_A_LINE.finditer(blocks[n])]
        if len(starts) == 2:
            blocks[n], blocks[n + 1] = blocks[n][:starts[1]].rstrip(), blocks[n][starts[1]:]
    return blocks


def _question_blocks(lines):
    """Split a ·答案详解· block into {q_number: block_text}; numbers must increase, so stray
    digits inside explanations do not start a new block."""
    blocks, current, expect = {}, None, 1
    for line in lines:
        m = QUESTION_LINE.match(line)
        # A decimal/time in prose (9.30, 50.5%) is not a question header.
        # Preserve it in the current explanation instead of opening a bogus block.
        if m and re.match(r'\d', m.group(2)):
            m = None
        n = int(m.group(1)) if m else None
        if n is not None and 1 <= n <= 55:
            if n >= expect:
                current, expect = n, n + 1
                blocks[n] = [m.group(2)]
            else:
                # A header we cannot place (a number in prose opened a bogus block earlier).
                # Dropping its text turns a wrong answer into a missing one; appending it to
                # the open block would hand this question's verdict to another number.
                current = None
        elif current is not None:
            blocks[current].append(line)
    joined = {n: '\n'.join(v).strip() for n, v in blocks.items()}
    return _split_merged(joined)


def _by_phrases(block, letters):
    for phrase in ANSWER_PHRASES:
        found = {m.group(1) for m in re.finditer(phrase.replace('{L}', letters), block)}
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            return None
    return None


def answer_in(block, q_number):
    """The option letter this block names, or None when nothing unambiguous is found."""
    if 26 <= q_number <= 35:
        m = LETTER_FIRST.match(block)          # newer keys: "26.D）detailed"
        if m:
            return m.group(1)
        m = LETTER_OWN_LINE.search(block)      # older keys: the letter heads the next line
        if m:
            return m.group(1)
        return _by_phrases(block, '[A-O]')
    if 36 <= q_number <= 45:
        found = {m.group(1) for m in MATCHING_KEY.finditer(block)}
        if len(found) == 1:
            return found.pop()
        if found:
            return None
        m = LETTER_FIRST.match(block)
        return m.group(1) if m else _by_phrases(block, '[A-O]')
    return _by_phrases(block, '[A-D]')


def parse_answer_key(text):
    """{q_number: {'answer': 'B' or None, 'explanation': str}} for one paper's OCR text."""
    lines = normalize_ocr(text).split('\n')
    bodies = [body for marker, body in _sections(lines) if '答案详解' in marker]
    # Some keys (2023-03) print no section headings at all and label each answer inline
    # with 【答案解析】X. With nothing to split on, read the whole document as one body.
    if not bodies:
        bodies = [lines]
    result = {}
    for body in bodies:
        for n, block in _question_blocks(body).items():
            if n in result:      # a later section re-using a number (OCR noise): keep the first
                continue
            result[n] = {'answer': answer_in(block, n), 'explanation': block}
    return result


LISTENING_GROUPS = [(1, 2), (3, 4), (5, 7), (8, 11), (12, 15), (16, 18), (19, 21), (22, 25)]


def listening_blocks(text):
    """Older books bind transcripts to explicit question ranges, not section labels."""
    heading = re.compile(r'Questions\s+(\d{1,2})\s*(?:and|to|-)\s*(\d{1,2})\s+are\s+based[^\n]*', re.I)
    matches = list(heading.finditer(text))
    for index, match in enumerate(matches):
        start, end = int(match[1]), int(match[2])
        if not 1 <= start <= end <= 25:
            continue
        stop = matches[index+1].start() if index+1 < len(matches) else len(text)
        body = text[match.end():stop]
        body = re.split(r'Part\s*(?:III|Ⅲ)\s*Reading|Section\s*A\s*\nDirections.*blanks', body, maxsplit=1, flags=re.I)[0]
        yield start, end, body


def parse_transcripts(text, spans=None):
    """{(start_q, end_q): transcript} — the 听力原文 blocks matched to listening groups.

    `spans` is the paper's own listening layout, in paper order. CET-4 hears 3 news reports,
    2 conversations and 3 passages; CET-6 hears 2 conversations, 2 passages and 3 recordings
    — a different number of groups — so the caller passes what the paper actually has and
    LISTENING_GROUPS is only the CET-4 default.

    Returns {} when the counts do not line up: without a 1:1 match we cannot tell which
    recording belongs to which group, and a mismatched transcript is worse than none."""
    spans = list(spans) if spans is not None else LISTENING_GROUPS
    blocks = [body for marker, body in _sections(text.split('\n')) if '听力原文' in marker]
    if not spans or len(blocks) != len(spans):
        result = {}
        for start, end, body in listening_blocks(text):
            if (start, end) not in spans:
                continue
            lines = []
            for line in body.splitlines():
                if section_name(line) or re.match(r'^\s*(?:\d{1,2}|l)\s*[.．]\s*(?:[A-Z]|【)', line):
                    break
                lines.append(line)
            transcript = '\n'.join(lines).strip()
            if len(re.findall(r'[A-Za-z]{2,}', transcript)) >= 35:
                result[(start, end)] = transcript
        return result
    return {span: '\n'.join(l for l in body if l.strip()).strip()
            for span, body in zip(spans, blocks)}
