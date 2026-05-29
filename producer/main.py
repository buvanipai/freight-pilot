# producer/main.py

import os
import random
import time
from datetime import datetime, timezone

import psycopg2
import redis
from dotenv import load_dotenv


def _redis_client(url: str, **kwargs):
    if os.environ.get("REDIS_CLUSTER", "false").lower() == "true":
        return redis.RedisCluster.from_url(
            url, ssl_cert_reqs=None, skip_full_coverage_check=True, **kwargs
        )
    return redis.from_url(url, **kwargs)

load_dotenv()

STREAM_NAME = "freight-events"

LOCATIONS = [
    "Memphis, TN", "Chicago, IL", "Dallas, TX", "Atlanta, GA", "Los Angeles, CA",
    "Houston, TX", "Phoenix, AZ", "Seattle, WA", "Denver, CO", "Miami, FL",
    "Kansas City, MO", "Nashville, TN", "Indianapolis, IN", "Columbus, OH", "Portland, OR",
    "Louisville, KY", "Salt Lake City, UT", "Albuquerque, NM", "Detroit, MI", "Baltimore, MD",
]

EVENT_TYPES_NORMAL = ["in_transit", "picked_up", "out_for_delivery", "location_update"]
NOTES_NORMAL = ["On schedule", "En route", "No issues", "Moving normally", "ETA confirmed"]
NOTES_DELAY = ["Weather delay", "Traffic congestion", "Mechanical issue", "Driver change", "Loading delay"]


def load_shipment_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, mode, freight_value_usd, carrier_dot, status FROM shipments WHERE status != 'delivered'")
        rows = cur.fetchall()
    return [{"id": r[0], "mode": r[1], "freight_value_usd": r[2], "carrier_dot": r[3], "status": r[4]} for r in rows]


def load_carrier_name(conn, dot):
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM carriers WHERE dot = %s", (dot,))
        row = cur.fetchone()
    return row[0] if row else "UNKNOWN CARRIER"


def inject_exception(conn, shipment):
    roll = random.random()
    if roll < 0.5:
        # Delay
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shipments SET status = 'delayed', eta_hours = eta_hours + %s, last_update = NOW() WHERE id = %s",
                (random.uniform(2, 8), shipment["id"])
            )
        conn.commit()
        return "delay_reported", random.choice(NOTES_DELAY)
    else:
        # Unresponsive
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shipments SET carrier_status = 'unresponsive', last_update = NOW() WHERE id = %s",
                (shipment["id"],)
            )
        conn.commit()
        return "carrier_silent", "Carrier not responding"


def main():
    r = _redis_client(os.environ["REDIS_URL"], decode_responses=True)
    conn = psycopg2.connect(os.environ["DATABASE_URL"])

    print(f"[producer] Connected to Redis and PostgreSQL")

    # Refresh stale last_update so old seed data doesn't immediately flood the silent exception queue
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE shipments SET last_update = NOW() - (random() * INTERVAL '2 hours') "
            "WHERE status != 'delivered' AND last_update < NOW() - INTERVAL '1 hour'"
        )
    conn.commit()
    print(f"[producer] Refreshed stale last_update timestamps")

    shipments = load_shipment_ids(conn)
    print(f"[producer] Loaded {len(shipments)} active shipments")

    # Cache carrier names
    carrier_cache = {}

    try:
        while True:
            # Reload shipments periodically to pick up new ones
            if random.random() < 0.05:
                shipments = load_shipment_ids(conn)

            if not shipments:
                time.sleep(2)
                continue

            n_events = random.randint(1, 3)
            for _ in range(n_events):
                shipment = random.choice(shipments)

                if shipment["carrier_dot"] not in carrier_cache:
                    carrier_cache[shipment["carrier_dot"]] = load_carrier_name(conn, shipment["carrier_dot"])
                carrier_name = carrier_cache[shipment["carrier_dot"]]

                # 10% exception injection; out_for_delivery shipments have 25% chance to deliver
                if shipment["status"] == "out_for_delivery" and random.random() < 0.25:
                    event_type = "delivered"
                    note = random.choice(["Delivered successfully", "Package received", "Delivery confirmed", "Shipment complete"])
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE shipments SET status = 'delivered', last_update = NOW() WHERE id = %s",
                            (shipment["id"],)
                        )
                    conn.commit()
                    shipments = [s for s in shipments if s["id"] != shipment["id"]]
                elif random.random() < 0.10:
                    event_type, note = inject_exception(conn, shipment)
                else:
                    event_type = random.choice(EVENT_TYPES_NORMAL)
                    note = random.choice(NOTES_NORMAL)

                event = {
                    "shipment_id": shipment["id"],
                    "event_type": event_type,
                    "mode": shipment["mode"],
                    "location": random.choice(LOCATIONS),
                    "note": note,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "freight_value_usd": str(round(shipment["freight_value_usd"], 2)),
                    "carrier": carrier_name,
                }

                msg_id = r.xadd(STREAM_NAME, event)
                print(f"[producer] Published {event_type} for {shipment['id']} → {msg_id}")

            sleep_secs = random.uniform(2, 5)
            time.sleep(sleep_secs)

    except KeyboardInterrupt:
        print("[producer] Shutting down gracefully")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
