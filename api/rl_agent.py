"""
RL inference module — loads the trained PPO (shaped_v2) and runs a single
forward pass to produce an action recommendation for any shipment DB row.

Stable-baselines3 / torch imports are lazy so the API starts cleanly even
before the model checkpoint exists; the endpoint returns a "model_not_trained"
status in that case.
"""
import os
import sys
import threading
from datetime import datetime, timezone

import numpy as np

# Project root on PYTHONPATH in Docker (ENV PYTHONPATH=/app) and in local
# dev (uvicorn runs from project root). Inserting here as a belt-and-suspenders
# guard so the env.data_generator import works from both contexts.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MODEL_PATH = os.environ.get("RL_MODEL_PATH", os.path.join(_ROOT, "rl/checkpoints/ppo_shaped_v2"))
_ACTIONS    = {0: "hold", 1: "reroute_cheap", 2: "reroute_fast"}
_OBS_HIGH   = np.array([200.0, 48.0, 1.0, 720.0, 6.0, 4.0, 500.0, 2.0, 1.0], dtype=np.float32)

_lock: threading.Lock = threading.Lock()
_cache: dict = {}


def _load_model():
    with _lock:
        path = _MODEL_PATH + ".zip"
        if not os.path.exists(path):
            return None
        if _cache.get("path") == path and _cache.get("model") is not None:
            return _cache["model"]
        from stable_baselines3 import PPO
        model = PPO.load(_MODEL_PATH)
        _cache["model"] = model
        _cache["path"]  = path
        print(f"[rl_agent] Loaded model from {path}")
        return model


def _build_obs(shipment: dict) -> np.ndarray:
    from env.data_generator import MODE_TO_INT, STATUS_TO_INT, TIER_TO_INT, _REROUTE_BASE

    now = datetime.now(timezone.utc)

    def _utc(dt):
        if dt is None:
            return now
        if hasattr(dt, "tzinfo") and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    created_at  = _utc(shipment.get("created_at"))
    last_update = _utc(shipment.get("last_update"))

    eta_hours       = float(shipment.get("eta_hours") or 0)
    hours_elapsed   = (now - created_at).total_seconds() / 3600
    eta_overrun     = max(hours_elapsed - eta_hours, 0.0)
    hours_since_upd = (now - last_update).total_seconds() / 3600

    mode    = shipment.get("mode", "truck") or "truck"
    status  = shipment.get("status", "in_transit") or "in_transit"
    tier    = shipment.get("tier", "small") or "small"

    eta_lower   = float(shipment.get("eta_lower") or 0)
    eta_upper   = float(shipment.get("eta_upper") or 0)
    mapie_width = max(eta_upper - eta_lower, 0.0)

    freight_value     = float(shipment.get("freight_value_usd") or 0)
    base              = _REROUTE_BASE.get(mode, 500)
    reroute_cost_norm = min(base * (freight_value / 10_000) / 10_000, 1.0)

    obs = np.array([
        eta_overrun,
        min(hours_since_upd, 48.0),
        float(shipment.get("carrier_status") == "unresponsive"),
        eta_hours,
        float(MODE_TO_INT.get(mode, 0)),
        float(STATUS_TO_INT.get(status, 0)),
        mapie_width,
        float(TIER_TO_INT.get(tier, 0)),
        reroute_cost_norm,
    ], dtype=np.float32).clip(0, _OBS_HIGH)

    return obs


def recommend(shipment: dict) -> dict:
    """
    Returns action recommendation from the trained PPO policy.
    Shipment dict should include DB columns + joined `tier` from customers.

    Returns:
      {"status": "ok", "action": "hold"|"reroute_cheap"|"reroute_fast",
       "action_index": int, "confidence": float, "probabilities": {...},
       "model": str, "obs_features": {...}}
    or
      {"status": "model_not_trained", "model_path": str}
    """
    model = _load_model()
    if model is None:
        return {
            "status":     "model_not_trained",
            "model_path": _MODEL_PATH + ".zip",
        }

    obs = _build_obs(shipment)

    import torch
    obs_t = torch.as_tensor(obs[np.newaxis, :], dtype=torch.float32, device=model.device)
    with torch.no_grad():
        dist  = model.policy.get_distribution(obs_t)
        probs = dist.distribution.probs[0].cpu().numpy().tolist()

    action_idx  = int(np.argmax(probs))
    confidence  = float(probs[action_idx])

    return {
        "status":       "ok",
        "action":       _ACTIONS[action_idx],
        "action_index": action_idx,
        "confidence":   round(confidence, 4),
        "probabilities": {
            "hold":          round(probs[0], 4),
            "reroute_cheap": round(probs[1], 4),
            "reroute_fast":  round(probs[2], 4),
        },
        "model": "ppo_shaped_v2",
        "obs_features": {
            "eta_overrun_hours":       round(float(obs[0]), 2),
            "hours_since_last_update": round(float(obs[1]), 2),
            "is_unresponsive":         int(obs[2]),
            "mapie_interval_width":    round(float(obs[6]), 2),
            "priority_tier":           int(obs[7]),
        },
    }
