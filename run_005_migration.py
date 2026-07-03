import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

MIGRATION_FILE = os.path.join(os.path.dirname(__file__), "migrations", "005_idempotency.sql")

def run():
    print(f"Connecting to {DATABASE_URL.split('@')[1]}...")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cursor = conn.cursor()
    
    with open(MIGRATION_FILE, "r") as f:
        sql = f.read()
        
    print("Executing migration 005_idempotency.sql...")
    try:
        cursor.execute(sql)
        print("Success! Unique constraint added to wa_message_id.")
    except Exception as e:
        print(f"Migration failed: {e}")
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    run()
