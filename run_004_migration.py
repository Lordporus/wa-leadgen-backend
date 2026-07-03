import os
import sys
from dotenv import load_dotenv

# Load env variables to get DATABASE_URL and LORD_PHONE_NUMBER
load_dotenv()

# We need SQLAlchemy
from sqlalchemy import create_engine, text

database_url = os.environ.get("DATABASE_URL")
lord_phone = os.environ.get("LORD_PHONE_NUMBER")

if not database_url:
    print("No DATABASE_URL found in .env")
    sys.exit(1)

engine = create_engine(database_url)

with engine.begin() as conn:
    # 1. Run the migration
    with open("migrations/004_f6b_jobs.sql", "r") as f:
        sql = f.read()
    
    print("Running migration...")
    conn.execute(text(sql))
    print("Migration applied successfully.")
    
    # 2. Backfill client_id = 1
    if lord_phone:
        print(f"Backfilling client_id=1 admin_phone to {lord_phone}")
        conn.execute(text("UPDATE clients SET admin_phone = :phone WHERE id = 1"), {"phone": lord_phone})
        print("Backfill complete.")
    else:
        print("LORD_PHONE_NUMBER not found in .env, skipping backfill.")

    # Verify columns
    result = conn.execute(text("SELECT id, name, admin_phone, calendly_api_token FROM clients WHERE id = 1"))
    row = result.fetchone()
    print("Client 1 after backfill:", row)
