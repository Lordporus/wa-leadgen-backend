import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()
from app.clients.airtable_client import AirtableClient
from app.clients.gemini_client import GeminiClient

a = AirtableClient()
g = GeminiClient()

lead = a.get_lead('918527413901')
last_msg = lead.get('fields', {}).get('Last_Message', '')

# Append a Hot qualifying message
new_inbound = "Haan mujhe naye patients chahiye clinic ke liye, aur main hi owner hoon. Agar aap 15-20k me achhe leads la sakein toh kal se hi start kar sakte hain."
last_msg += f"\n[2026-06-13 16:00:00] INBOUND (text): {new_inbound}\n"

# Parse history
history = g.parse_conversation_history(last_msg)

# Get AI Reply
reply = g.generate_response_with_history(history, new_inbound)
print(f"=== AI REPLY ===\n{reply}")

# Update lead in Airtable with the AI reply
last_msg += f"[2026-06-13 16:01:00] OUTBOUND (text): {reply}\n"
score = g.score_lead(last_msg)
print(f"=== AI SCORE ===\n{score}")

a.update_lead_score('918527413901', score)
if score == 'Hot':
    a.update_lead_status('918527413901', 'Qualified')

# append the inbound msg first
a.append_message('918527413901', 'inbound', new_inbound, 'text')
# append the outbound reply
a.append_message('918527413901', 'outbound', reply, 'text')

print("Airtable updated.")
