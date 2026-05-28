# db/export_tableau.py
import csv
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path(__file__).parent.parent / "docs" / "tableau"
OUTPUT_FILE = OUTPUT_DIR / "shipments_export.csv"

QUERY = """
SELECT
    s.id,
    s.carrier_dot,
    c.name               AS carrier_name,
    s.customer_id,
    cu.company_name      AS customer_name,
    cu.tier              AS customer_tier,
    s.origin_state,
    s.destination_state,
    s.mode,
    s.commodity,
    s.status,
    s.carrier_status,
    s.freight_value_usd,
    s.weight_tons,
    s.eta_hours,
    s.predicted_eta_hours,
    s.anomaly_score,
    s.is_anomaly,
    s.created_at,
    s.last_update,
    s.resolved,
    s.resolved_at
FROM shipments s
LEFT JOIN carriers c  ON c.dot  = s.carrier_dot
LEFT JOIN customers cu ON cu.id = s.customer_id
ORDER BY s.created_at DESC
"""


def export(output_file: Path = OUTPUT_FILE) -> int:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("[tableau_export] No rows — nothing written.")
        return 0

    with open(output_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"[tableau_export] {len(rows):,} rows → {output_file}")
    return len(rows)


if __name__ == "__main__":
    export()
