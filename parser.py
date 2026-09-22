import pdfplumber
import re
import os

import ocr

def extract_mcq_robust(q_num, text_to_search, section_name):
    # Find the start of the question: "q_num. " (very flexible, handle missing spaces like "1.A)" or "1 . A)")
    start_pattern = rf'(?:\s|^){q_num}\s*\.(?:\s*|(?=[A-D][).]))'
    start_match = re.search(start_pattern, text_to_search)
    if not start_match:
        return None
    
    # Search window: about 1500 chars, cut at whatever comes next. Everything past that
    # belongs to another question, and letting an option run into it makes one option
    # swallow the questions that follow.
    window = text_to_search[start_match.end():start_match.end() + 1500]
    boundary = re.search(
        rf'(?:\s|^){q_num + 1}\s*\.(?:\s*|(?=[A-D][).]))'
        r'|Questions\s*\d+|Section\s*[A-O]\b|Part\s*[I-V]+\b'
        r'|Passage\s*(?:One|Two|Three|I|II|III)\b|\bDirections\b',
        window, re.IGNORECASE)
    if boundary:
        window = window[:boundary.start()]

    # All four labels must be inside that window. They are NOT in alphabetical order on the
    # page: the choices are set in two columns, so the text reads "A) … C) …" then
    # "B) … D) …". Order them by position, not by letter.
    #
    # Most papers print "A)", but some print "A." — miss that and the paper yields nothing
    # at all. The period form is the looser pattern (an initial or an abbreviation can
    # imitate it), so it is only tried when the paren form fails to find all four.
    pos = None
    for bracket in (r'\)', r'[).]'):
        found = {}
        for letter in 'ABCD':
            m = re.compile(rf'(?<![A-Za-z]){letter}\s*{bracket}').search(window)
            if not m:
                break
            found[letter] = (m.start(), m.end())
        else:
            pos = found
            break
    if not pos:
        return None

    order = sorted('ABCD', key=lambda l: pos[l][0])

    # Content is between the question number and the first label on the page
    content = window[:pos[order[0]][0]].strip()
    if not content or len(content) < 2:
        content = f"Question {q_num}"

    options_dict = {}
    for i, letter in enumerate(order):
        start = pos[letter][1]
        end = pos[order[i + 1]][0] if i + 1 < len(order) else len(window)
        options_dict[letter] = window[start:end].strip()

    return {
        'q_number': q_num,
        'content': content,
        'section': section_name,
        'options': [
            {'label': 'A', 'text': options_dict.get('A', '')},
            {'label': 'B', 'text': options_dict.get('B', '')},
            {'label': 'C', 'text': options_dict.get('C', '')},
            {'label': 'D', 'text': options_dict.get('D', '')}
        ]
    }

def clean_noise(text):
    # Page furniture can sit in the middle of a sentence after PDF extraction.
    # Only remove the exact exam-header pattern, not ordinary years/numbers.
    text = re.sub(r'(?:\b\d{1,3}\s*)?[·•]\s*\d{4}\s*年\s*\d{1,2}\s*月\s*[四六]级真题\s*[（(][^()（）\n]{1,12}[)）]\s*[·•]', '', text)
    # Remove website links
    text = re.sub(r'pastpapers\.cn', '', text, flags=re.IGNORECASE)
    # Remove patterns like "·2024年6月四级真题(第一套)· 6"
    text = re.sub(r'·\d{4}年\d{1,2}月四级真题\(.*?\)\s*·\s*\d+', '', text)
    # Remove patterns like "2023 年 12 月四级真题第 1 套 第 4 页，共 9 页"
    text = re.sub(r'\d{4}\s*年\s*\d{1,2}\s*月四级真题第\s*\d+\s*套\s*第\s*\d+\s*页，共\s*\d+\s*页', '', text)
    # Remove standalone page numbers or "Page X" if they appear at the end of lines
    text = re.sub(r'\n\s*第\s*\d+\s*页.*?\n', '\n', text)
    return text

BANK_LABELS = "ABCDEFGHIJKLMNO"

def extract_word_bank(bank_text):
    """Return the Section A word bank as [{'label', 'text'}], sorted by label.

    pdfplumber / OCR output for the bank is not always clean. Seen in the real papers:
      '0) underneath'  -> zero printed instead of O
      'l)roughly'      -> lower-case L instead of I
      'ID)literary', 'OM) splitting' -> a stray letter glued to the label
      'J potential', 'Hpressure'     -> ')' dropped (and maybe the space)
      'D normal', 'Dnatural', ') properly' -> 'I)' read as a bare D or a lone ')'
    Clean 'X) word' entries win (the last one for a repeated label: anything earlier is the
    tail of the passage). Labels still missing are then recovered, in this order, from what
    is left of the text once the clean entries are removed.
    """
    text = re.sub(r'(?<![A-Za-z])0\s*([).])', r'O\1', bank_text)
    text = re.sub(r'(?<![A-Za-z])l\)', 'I)', text)

    # Most papers print "A) word"; some print "A.word". The period form is the looser
    # pattern (an initial like "U.S." can imitate it), so only fall back to it when the
    # paren form clearly did not find a bank.
    strict = re.compile(r'(?<![A-Za-z])([A-O])\)\s*([A-Za-z\-]+)')
    if len(set(m.group(1) for m in strict.finditer(text))) < 10:
        strict = re.compile(r'(?<![A-Za-z])([A-O])\s*[).]\s*([A-Za-z\-]+)')
    words, bank_start = {}, None
    for m in strict.finditer(text):
        words[m.group(1)] = m.group(2)
        if bank_start is None:
            bank_start = m.start()
    if bank_start is None:
        return []

    bank_only = text[bank_start:]
    residual = strict.sub(' ', bank_only)
    used = lambda word: word in words.values()
    missing = lambda: [label for label in BANK_LABELS if label not in words]

    # 1. a stray letter glued to the label: 'ID)literary', 'OM) splitting'
    for label in missing():
        m = re.search(rf'(?<![A-Za-z])(?:[A-Z]{label}|{label}[A-Z])\)\s*([A-Za-z\-]+)', bank_only)
        if m and not used(m.group(1)):
            words[label] = m.group(1)
    # 2. the label with its ')' dropped, maybe glued to the word: 'J potential', 'Hpressure'
    for label in missing():
        m = re.search(rf'(?<![A-Za-z]){label}\s*\)?\s*([a-z][A-Za-z\-]*)', residual)
        if m and not used(m.group(1)):
            words[label] = m.group(1)
    # 3. 'I)' read as a bare D or a lone ')': 'D normal', 'Dnatural', ') properly'
    for m in re.finditer(r'(?<![A-Za-z])(?:D|\))\s*([A-Za-z][A-Za-z\-]+)', residual):
        if missing() and not used(m.group(1)):
            words['I' if 'I' in missing() else missing()[0]] = m.group(1)
    # 4. a single label left: whatever bare 'X word' remains is it
    if len(missing()) == 1:
        m = re.search(r'(?<![A-Za-z])[A-O] ([A-Za-z][A-Za-z\-]+)', residual)
        if m and not used(m.group(1)):
            words[missing()[0]] = m.group(1)

    return [{'label': label, 'text': words[label]} for label in sorted(words)]


def pdf_text(file_path):
    with pdfplumber.open(file_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            # Increase x_tolerance to better capture spaces between words
            # Default is usually 3. Increasing it slightly helps with stuck-together text.
            text = page.extract_text(x_tolerance=2, y_tolerance=3)
            if text:
                full_text += text + "\n"
    return full_text

OCR_DIGITS = str.maketrans({'l': '1', 'I': '1', 'O': '0', 'o': '0'})

def normalize_ocr(text):
    """Undo the misreads OCR makes exactly where the parser keys on digits: 'l'/'I' for 1 and
    'O'/'o' for 0 in question numbers at line starts and in 'Questions X to Y' headings, and a
    comma or colon after a question number ('12, A)' -> '12. A)')."""
    text = re.sub(r'Questions\s+([0-9lIOo]{1,2})\s+(and|to)\s+([0-9lIOo]{1,2})\b',
                  lambda m: f"Questions {m.group(1).translate(OCR_DIGITS)} {m.group(2)} "
                            f"{m.group(3).translate(OCR_DIGITS)}", text)
    text = re.sub(r'(?m)^([ \t]*)([0-9lIOo]{1,2})[ \t]*[.,:、．][ \t]*(?=[A-Z(])',
                  lambda m: f"{m.group(1)}{m.group(2).translate(OCR_DIGITS)}. ", text)
    return text

def load_text(file_path, allow_ocr=False):
    """Text of a paper: its own text layer, else the cached OCR (or OCR now when allowed)."""
    text = pdf_text(file_path)
    if len(text.strip()) >= 500:
        return text
    text = ocr.ocr_pdf(file_path) if allow_ocr else ocr.cached_text(file_path)
    return normalize_ocr(text) if text else ''

def parse_cet_pdf_v2(file_path, allow_ocr=False):
    return parse_cet_text(load_text(file_path, allow_ocr))

def parse_cet_text(full_text):
    groups = []

    # Clean the text from noise before processing
    full_text = clean_noise(full_text)

    # Normalize text for regex: single spaces
    norm_text = re.sub(r'\s+', ' ', full_text)

    # --- 1. Careful Reading (Section C) ---
    # Very flexible: handle missing spaces like "46to50are"
    reading_groups = re.finditer(r'Questions\s*(\d+)\s*(?:to|-|~)\s*(\d+)\s*are\s*based\s*on\s*the\s*following\s*passage\.', norm_text, re.IGNORECASE)
    
    last_end = 0
    matches = list(reading_groups)
    for i, match in enumerate(matches):
        start_q = int(match.group(1))
        end_q = int(match.group(2))
        
        # The passage is between the end of the PREVIOUS match (or start of Section C) and this match
        # OR between this match and the first question number. 
        # Usually: "Questions 46 to 50 are based on... [Passage] 46. [Q] 47. [Q] ..."
        
        # Let's find where this group ends. It ends at the next group start or end of text.
        next_match_start = matches[i+1].start() if i+1 < len(matches) else len(norm_text)
        group_body = norm_text[match.end():next_match_start]
        
        # Find the first question number in group_body
        q_start_match = re.search(rf'(?:\s|^){start_q}\.\s*', group_body)
        if q_start_match:
            passage = group_body[:q_start_match.start()].strip()
            # Clean passage
            passage = re.sub(r'Passage\s*(?:One|Two|Three|I|II|III)', '', passage, flags=re.IGNORECASE).strip()
            
            group = {
                'title': f"Questions {start_q} to {end_q}",
                'passage': passage,
                'type': 'reading',
                'questions': []
            }
            
            for q_num in range(start_q, end_q + 1):
                q_data = extract_mcq_robust(q_num, group_body, "Reading Section C")
                if q_data:
                    group['questions'].append(q_data)
            
            if group['questions']:
                groups.append(group)

    # --- 2. Banked Cloze (Section A) ---
    # The bank is the tail of the Section A region. Bound that region at Section B FIRST:
    # papers that typeset the bank as "A.acknowledge" instead of "A) acknowledge" have no
    # "X)" in Section A at all, and a regex allowed to run past the boundary happily binds
    # the "bank" to Section B's paragraph letters, giving the student 15 nonsense words.
    cloze_start = re.search(r'Section\s*A.*?ten\s*blanks', norm_text, re.DOTALL | re.IGNORECASE)
    if cloze_start:
        region_end = re.search(r'Section\s*B|Part\s*IV', norm_text[cloze_start.end():], re.IGNORECASE)
        region = norm_text[cloze_start.end():
                           cloze_start.end() + (region_end.start() if region_end else 15000)]
        # Label A opens the bank; take its LAST occurrence, since the passage above may
        # contain an "A)" of its own.
        starts = [m.start() for m in re.finditer(r'(?<![A-Za-z])A\s*[).]\s*[A-Za-z]', region)]
        split_at = starts[-1] if starts else len(region)
        passage = region[:split_at].strip()
        bank_text = region[split_at:].strip()

        bank = extract_word_bank(bank_text)
            
        if bank:
            groups.append({
                'title': "Section A: Banked Cloze",
                'passage': passage,
                'type': 'cloze',
                'questions': [{'q_number': i, 'content': f"Blank {i}", 'section': "Reading Section A", 'options': bank} for i in range(26, 36)]
            })

    # --- 3. Matching (Section B) ---
    matching_matches = list(re.finditer(r'Section\s*B', norm_text, re.IGNORECASE))
    for i, m in enumerate(matching_matches):
        search_window = norm_text[m.end():m.end() + 15000]
        # Only consider Section B that contains matching instructions
        if re.search(r'ten\s*statements', search_window, re.IGNORECASE) and re.search(r'(?:\s|^)36\s*\.', search_window):
            # Find the end of this section (Section C or other major marker)
            # Use more specific markers to avoid matching words in the middle of a passage
            end_match = re.search(r'Section\s*C\s*Directions|Part\s*IV\s*Translation|Translation\s*Sheet|$', search_window, re.IGNORECASE)
            section_b_text = search_window[:end_match.start()].strip()
            
            q36_match = re.search(r'(?:\s|^)36\s*\.', section_b_text)
            if q36_match:
                passage = section_b_text[:q36_match.start()].strip()
                statements_text = section_b_text[q36_match.start():].strip()
                
                group = {
                    'title': "Section B: Matching",
                    'passage': passage,
                    'type': 'matching',
                    'questions': []
                }
                
                # Extract statements 36-45
                # Using a more robust regex that handles potential line breaks or noise
                all_statements = re.findall(rf'(?:\s|^)(\d{{2}})\s*\.\s*(.*?)(?=(?:\s|^)\d{{2}}\s*\.|Section|Part|Questions|$)', statements_text, re.IGNORECASE | re.DOTALL)
                
                for q_num_str, content in all_statements:
                    q_num = int(q_num_str)
                    if 36 <= q_num <= 45:
                        group['questions'].append({
                            'q_number': q_num,
                            'content': content.strip(),
                            'section': "Reading Section B",
                            'options': []
                        })
                
                if len(group['questions']) >= 5:
                    # Sort questions by number just in case
                    group['questions'].sort(key=lambda x: x['q_number'])
                    groups.append(group)
                    break

    # --- 4. Listening Groups ---
    # Questions 1and2are basedon thenewsreport you havejustheard.
    # Use list() because iterators are one-time use
    # Flexible regex: remove mandatory period at the end and handle missing spaces
    listening_pattern = (
        r'Questions\s*(\d+)\s*(?:and|to|-|~)\s*(\d+)\s*are\s*based\s*on\s*the\s*'
        # CET-4 hears news reports, conversations and passages; CET-6 also hears
        # recordings and lectures. The noun itself is optional: extraction sometimes
        # drops it ('based on the you have just heard') and the heading is still one.
        r'(?:passage|news\s*report|conversation|recording|lecture|talk)?\s*'
        r'you\s*have\s*just\s*heard'
    )
    listening_groups = list(re.finditer(listening_pattern, norm_text, re.IGNORECASE))
    
    if not listening_groups:
        # Fallback for even more weird formats
        listening_groups = list(re.finditer(r'Questions\s*(\d+)\s*(?:and|to|~)\s*(\d+)\s*are\s*based\s*on\s*the\s*.*?heard', norm_text, re.IGNORECASE))
    
    for match in listening_groups:
        start_q, end_q = int(match.group(1)), int(match.group(2))
        # Reading headers may contain OCR-spliced "heard" text. Listening only
        # occupies questions 1-25 in both CET-4 and CET-6.
        if not 1 <= start_q <= end_q <= 25:
            continue
        mcqs = []
        for q_num in range(start_q, end_q + 1):
            q_data = extract_mcq_robust(q_num, full_text, "Listening Comprehension")
            if q_data:
                mcqs.append(q_data)
        
        if mcqs:
            groups.append({
                'title': f"Listening: Questions {start_q} to {end_q}",
                'passage': "Audio context required.",
                'type': 'listening',
                'questions': mcqs
            })

    return groups

if __name__ == "__main__":
    test_file = r"d:\lxx_py\英语刷题系统\data\2023-12-CET4-1.pdf"
    results = parse_cet_pdf_v2(test_file)
    print(f"Found {len(results)} groups in {test_file}")
    for g in results:
        print(f"Group: {g['title']} ({g['type']}) - {len(g['questions'])} questions")
