"""Content and HTML-safety regressions for the practice-page text formatter.

The database samples were read with SQLite mode=ro on 2026-11-08 and frozen
here. Tests never open or mutate the user's question/history database.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'static' / 'practice-text.js'
NODE = shutil.which('node')

# Current DB: 2025-12-CET4-3.pdf, question id 5242, question number 1.
EGG_PRICE_EXPLANATION = """What do we learn from the news report about the egg price in the U.S.?
关于美国的鸡蛋价格，我们从这则新闻报道中了解到什么？
C)它因禽流感而上涨。
A）它已急剧上涨。
D）它一直是消费者关心的问题
B）它一直在波动。
新闻开头提到，美国一打大号鸡蛋的平均价格已达到4.25美元，这一价格是2023年12月价格（1.78美元）
的两倍多。这直接表明美国的鸡蛋价格急剧上涨，因此选项A为正确答案。虽然新闻中提及从2012年起美
国因禽流感风险而禁止从墨西哥进口鸡蛋，但并未明确指出禽流感是美国鸡蛋价格上涨的直接原因，故C
项不准确。B项和D项新闻中未提及，可排除。"""

# Current DB: group id 1099. S4.25/S1.78/s300 deliberately remain: a layout
# formatter must not invent OCR corrections to currencies or source words.
EGG_PRICE_TRANSCRIPT = """Customs and Border Protection officials are seeing a rise in people illegally bringing eggs from Mexico into
border states such as Texas. [1] The U.S. average price for a dozen large eggs last month hit S4.25. That's more than
double their S1.78 price in December 2023. Meanwhile, 30 eggs in Mexico sell for just $3.40. Those bringing eggs into
the U.S. risk fines of up to $10,000. But officials say they usually seize and destroy the eggs. They also impose a s300
penalty.
The Department of Agriculture banned eggs from Mexico in 2012. This was due to the risk of bird flu. Officials
said most people arriving at international borders with eggs are truthful about their purchases. [2] They simply don't
know bringing eggs is not allowed. *Generally, the items are declared during the first inspection. Then the person can
abandon the eggs without consequence,\" a Customs and Border Protection spokesperson said. Even so, there have been
a very small number of cases recently when authorities discovered eggs during further inspections."""

# Current DB: 2025-12-CET4-3.pdf, question id 667, question number 26.
EARTH_DAY_EXPLANATION = """M)rest（n.剩余部分）
语法判断空格前面是定冠词the，后面是介词短语oftheworld，故空格处应填名词。
语义判断句意为“美国在4月22日庆祝世界地球日，而世界
地区则在4月22日或春分当天庆祝”the
restoftheworld是固定搭配，表示“世界其他地区”，符合此处语境，故答案为M。"""

# An inert DOM makes unsafe HTML insertion fail explicitly. It supports normal
# createElement/textContent operations without interpreting source strings.
NODE_HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const formatter = require(process.argv[1]);
if (typeof formatter.blocks !== 'function' || typeof formatter.render !== 'function') {
  throw new Error('practice-text.js must export CETText.blocks and CETText.render');
}
if (input.operation !== 'render') {
  process.stdout.write(JSON.stringify(formatter.blocks(input.raw, input.kind)));
} else {
  const created = [];
  class TextNode {
    constructor(value) { this.nodeType = 3; this.data = String(value); }
    get textContent() { return this.data; }
    set textContent(value) { this.data = String(value); }
  }
  class Element {
    constructor(tag) {
      this.nodeType = tag === '#fragment' ? 11 : 1;
      this.tagName = tag.toUpperCase();
      this.children = []; this.attributes = {}; this.dataset = {}; this.style = {};
      this.className = ''; this._text = '';
      const classes = new Set();
      this.classList = {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name),
        toggle: (name, enabled) => {
          const next = enabled === undefined ? !classes.has(name) : enabled;
          next ? classes.add(name) : classes.delete(name); return next;
        }
      };
    }
    get ownerDocument() { return globalThis.document; }
    get childNodes() { return this.children; }
    get firstChild() { return this.children[0] || null; }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; }
    set innerHTML(value) {
      if (value !== '') throw new Error('Unsafe nonempty innerHTML insertion');
      this._text = ''; this.children = [];
    }
    set outerHTML(value) { throw new Error('Unsafe outerHTML insertion'); }
    insertAdjacentHTML() { throw new Error('Unsafe insertAdjacentHTML insertion'); }
    appendChild(node) {
      if (node.nodeType === 11) this.children.push(...node.children);
      else this.children.push(node);
      return node;
    }
    append(...nodes) { nodes.forEach(node => this.appendChild(typeof node === 'string' ? new TextNode(node) : node)); }
    replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
    removeChild(node) { this.children.splice(this.children.indexOf(node), 1); return node; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
  }
  globalThis.document = {
    createElement: tag => { created.push(tag.toLowerCase()); return new Element(tag); },
    createElementNS: (_namespace, tag) => { created.push(tag.toLowerCase()); return new Element(tag); },
    createTextNode: value => new TextNode(value),
    createDocumentFragment: () => new Element('#fragment')
  };
  globalThis.__rawSourceExecuted = false;
  const container = new Element('div');
  const result = formatter.render(container, input.raw, {kind: input.kind});
  function serialize(node) {
    if (node.nodeType === 3) return {type: 'text', text: node.textContent};
    return {type: node.tagName, text: node._text, attributes: node.attributes,
            children: node.children.map(serialize)};
  }
  process.stdout.write(JSON.stringify({blocks: result, tree: serialize(container),
    visibleText: container.textContent, createdTags: created,
    rawSourceExecuted: globalThis.__rawSourceExecuted}));
}
"""


@pytest.fixture(scope='module')
def format_text():
    if NODE is None:
        pytest.skip('Node.js is needed for the browser formatter regression tests')
    assert MODULE.is_file(), 'The practice page text formatter is missing'

    def call(raw, kind='explanation', operation='blocks'):
        result = subprocess.run(
            [NODE, '-e', NODE_HARNESS, str(MODULE)],
            input=json.dumps({'raw': raw, 'kind': kind, 'operation': operation}, ensure_ascii=False),
            capture_output=True, text=True, encoding='utf-8', timeout=15,
            env={**os.environ, 'NODE_NO_WARNINGS': '1'}, check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return call


def compact(value):
    return re.sub(r'\s+', '', value)


def plain_stream(blocks):
    """Read display order while retaining option labels and dialogue speakers."""
    pieces = []
    for block in blocks:
        if block['type'] == 'options':
            pieces.extend(item['label'] + item['text'] for item in block['items'])
        else:
            pieces.append(block.get('speaker', '') + block.get('label', '') + block.get('text', ''))
    return '\n'.join(pieces)


def labelled_items(blocks):
    items = []
    for block in blocks:
        if block['type'] == 'options':
            items.extend(block['items'])
        elif block.get('label'):
            items.append(block)
    return items


def tree_nodes(node):
    yield node
    for child in node.get('children', []):
        yield from tree_nodes(child)


def test_real_q1_complete_option_set_is_ordered_without_changing_meaning(format_text):
    blocks = format_text(EGG_PRICE_EXPLANATION)
    choices = [block for block in blocks if block['type'] == 'options']
    assert len(choices) == 1
    items = choices[0]['items']
    assert [item['label'][0] for item in items] == list('ABCD')
    assert [item['text'] for item in items] == [
        '它已急剧上涨。', '它一直在波动。', '它因禽流感而上涨。', '它一直是消费者关心的问题',
    ]
    visible = compact(plain_stream(blocks))
    assert compact(EGG_PRICE_EXPLANATION.split('\nC)', 1)[0]) in visible
    assert compact(EGG_PRICE_EXPLANATION.split('新闻开头提到', 1)[1]) in visible
    assert '4.25美元' in visible and '1.78美元' in visible and '2023年12月' in visible


def test_real_egg_report_repairs_hard_wraps_without_rewriting_amounts(format_text):
    blocks = format_text(EGG_PRICE_TRANSCRIPT, 'transcript')
    assert compact(plain_stream(blocks)) == compact(EGG_PRICE_TRANSCRIPT)
    paragraphs = [block.get('text', '') for block in blocks]
    assert any('Mexico into border states' in paragraph for paragraph in paragraphs)
    assert any('s300 penalty.' in paragraph for paragraph in paragraphs)
    assert all('\n' not in value for value in paragraphs)
    visible = plain_stream(blocks)
    for literal in ['S4.25', 'S1.78', '$3.40', '$10,000', 's300', '30 eggs', '2023', '2012', '[1]', '[2]']:
        assert visible.count(literal) == EGG_PRICE_TRANSCRIPT.count(literal)


def test_real_q26_keeps_single_word_bank_label_and_chinese_reasoning(format_text):
    blocks = format_text(EARTH_DAY_EXPLANATION)
    assert compact(plain_stream(blocks)) == compact(EARTH_DAY_EXPLANATION)
    assert [item['label'][0] for item in labelled_items(blocks)] == ['M']
    assert not any(block['type'] == 'options' for block in blocks)
    visible = plain_stream(blocks)
    assert 'rest（n.剩余部分）' in visible
    assert '语法判断' in visible and '语义判断' in visible and 'restoftheworld' in visible
    assert compact(visible).count('4月22日') == 2


def test_incomplete_or_repeated_options_are_never_reordered(format_text):
    incomplete = 'C) Third choice\nA) First choice\nOnly these two choices were present.'
    blocks = format_text(incomplete)
    assert [item['label'][0] for item in labelled_items(blocks)] == ['C', 'A']
    assert compact(plain_stream(blocks)) == compact(incomplete)
    repeated = 'C) Third\nA) First\nD) Fourth\nB) Second\nA) Another A'
    blocks = format_text(repeated)
    assert [item['label'][0] for item in labelled_items(blocks)] == list('CADBA')
    assert compact(plain_stream(blocks)) == compact(repeated)


def test_separate_complete_choice_sets_are_sorted_within_their_own_group(format_text):
    raw = ('C) First C\nA) First A\nD) First D\nB) First B\n\n'
           'D) Second D\nB) Second B\nA) Second A\nC) Second C')
    choices = [block for block in format_text(raw) if block['type'] == 'options']
    assert len(choices) == 2
    assert [item['text'] for item in choices[0]['items']] == ['First A', 'First B', 'First C', 'First D']
    assert [item['text'] for item in choices[1]['items']] == ['Second A', 'Second B', 'Second C', 'Second D']


def test_explanation_sections_remain_distinct_and_keep_their_text(format_text):
    raw = '【定位】第2段最后一句。\n【解析】这是没有结束的文\n字，价值为1.78美元。\n【干扰项】B项说法不同。'
    blocks = format_text(raw)
    assert compact(plain_stream(blocks)) == compact(raw)
    assert [block['text'] for block in blocks if block['type'] == 'heading'] == [
        '【定位】', '【解析】', '【干扰项】',
    ]
    assert any('文字，价值为1.78美元。' in block.get('text', '') for block in blocks)


def test_dialogue_preserves_speakers_and_joins_only_their_wrapped_lines(format_text):
    raw = ('M: Did the well-known co-author pay $12.50 for the\nnon-fiction book?\n'
           'W： Yes. We discussed long-term plans in\n2025.\n'
           'M: Then keep the 20% receipt.')
    blocks = format_text(raw, 'transcript')
    dialogue = [block for block in blocks if block['type'] == 'dialogue']
    assert [block['speaker'] for block in dialogue] == ['M:', 'W：', 'M:']
    assert compact(plain_stream(blocks)) == compact(raw)
    assert dialogue[0]['text'] == 'Did the well-known co-author pay $12.50 for the non-fiction book?'
    assert dialogue[1]['text'] == 'Yes. We discussed long-term plans in 2025.'
    assert dialogue[2]['text'] == 'Then keep the 20% receipt.'


def test_inline_letters_and_decimal_numbers_are_not_option_boundaries(format_text):
    raw = ('In 2025, plan A) costs $1.78 while plan B) costs $4.25.\n'
           'The well-known non-fiction series has 30 parts; C) and D) occur within this sentence.')
    blocks = format_text(raw, 'passage')
    assert not labelled_items(blocks)
    assert compact(plain_stream(blocks)) == compact(raw)
    assert any('In 2025, plan A) costs $1.78 while plan B) costs $4.25.' in block.get('text', '') for block in blocks)


def test_narrative_paragraphs_keep_abbreviations_with_their_continuation(format_text):
    raw = 'The U.S.\nGovernment invited Dr.\nSmith to speak.\nThe second event cost $4.25.'
    blocks = format_text(raw, 'transcript')
    assert [block['text'] for block in blocks] == [
        'The U.S. Government invited Dr. Smith to speak.',
        'The second event cost $4.25.',
    ]
    assert compact(plain_stream(blocks)) == compact(raw)


def test_single_line_matching_passage_exposes_each_labelled_paragraph(format_text):
    raw = 'Directions: Match each statement. A) First paragraph. B) Second paragraph. C) Third paragraph.'
    blocks = format_text(raw, 'passage')
    assert [item['label'] for item in labelled_items(blocks)] == ['A)', 'B)', 'C)']
    assert blocks[0]['text'] == 'Directions: Match each statement.'
    assert compact(plain_stream(blocks)) == compact(raw)


@pytest.mark.parametrize('kind', ['explanation', 'transcript', 'passage'])
def test_render_treats_raw_html_and_event_attributes_as_visible_text(format_text, kind):
    raw = ('Literal <script>globalThis.__rawSourceExecuted=true</script> '
           '<img src=x onerror="globalThis.__rawSourceExecuted=true"> '
           '& <b>quoted source</b> "double quotes" and \'single quotes\'.')
    result = format_text(raw, kind, 'render')
    assert result['rawSourceExecuted'] is False
    assert compact(result['visibleText']) == compact(raw)
    assert not {'script', 'img', 'b'} & set(result['createdTags'])
    for node in tree_nodes(result['tree']):
        assert not any(name.lower().startswith('on') for name in node.get('attributes', {}))


def test_render_keeps_real_sorted_choices_and_analysis_visible(format_text):
    result = format_text(EGG_PRICE_EXPLANATION, 'explanation', 'render')
    visible = compact(result['visibleText'])
    ordered = ['它已急剧上涨。', '它一直在波动。', '它因禽流感而上涨。', '它一直是消费者关心的问题']
    assert [visible.index(value) for value in ordered] == sorted(visible.index(value) for value in ordered)
    assert compact(EGG_PRICE_EXPLANATION.split('新闻开头提到', 1)[1]) in visible
    assert '4.25美元' in visible and '1.78美元' in visible


def test_render_keeps_dialogue_speakers_and_money_in_the_visible_dom(format_text):
    raw = 'M: A well-known shop asks for $12.50.\nW： I paid $10,000 in 2025.\nM: Keep the 20% receipt.'
    result = format_text(raw, 'transcript', 'render')
    assert compact(result['visibleText']) == compact(raw)
    assert result['visibleText'].count('M:') == 2
    assert 'W：' in result['visibleText']
    assert '$12.50' in result['visibleText'] and '$10,000' in result['visibleText']
