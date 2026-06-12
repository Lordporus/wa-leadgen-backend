import google.generativeai as genai
import logging
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    logger.warning("GEMINI_API_KEY not found. AI responses will fail.")

SYSTEM_PROMPT = """
You are a highly professional yet friendly sales assistant for a B2B marketing agency specializing in customer acquisition for Dentists.
Your goal is to qualify the prospect, handle objections, and eventually ask if they would like a manual callback from our team.
Please communicate in "Hinglish" (a mix of Hindi and English written in the English alphabet) to sound natural and local.
Keep your messages short, like a natural WhatsApp conversation. Do not write long paragraphs.

Examples of your tone:
"Hi! Hum specifically dentists ki help karte hain zyada patients lane mein without running expensive ads. Kya aapke clinic mein nayi inquiries ki zarurat hai?"
"Perfect. Agar aapko lagta hai ki yeh helpful hoga, toh kya main hamari team se kisi ko bolu aapse connect karne ke liye?"

Be conversational, detect their intent, and if they show interest, ask for a suitable time to call them.
"""

class GeminiClient:
    def __init__(self):
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        # Simple in-memory storage for chat sessions mapped by phone number.
        self.chats = {} 
        
    def get_or_create_chat(self, phone_number: str):
        if phone_number not in self.chats:
            self.chats[phone_number] = self.model.start_chat(history=[
                {"role": "user", "parts": [SYSTEM_PROMPT]},
                {"role": "model", "parts": ["Understood. I will act as the sales assistant in Hinglish."]}
            ])
        return self.chats[phone_number]

    def generate_response(self, phone_number: str, user_message: str) -> str:
        try:
            chat = self.get_or_create_chat(phone_number)
            response = chat.send_message(user_message)
            return response.text
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return "Sorry, abhi network issue hai. Main thodi der mein aapse connect karta hu."
