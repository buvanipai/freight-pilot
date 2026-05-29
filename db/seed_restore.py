# db/seed_restore.py
# Restores seed_dump.sql into the database if it hasn't been seeded yet.
import os
import subprocess
import psycopg2
from dotenv import load_dotenv

load_dotenv()

database_url = os.environ["DATABASE_URL"]

force = os.environ.get("FORCE_RESEED", "false").lower() == "true"

conn = psycopg2.connect(database_url)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM carriers")
        if cur.fetchone()[0] > 0:
            if not force:
                print("[seed_restore] Data already present, skipping.")
                conn.close()
                exit(0)
            print("[seed_restore] FORCE_RESEED=true — truncating and reseeding")
            cur.execute("TRUNCATE carriers, customers, shipments, events, pipeline_metrics CASCADE")
    conn.commit()
finally:
    conn.close()

dump_path = os.path.join(os.path.dirname(__file__), "seed_dump.sql")
result = subprocess.run(
    ["psql", database_url, "-f", dump_path],
    capture_output=True, text=True
)
if result.returncode != 0:
    print(result.stderr)
    raise RuntimeError("[seed_restore] psql failed")
print("[seed_restore] Seed dump restored successfully.")
