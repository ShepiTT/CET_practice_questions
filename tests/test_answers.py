import answers

KEY = """Part II Listening Comprehension
News Report One
·听力原文·
Six people had to move away from their home.
·答案详解·
1. Why did the six residents have to find another place to stay?
A）They lost their jobs. C）The building was sold.
B）Their apartments were damaged. D）They were evicted.
由此可知，选项B为正确答案。其余三个选项新闻中并未提及，可排除。
2. What is the fire marshal doing?
在对话中，男士提到他们的截止日期快到了，所以答案为D项。
News Report Two
·听力原文·
Second script.
·答案详解·
3. What have past studies found?
A）Option one. C）Option three.
故选项C为正确答案。选项A具有一定的迷惑性。
A）This is question four's option line, header lost by OCR.
B）Whatever. D）Whatever.
选项A与文章内容一致，因此为正确答案。
5. What did the researchers find?
故本题选D。
Part III Reading Comprehension
·答案详解·
26.D）detailed（adj.详尽的）
语法判断空格处应填入形容词。
27，M)required（v.需要）
28.L)）partly（adv.部分地）
31.Dinvolves（v.包含）
·答案详解·
36.题干译文有些人把在工作场所的社交视为机会。
答案解析E。由题干关键信息定位到E段。
37题干译文尽管生产力不断提高。
客案解析F。由题干关键信息定位到F段。
38. 题干译文另一句。
tokeep their living standards与原文中的同义，故答案为H。
·答案详解·
46. What does the author say?
根据题干关键词可以定位到第二段。这与选项C表达的意思一致，故C正确。选项D中出现了原文词汇，故排除。
47. What can we infer?
选项B与文章内容一致，因此为正确答案。选项D与文章内容相反，故排除D。
48. Something ambiguous?
选项A为正确答案。选项B为正确答案。
"""


def test_parse_answer_key_reads_every_section_style():
    key = answers.parse_answer_key(KEY)
    got = {n: v['answer'] for n, v in key.items()}
    assert got == {1: 'B', 2: 'D', 3: 'C', 4: 'A', 5: 'D',
                   26: 'D', 27: 'M', 28: 'L', 31: 'D',
                   36: 'E', 37: 'F', 38: 'H',
                   46: 'C', 47: 'B', 48: None}


def test_explanations_keep_the_whole_block():
    key = answers.parse_answer_key(KEY)
    assert key[1]['explanation'].startswith('Why did the six residents')
    assert '选项B为正确答案' in key[1]['explanation']
    assert 'What is the fire marshal' not in key[1]['explanation']


def test_merged_block_is_split_at_second_option_line():
    key = answers.parse_answer_key(KEY)
    assert key[4]['explanation'].startswith('A）This is question four')
    assert 'question four' not in key[3]['explanation']


def test_transcripts_need_all_eight_blocks():
    assert answers.parse_transcripts(KEY) == {}
    eight = '\n'.join(f'Block {i}\n·听力原文·\nScript number {i}.\n·答案详解·\n{i}. Q?' for i in range(8))
    scripts = answers.parse_transcripts(eight)
    assert list(scripts) == answers.LISTENING_GROUPS
    assert scripts[(1, 2)] == 'Script number 0.'
    assert scripts[(22, 25)] == 'Script number 7.'
