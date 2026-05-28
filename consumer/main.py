# consumer/main.py

import os
import time
from datetime import datetime, timezone

import psycopg2
import redis
from dotenv import load_dotenv


def _redis_client(url: str, **kwargs):
    """Use RedisCluster when REDIS_CLUSTER=true (ElastiCache), plain Redis otherwise (local)."""
    if os.environ.get("REDIS_CLUSTER", "false").lower() == "true":
        return redis.RedisCluster.from_url(url, **kwargs)
    return redis.from_url(url, **kwargs)

load_dotenv()

STREAM_NAME = "freight-events"
GROUP_NAME = "freightpilot-consumers"
CONSUMER_NAME = "consumer-1"
DLQ_STREAM = "freight:dlq"
RETRY_HASH = "freight:retry-counts"
MAX_RETRIES = 3
CLAIM_IDLE_MS = 60_000   # reclaim PEL messages idle > 60 s
RECLAIM_INTERVAL = 30    # seconds between XAUTOCLAIM sweeps
METRICS_INTERVAL = 30    # seconds between metric flushes

REQUIRED_FIELDS = {"shipment_id", "event_type", "mode", "timestamp"}

STATUS_MAP = {
    "shipment_created": None,
    "picked_up": "picked_up",
    "in_transit": "in_transit",
    "out_for_delivery": "out_for_delivery",
    "delivered": "delivered",
    "delay_reported": "delayed",
    "carrier_silent": "in_transit",
    "location_update": None,
}


def create_consumer_group(r):
    try:
        r.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
        print(f"[consumer] Created consumer group '{GROUP_NAME}'")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            print(f"[consumer] Consumer group '{GROUP_NAME}' already exists")
        else:
            raise


def process_message(conn, message_id, data):
    """
    Returns (ok, reason, permanent).
    permanent=True means validation failure — retry will not help.
    """
    if not REQUIRED_FIELDS.issubset(data.keys()):
        missing = REQUIRED_FIELDS - data.keys()
        return False, f"missing fields: {missing}", True

    shipment_id = data["shipment_id"]
    event_type = data["event_type"]
    mode = data["mode"]
    location = data.get("location")
    note = data.get("note")
    timestamp_str = data["timestamp"]

    try:
        timestamp = datetime.fromisoformat(timestamp_str)
    except ValueError:
        return False, f"invalid timestamp: {timestamp_str!r}", True

    new_status = STATUS_MAP.get(event_type)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO events (shipment_id, event_type, mode, location, note, timestamp, stream_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (shipment_id, event_type, mode, location, note, timestamp, message_id),
            )
            cur.execute(
                "UPDATE shipments SET last_update = NOW() WHERE id = %s",
                (shipment_id,),
            )
            if new_status is not None:
                cur.execute(
                    "UPDATE shipments SET status = %s WHERE id = %s",
                    (new_status, shipment_id),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return False, f"db error: {e}", False

    return True, "", False


def send_to_dlq(r, message_id, data, reason, attempts):
    r.xadd(DLQ_STREAM, {
        **data,
        "_original_id": message_id,
        "_reason": reason,
        "_attempts": str(attempts),
        "_failed_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"[consumer] DLQ ← {message_id} after {attempts} attempt(s): {reason}")


def handle_result(r, message_id, data, ok, reason, permanent):
    if ok:
        r.xack(STREAM_NAME, GROUP_NAME, message_id)
        r.hdel(RETRY_HASH, message_id)
        return

    # Permanent failures (bad schema, unparseable data) go straight to DLQ.
    if permanent:
        send_to_dlq(r, message_id, data, reason, 1)
        r.xack(STREAM_NAME, GROUP_NAME, message_id)
        r.hdel(RETRY_HASH, message_id)
        return

    # Transient failures (DB error) — increment retry counter.
    attempts = r.hincrby(RETRY_HASH, message_id, 1)
    if attempts >= MAX_RETRIES:
        send_to_dlq(r, message_id, data, reason, attempts)
        r.xack(STREAM_NAME, GROUP_NAME, message_id)
        r.hdel(RETRY_HASH, message_id)
    else:
        # Leave in PEL — XAUTOCLAIM will reclaim it after CLAIM_IDLE_MS.
        print(f"[consumer] ✗ {message_id} attempt {attempts}/{MAX_RETRIES}: {reason}")


def reclaim_pending(r, conn):
    """
    Pull back PEL messages idle > CLAIM_IDLE_MS and reprocess them.
    Returns (extra_events_processed, extra_latencies).
    Requires Redis >= 6.2 for XAUTOCLAIM; silently skips on older versions.
    """
    extra_processed = 0
    extra_latencies = []

    try:
        result = r.xautoclaim(
            STREAM_NAME, GROUP_NAME, CONSUMER_NAME,
            min_idle_time=CLAIM_IDLE_MS,
            start_id="0-0",
            count=100,
        )
        entries = result[1] if result and len(result) >= 2 else []
    except redis.exceptions.ResponseError as e:
        print(f"[consumer] XAUTOCLAIM unavailable: {e}")
        return extra_processed, extra_latencies

    for message_id, data in entries:
        t_start = time.time()
        ok, reason, permanent = process_message(conn, message_id, data)
        elapsed_ms = (time.time() - t_start) * 1000
        handle_result(r, message_id, data, ok, reason, permanent)
        if ok:
            extra_processed += 1
            extra_latencies.append(elapsed_ms)
            print(
                f"[consumer] ✓ (reclaimed) {data.get('event_type')} "
                f"for {data.get('shipment_id')} ({elapsed_ms:.1f}ms)"
            )

    if entries:
        print(f"[consumer] Reclaimed {len(entries)} pending message(s)")

    return extra_processed, extra_latencies


def insert_metrics(conn, r, events_processed, latencies):
    now = datetime.now(timezone.utc)
    events_per_second = events_processed / METRICS_INTERVAL if METRICS_INTERVAL > 0 else 0

    try:
        pending_info = r.xpending(STREAM_NAME, GROUP_NAME)
        consumer_lag = pending_info.get("pending", 0) if isinstance(pending_info, dict) else pending_info[0]
    except Exception:
        consumer_lag = 0

    avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

    with conn.cursor() as cur:
        cur.execute(
            """SELECT
               COUNT(*) FILTER (
                 WHERE carrier_status = 'unresponsive'
                    OR (eta_hours IS NOT NULL AND last_update < NOW() - INTERVAL '1 hour' * (eta_hours - 4) AND eta_hours > 4)
                    OR is_anomaly = TRUE
               ) AS exceptions_detected,
               COUNT(*) FILTER (WHERE is_anomaly = TRUE) AS anomalies_detected
               FROM shipments
               WHERE status NOT IN ('delivered', 'resolved')"""
        )
        row = cur.fetchone()
        exceptions_detected = row[0]
        anomalies_detected = row[1]

        cur.execute(
            """INSERT INTO pipeline_metrics
               (timestamp, events_processed, events_per_second, consumer_lag,
                exceptions_detected, anomalies_detected, processing_latency_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (now, events_processed, events_per_second, consumer_lag,
             exceptions_detected, anomalies_detected, avg_latency),
        )
    conn.commit()
    print(
        f"[consumer] Metrics — processed: {events_processed}, "
        f"eps: {events_per_second:.2f}, lag: {consumer_lag}, "
        f"exceptions: {exceptions_detected}, anomalies: {anomalies_detected}, "
        f"latency: {avg_latency:.1f}ms"
    )


def main():
    r = _redis_client(os.environ["REDIS_URL"], decode_responses=True)
    conn = psycopg2.connect(os.environ["DATABASE_URL"])

    print("[consumer] Connected to Redis and PostgreSQL")
    create_consumer_group(r)

    events_processed = 0
    latencies = []
    last_metrics_time = time.time()
    last_reclaim_time = time.time()

    # Recover any messages left pending from a previous crash before reading new ones.
    extra, extra_lat = reclaim_pending(r, conn)
    events_processed += extra
    latencies.extend(extra_lat)

    try:
        while True:
            messages = r.xreadgroup(
                GROUP_NAME, CONSUMER_NAME,
                {STREAM_NAME: ">"},
                count=10,
                block=5000,
            )

            if messages:
                for stream, entries in messages:
                    for message_id, data in entries:
                        t_start = time.time()
                        ok, reason, permanent = process_message(conn, message_id, data)
                        elapsed_ms = (time.time() - t_start) * 1000
                        handle_result(r, message_id, data, ok, reason, permanent)
                        if ok:
                            events_processed += 1
                            latencies.append(elapsed_ms)
                            print(
                                f"[consumer] ✓ {data.get('event_type')} "
                                f"for {data.get('shipment_id')} ({elapsed_ms:.1f}ms)"
                            )

            now = time.time()

            if now - last_reclaim_time >= RECLAIM_INTERVAL:
                extra, extra_lat = reclaim_pending(r, conn)
                events_processed += extra
                latencies.extend(extra_lat)
                last_reclaim_time = now

            if now - last_metrics_time >= METRICS_INTERVAL:
                insert_metrics(conn, r, events_processed, latencies)
                events_processed = 0
                latencies = []
                last_metrics_time = now

    except KeyboardInterrupt:
        print("[consumer] Shutting down gracefully")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
