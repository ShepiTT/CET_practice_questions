import pdfplumber
import re
import os

def extract_mcq_robust(q_num, text_to_search, section_name):
    # Find the start of the question: "q_num. " (very flexible, handle missing spaces like "1.A)" or "1 . A)")
    start_pattern = rf'(?:\s|^){q_num}\s*\.(?:\s*|(?=[A-D]\)))'
    start_match = re.search(start_pattern, text_to_search)
    if not start_match:
        return None
    
    # Search window: about 1500 chars
    window = text_to_search[start_match.end():start_match.end() + 1500]
    
    # Labels A) B) C) D) - handle missing spaces like A)Option
    labels = [r'A\)', r'B\)', r'C\)', r'D\)']
    pos = {}
    for label in labels:
        m = re.search(label, window)
        if m:
            pos[label] = m.start()
    
    if len(pos) < 4:
        return None
        
    sorted_labels = sorted(pos.keys(), key=lambda l: pos[l])
    
    # Content is between start and first label
    content = window[:pos[sorted_labels[0]]].strip()
    if not content or len(content) < 2:
        content = f"Question {q_num}"
    
    options_dict = {}
    for i in range(len(sorted_labels)):
        curr_label = sorted_labels[i]
        start = pos[curr_label] + 2 # skip "A)"
        
        if i + 1 < len(sorted_labels):
            next_label = sorted_labels[i+1]
            end = pos[next_label]
        else:
            end = len(window)
            
        segment = window[start:end]
        # Clean segment: stop at next question or major markers
        next_q_num = q_num + 1
        # Flexible next question marker: "2. ", "2 . ", "2.A)", etc.
        next_q_pattern = rf'(?:\s|^){next_q_num}\s*\.(?:\s*|(?=[A-D]\)))'
        # Even more aggressive splitting for stuck-together strings like "Questions3and4"
        split_pattern = next_q_pattern + r'|Questions\s*\d+|Section\s*[A-O]|Part\s*[I-V]|Passage\s*(?:One|Two|Three|I|II|III)|\bDirections\b'
        segment = re.split(split_pattern, segment, flags=re.IGNORECASE)[0].strip()
        
        label_char = curr_label[0] # 'A', 'B', etc.
        options_dict[label_char] = segment
        
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
    # Remove website links
    text = re.sub(r'pastpapers\.cn', '', text, flags=re.IGNORECASE)
    # Remove patterns like "·2024年6月四级真题(第一套)· 6"
    text = re.sub(r'·\d{4}年\d{1,2}月四级真题\(.*?\)\s*·\s*\d+', '', text)
    # Remove patterns like "2023 年 12 月四级真题第 1 套 第 4 页，共 9 页"
    text = re.sub(r'\d{4}\s*年\s*\d{1,2}\s*月四级真题第\s*\d+\s*套\s*第\s*\d+\s*页，共\s*\d+\s*页', '', text)
    # Remove standalone page numbers or "Page X" if they appear at the end of lines
    text = re.sub(r'\n\s*第\s*\d+\s*页.*?\n', '\n', text)
    return text

def parse_cet_pdf_v2(file_path):
    filename = os.path.basename(file_path)
    groups = []
    
    with pdfplumber.open(file_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            # Increase x_tolerance to better capture spaces between words
            # Default is usually 3. Increasing it slightly helps with stuck-together text.
            text = page.extract_text(x_tolerance=2, y_tolerance=3)
            if text:
                full_text += text + "\n"

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
    cloze_section = re.search(r'Section\s*A.*?ten\s*blanks.*?(.*?)([A-Z]\)\s*[A-Z].*?)(?=(?:Section\s*B|Part\s*IV|$))', norm_text, re.DOTALL | re.IGNORECASE)
    if cloze_section:
        passage = cloze_section.group(1).strip()
        bank_text = cloze_section.group(2).strip()
        
        bank = []
        bank_matches = re.findall(r'([A-Z])\)\s*([A-Za-z\-]+)', bank_text)
        for label, word in bank_matches:
            bank.append({'label': label, 'text': word})
            
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
    listening_pattern = r'Questions\s*(\d+)\s*(?:and|to|-|~)\s*(\d+)\s*are\s*based\s*on\s*the\s*(?:passage|news\s*report|conversation)\s*you\s*have\s*just\s*heard'
    listening_groups = list(re.finditer(listening_pattern, norm_text, re.IGNORECASE))
    
    if not listening_groups:
        # Fallback for even more weird formats
        listening_groups = list(re.finditer(r'Questions\s*(\d+)\s*(?:and|to|~)\s*(\d+)\s*are\s*based\s*on\s*the\s*.*?heard', norm_text, re.IGNORECASE))
    
    for match in listening_groups:
        start_q, end_q = int(match.group(1)), int(match.group(2))
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
