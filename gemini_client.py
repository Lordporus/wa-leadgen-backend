import google.generativeai as genai
from openai import OpenAI
import logging
from config import (
    GEMINI_API_KEY,
    NINEROUTER_API_KEY,
    NINEROUTER_BASE_URL,
    NINEROUTER_MODEL,
)

logger = logging.getLogger(__name__)

# Configure Gemini (fallback)
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    logger.warning("GEMINI_API_KEY not found. Direct-Gemini fallback won't work.")

# Configure 9Router (primary)
_router_client: OpenAI | None = None
if NINEROUTER_API_KEY:
    _router_client = OpenAI(
        api_key=NINEROUTER_API_KEY,
        base_url=NINEROUTER_BASE_URL,
        timeout=15,
    )
    logger.info(f"9Router configured → {NINEROUTER_BASE_URL} model={NINEROUTER_MODEL}")
else:
    logger.warning("NINEROUTER_API_KEY not set. Will use direct Gemini only.")

DEFAULT_SYSTEM_PROMPT = """
Tum Team BuildWithPorus ke AI sales assistant ho — ek B2B marketing agency jo dentists ko WhatsApp aur AI automation ke through naye patient leads dilate hai, bina expensive ads ke.

TONE: Friendly, confident, sales-driven Hinglish. Short messages, WhatsApp-style (maximum 2-3 lines). Emojis sparingly use karo. Corporate ya robotic bilkul nahi lagna chahiye.

GOAL: Naturally (conversation ke through, interrogation nahi) in signals ko surface karna:
1. NEED: Kya clinic currently enough new patients lane me struggle kar raha hai?
2. AUTHORITY: Kya ye person owner/decision-maker hai for marketing spend?
3. BUDGET FIT: Gauge willingness - jaise "agar hum aapko har mahine 10-15 ready patients la kar dein, kya iske liye ₹15-20k/month invest karna sense banega?"
4. TIMELINE: Agar interested hain, toh kab start karna chahenge?

End goal of a "hot" conversation: offer to set up a quick call using this link: https://calendly.com/buildporus/30min (e.g. "Bilkul! Yahan se ek free strategy call book kar lo jab convenient ho: https://calendly.com/buildporus/30min").

OBJECTION HANDLING (Few-shot examples):
User: "Not interested"
AI: "Koi problem nahi doctor! Just in case aap future mein patient footfall badhana chahein, hum connected rahenge. Have a great day! 😊"

User: "Send details"
AI: "Zaroor! Main details bhej deta hoon. Waise abhi aap patients lane ke liye kya strategies use kar rahe hain, jaise JustDial ya ads?"

User: "Abhi busy hoon"
AI: "No worries doctor, samajh sakta hoon. Main kal is waqt ek baar fir message karunga. Ya phir aap apne free time mein reply kar sakte hain."

User: "Kitna cost hai?"
AI: "Cost clinic ki requirements par depend karta hai, par agar hum aapko har mahine 10-15 ready patients la kar dein, kya iske liye ₹15-20k/month invest karna sense banega aapke liye?"

User: "How does this work"
AI: "Hum aapke local area mein potential patients ko identify karte hain aur WhatsApp automation ke through unhe aapke clinic se connect karte hain. Kya main aapke clinic ke liye ek free strategy call arrange karun?"

IMPORTANT: Hamesha conversation naturally lead karo, question by question.
"""

class GeminiClient:
    def __init__(self, system_prompt: str | None = None):
        """
        Initialise the Gemini client.

        system_prompt: per-client sales persona loaded by tenant.py.
                       Falls back to DEFAULT_SYSTEM_PROMPT when None/empty.
        """
        self._fallback_model = genai.GenerativeModel('gemini-2.5-flash')
        self._system_prompt = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
        
    def parse_conversation_history(self, history_text: str):
        """
        Parses Last_Message append-only log into a list of Gemini history dicts:
        [{'role': 'user'|'model', 'parts': ['text']}]
        INBOUND -> user, OUTBOUND -> model
        Format: [YYYY-MM-DD HH:MM:SS] INBOUND (text): Message text
        """
        if not history_text:
            return []
        
        history = []
        lines = history_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if "] INBOUND" in line:
                parts = line.split("): ", 1)
                if len(parts) > 1:
                    history.append({"role": "user", "parts": [parts[1]]})
            elif "] OUTBOUND" in line:
                parts = line.split("): ", 1)
                if len(parts) > 1:
                    history.append({"role": "model", "parts": [parts[1]]})
        return history

    def generate_response_with_history(self, parsed_history: list, user_message: str) -> str:
        # ── Primary: 9Router ──
        if _router_client:
            try:
                messages = [{"role": "system", "content": self._system_prompt}]
                for turn in parsed_history:
                    role = "assistant" if turn["role"] == "model" else "user"
                    messages.append({"role": role, "content": turn["parts"][0]})
                messages.append({"role": "user", "content": user_message})

                resp = _router_client.chat.completions.create(
                    model=NINEROUTER_MODEL,
                    messages=messages,
                )
                reply = resp.choices[0].message.content
                logger.info(f"9Router OK — model_used={resp.model}")
                return reply
            except Exception as e:
                logger.warning(f"9Router failed ({e}), falling back to direct Gemini")

        # ── Fallback: direct Gemini SDK ──
        try:
            gemini_history = [
                {"role": "user", "parts": [self._system_prompt]},
                {"role": "model", "parts": ["Understood. I will act as the sales assistant in Hinglish."]}
            ]
            gemini_history.extend(parsed_history)

            chat = self._fallback_model.start_chat(history=gemini_history)
            response = chat.send_message(user_message)
            logger.info("Direct Gemini fallback OK")
            return response.text
        except Exception as e:
            logger.error(f"Both 9Router and direct Gemini failed: {e}")
            return "Sorry, abhi network issue hai. Main thodi der mein aapse connect karta hu."

    def extract_lead_info(self, text: str):
        prompt = f"""
        Extract the person's name and business/clinic name from the following text if present.
        Return ONLY a JSON dictionary with keys "Name" and "Business_Name". 
        If a value is not found, use null.
        Text: "{text}"
        """
        import json

        # ── Primary: 9Router ──
        if _router_client:
            try:
                resp = _router_client.chat.completions.create(
                    model=NINEROUTER_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                )
                content = resp.choices[0].message.content
                content = content.replace("```json", "").replace("```", "").strip()
                logger.info(f"9Router extract OK — model_used={resp.model}")
                return json.loads(content)
            except Exception as e:
                logger.warning(f"9Router extract failed ({e}), falling back to direct Gemini")

        # ── Fallback: direct Gemini SDK ──
        try:
            response = self._fallback_model.generate_content(prompt)
            content = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        except Exception as e:
            logger.error(f"Extraction error: {e}")
            return {}

    def score_lead(self, conversation_text: str) -> str:
        prompt = f"""
        Analyze the following conversation history between an AI sales assistant and a dentist.
        Score the lead as "Cold", "Warm", or "Hot" based on the signals for NEED, AUTHORITY, BUDGET FIT, and TIMELINE.
        - Hot: Clearly interested, ready to book a call, confirmed budget fit or strong need.
        - Warm: Engaged, asking questions, somewhat interested but hasn't committed to a call.
        - Cold: Not interested, dismissive, or completely unresponsive to the value proposition.
        
        Return ONLY the word "Cold", "Warm", or "Hot".
        
        Conversation:
        {conversation_text}
        """

        def _clean_score(raw: str) -> str:
            score = raw.strip().replace('"', '').replace('.', '').capitalize()
            return score if score in ["Cold", "Warm", "Hot"] else "Cold"

        # ── Primary: 9Router ──
        if _router_client:
            try:
                resp = _router_client.chat.completions.create(
                    model=NINEROUTER_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                )
                logger.info(f"9Router score OK — model_used={resp.model}")
                return _clean_score(resp.choices[0].message.content)
            except Exception as e:
                logger.warning(f"9Router score failed ({e}), falling back to direct Gemini")

        # ── Fallback: direct Gemini SDK ──
        try:
            response = self._fallback_model.generate_content(prompt)
            return _clean_score(response.text)
        except Exception as e:
            logger.error(f"Scoring error: {e}")
            return "Cold"
