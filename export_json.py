import json
import os
from parser import parse_cet_pdf

def export_all_to_json():
    data_dir = r"d:\lxx_py\英语刷题系统\data"
    output_file = r"d:\lxx_py\英语刷题系统\parsed_data.json"
    
    all_data = {}
    
    files = [f for f in os.listdir(data_dir) if f.endswith('.pdf')]
    print(f"Starting export of {len(files)} files...")
    
    for filename in files:
        file_path = os.path.join(data_dir, filename)
        print(f"Processing {filename}...")
        try:
            questions = parse_cet_pdf(file_path)
            all_data[filename] = questions
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            all_data[filename] = []
            
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
        
    print(f"\nSuccess! Exported all data to {output_file}")

if __name__ == "__main__":
    export_all_to_json()
