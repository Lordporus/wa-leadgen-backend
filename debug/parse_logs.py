import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json

count_500 = 0
count_err = 0
log_path = r'C:\Users\Sachin\.gemini\antigravity\brain\8f0aa796-bb1a-44c2-9be5-5f81ff45ceca\.system_generated\steps\977\output.txt'

with open(log_path, 'r', encoding='utf-8') as f:
    data = json.load(f)
    for line in data.get('logs', []):
        text = line.get('message', '')
        if ' 500 ' in text or 'HTTP/1.1" 500' in text:
            count_500 += 1
            print('500 Error:', text)
        elif 'error' in text.lower() or 'exception' in text.lower():
            count_err += 1
            print('Exception:', text)

print('Total 500s:', count_500)
print('Total Exceptions:', count_err)
