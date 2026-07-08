from database import SessionLocal
from models import Client
with SessionLocal() as s:
    c = s.query(Client).filter(Client.id == 1).first()
    print(f'DB system_prompt: {c.system_prompt}')
