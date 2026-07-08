import database
from config import DATABASE_URL
from sqlalchemy import text
import pprint

# Initialize database session maker
database.init_engine(DATABASE_URL)

with database.SessionLocal() as s:
    print("=== STEP 2: created_at counts ===")
    res1 = s.execute(text("SELECT created_at, COUNT(*) FROM leads WHERE client_id = 1 GROUP BY created_at ORDER BY created_at DESC LIMIT 20"))
    for row in res1:
        print(row)
        
    print("\n=== STEP 3: Recent leads ===")
    res2 = s.execute(text("SELECT id, name, created_at FROM leads WHERE client_id = 1 ORDER BY id DESC LIMIT 10"))
    for row in res2:
        print(row)
