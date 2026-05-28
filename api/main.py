# api/main.py

import os
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool
import redis
from dotenv import load_dotenv


def _redis_client(url: str, **kwargs):
    if os.environ.get("REDIS_CLUSTER", "false").lower() == "true":
        return redis.RedisCluster.from_url(
            url, ssl_cert_reqs=None, skip_full_coverage_check=True, **kwargs
        )
    return redis.from_url(url, **kwargs)
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request

from api.agent import propose_reroute
from api.models import AnomalyDetector, ETAPredictor

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

limiter = Limiter(key_func=get_remote_address)

# Module-level state (populated by lifespan)
_state: dict = {}
_retrain_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    r = _redis_client(REDIS_URL, decode_responses=True)

    anomaly_detector = AnomalyDetector.load()
    eta_predictor = ETAPredictor.load()

    _state["pool"] = pool
    _state["redis"] = r
    _state["anomaly_detector"] = anomaly_detector
    _state["eta_predictor"] = eta_predictor

    def _fit_models():
        with _pool_conn(pool) as conn:
            if not anomaly_detector.is_fitted:
                anomaly_detector.fit_and_score_all(conn)
            if not eta_predictor.is_fitted:
                eta_predictor.fit(conn)
                eta_predictor.update_predictions(conn)
        print("[api] Model fitting complete")

    t = threading.Thread(target=_fit_models, name="model_fitting", daemon=True)
    t.start()
    print("[api] Started model_fitting thread")

    print("[api] Startup complete")
    yield

    # Shutdown
    pool.closeall()
    print("[api] Shutdown complete")


app = FastAPI(title="FreightPilot API", lifespan=lifespan)
_CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000"
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@contextmanager
def _pool_conn(pool):
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def get_conn():
    conn = _state["pool"].getconn()
    try:
        yield conn
    finally:
        _state["pool"].putconn(conn)


def get_redis():
    return _state["redis"]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_status = "connected"
    except Exception:
        db_status = "error"

    try:
        get_redis().ping()
        redis_status = "connected"
    except Exception:
        redis_status = "error"

    return {
        "status": "ok",
        "db": db_status,
        "redis": redis_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------

_SORTABLE = {"id", "carrier_dot", "mode", "status", "freight_value_usd",
             "weight_tons", "eta_hours", "last_update", "created_at", "is_anomaly"}

@app.get("/shipments")
def list_shipments(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    mode: str = Query(None),
    status: str = Query(None),
    sort: str = Query("created_at"),
    sort_dir: str = Query("desc"),
):
    if sort not in _SORTABLE:
        sort = "created_at"
    order = f"{sort} {'DESC' if sort_dir.lower() == 'desc' else 'ASC'}"

    filters = []
    params = []

    if mode:
        filters.append("mode = %s")
        params.append(mode)
    if status:
        filters.append("status = %s")
        params.append(status)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as total FROM shipments {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""SELECT id, carrier_dot, customer_id, origin_state, destination_state,
                           mode, commodity, status, carrier_status,
                           freight_value_usd, weight_tons, eta_hours,
                           created_at, last_update, resolved, resolved_at,
                           predicted_eta_hours, anomaly_score, is_anomaly
                    FROM shipments {where}
                    ORDER BY {order}
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            rows = cur.fetchall()

    return {"shipments": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM shipments WHERE id = %s", (shipment_id,))
            shipment = cur.fetchone()
            if not shipment:
                raise HTTPException(status_code=404, detail="Shipment not found")

            cur.execute(
                "SELECT * FROM events WHERE shipment_id = %s ORDER BY timestamp DESC LIMIT 5",
                (shipment_id,),
            )
            events = cur.fetchall()

    return {"shipment": dict(shipment), "events": [dict(e) for e in events]}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

@app.get("/exceptions")
@limiter.limit("30/minute")
def get_exceptions(request: Request):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM (
                 SELECT DISTINCT ON (id) *, CASE
                   WHEN carrier_status = 'unresponsive' THEN 'unresponsive'
                   WHEN (EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600) - eta_hours > 4 THEN 'delayed'
                   WHEN (EXTRACT(EPOCH FROM (NOW() - last_update)) / 3600) > 6
                        AND carrier_status != 'unresponsive' THEN 'silent'
                   WHEN is_anomaly = TRUE THEN 'anomaly'
                 END AS exception_type
                 FROM shipments
                 WHERE status != 'delivered'
                   AND resolved = FALSE
                   AND (
                     carrier_status = 'unresponsive'
                     OR (EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600) - eta_hours > 4
                     OR (EXTRACT(EPOCH FROM (NOW() - last_update)) / 3600) > 6
                     OR is_anomaly = TRUE
                   )
                 ORDER BY id
               ) sub
               ORDER BY freight_value_usd DESC"""
        )
        rows = cur.fetchall()

        return {"exceptions": [dict(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Reroute
# ---------------------------------------------------------------------------

@app.post("/reroute/{shipment_id}")
@limiter.limit("20/minute")
def reroute_shipment(shipment_id: str, request: Request):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM shipments WHERE id = %s", (shipment_id,))
            shipment = cur.fetchone()
            if not shipment:
                raise HTTPException(status_code=404, detail="Shipment not found")

    options = propose_reroute(dict(shipment))
    return {"shipment_id": shipment_id, "reroute_options": options}


# ---------------------------------------------------------------------------
# Pipeline metrics
# ---------------------------------------------------------------------------

@app.get("/pipeline/metrics")
def pipeline_metrics():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pipeline_metrics ORDER BY timestamp DESC LIMIT 20"
            )
            rows = cur.fetchall()
            # Live counts so dashboard tiles are always current
            cur.execute(
                """SELECT
                   COUNT(*) FILTER (
                     WHERE carrier_status = 'unresponsive'
                        OR (eta_hours IS NOT NULL
                            AND last_update < NOW() - INTERVAL '1 hour' * (eta_hours - 4)
                            AND eta_hours > 4)
                        OR is_anomaly = TRUE
                   ) AS exceptions_detected,
                   COUNT(*) FILTER (WHERE is_anomaly = TRUE) AS anomalies_detected
                   FROM shipments
                   WHERE status NOT IN ('delivered', 'resolved')"""
            )
            live = cur.fetchone()
    metrics = [dict(r) for r in rows]
    # Overwrite the most-recent snapshot's counts with live values
    if metrics:
        metrics[0]["exceptions_detected"] = live["exceptions_detected"]
        metrics[0]["anomalies_detected"]   = live["anomalies_detected"]
    return {"metrics": metrics}


@app.get("/pipeline/stream-info")
def stream_info():
    r = get_redis()
    try:
        stream_len = r.xlen("freight-events")
        info = r.xinfo_stream("freight-events")
        last_entry_id = info.get("last-entry", [None])[0] if info else None
    except Exception:
        stream_len = 0
        last_entry_id = None

    try:
        pending = r.xpending("freight-events", "freightpilot-consumers")
        consumer_lag = pending.get("pending", 0) if isinstance(pending, dict) else pending[0]
    except Exception:
        consumer_lag = 0

    return {
        "stream_length": stream_len,
        "consumer_lag": consumer_lag,
        "last_entry_id": last_entry_id,
    }


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@app.get("/customers")
def list_customers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, company_name, tier, industry, state FROM customers ORDER BY company_name")
            rows = cur.fetchall()
    return {"customers": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

@app.post("/resolve/{shipment_id}")
def resolve_shipment(shipment_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM shipments WHERE id = %s", (shipment_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Shipment not found")
            cur.execute(
                "UPDATE shipments SET resolved = TRUE, resolved_at = NOW() WHERE id = %s",
                (shipment_id,),
            )
        conn.commit()
    return {"shipment_id": shipment_id, "resolved": True}


# ---------------------------------------------------------------------------
# ML retrain
# ---------------------------------------------------------------------------

def _retrain():
    try:
        detector = _state["anomaly_detector"]
        predictor = _state["eta_predictor"]
        with get_conn() as conn:
            detector.fit_and_score_all(conn)
            predictor.fit(conn)
            predictor.update_predictions(conn)
        _state["anomaly_detector"] = detector
        _state["eta_predictor"] = predictor
        print("[api] ML retrain complete")
    finally:
        _retrain_lock.release()


@app.post("/ml/retrain")
@limiter.limit("5/minute")
def ml_retrain(request: Request, background_tasks: BackgroundTasks):
    if not _retrain_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="retrain already in progress")
    try:
        background_tasks.add_task(_retrain)
    except Exception:
        _retrain_lock.release()
        raise
    return {"status": "retrain started"}
