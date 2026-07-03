import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

if 'Response' not in content.splitlines()[0]:
    content = re.sub(r'from fastapi import (.*)', r'from fastapi import \1, Response', content, count=1)

def replace_func(match):
    limiter_line = match.group(1)
    def_line = match.group(2)
    
    if 'response: Response' in def_line:
        return match.group(0)
    
    new_def_line = def_line.replace('request: Request', 'request: Request, response: Response')
    return f"{limiter_line}\n{new_def_line}"

# regex matches @limiter.limit line, then the def line
content = re.sub(r'(@limiter\.limit[^\n]*)\n([ \t]*(?:async )?def \w+\(.*?\):)', replace_func, content, flags=re.DOTALL)

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patched main.py")
