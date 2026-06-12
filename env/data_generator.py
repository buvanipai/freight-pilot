"""
Standalone synthetic shipment generator for RL training.
Matches the schema and feature distributions used by the live pipeline
(IsolationForest + MAPIE) without requiring a DB connection.
"""
from dataclasses import dataclass
import numpy as np

MODES = ["truck", "rail", "water", "air", "multimodal", "pipeline", "other"]
STATUSES = ["picked_up", "in_transit", "out_for_delivery", "delivered", "delayed"]
TIERS = ["small", "medium", "large"]

_ETA_RANGE = {
    "truck":      (4,   72),
    "rail":       (24,  168),
    "air":        (2,   24),
    "water":      (72,  720),
    "multimodal": (24,  240),
    "pipeline":   (1,   48),
    "other":      (1,   48),
}

MODE_TO_INT   = {m: i for i, m in enumerate(MODES)}
STATUS_TO_INT = {s: i for i, s in enumerate(STATUSES)}
TIER_TO_INT   = {t: i for i, t in enumerate(TIERS)}

# Rerouting adds these base-cost multiples (scaled by freight value later)
_REROUTE_BASE = {
    "truck":      500,
    "rail":       300,
    "air":        2000,
    "water":      200,
    "multimodal": 800,
    "pipeline":   100,
    "other":      400,
}


@dataclass
class Shipment:
    mode: str
    status: str
    carrier_status: str
    tier: str
    eta_hours: float
    hours_elapsed: float
    hours_since_last_update: float
    freight_value_usd: float
    weight_tons: float
    eta_predicted: float
    eta_lower: float
    eta_upper: float

    # ------------------------------------------------------------------
    # Derived features — mirror the six IsolationForest features + extras
    # ------------------------------------------------------------------
    @property
    def eta_overrun_hours(self) -> float:
        return max(self.hours_elapsed - self.eta_hours, 0.0)

    @property
    def is_unresponsive(self) -> int:
        return 1 if self.carrier_status == "unresponsive" else 0

    @property
    def mode_encoded(self) -> int:
        return MODE_TO_INT.get(self.mode, 0)

    @property
    def status_encoded(self) -> int:
        return STATUS_TO_INT.get(self.status, 0)

    @property
    def priority_tier(self) -> int:
        return TIER_TO_INT.get(self.tier, 0)

    @property
    def mapie_interval_width(self) -> float:
        return max(self.eta_upper - self.eta_lower, 0.0)

    @property
    def reroute_cost_norm(self) -> float:
        """Normalised [0,1] reroute cost signal for the observation vector."""
        base = _REROUTE_BASE.get(self.mode, 500)
        raw = base * (self.freight_value_usd / 10_000)
        return min(raw / 10_000, 1.0)


def generate_shipment(rng: np.random.Generator) -> Shipment:
    mode = rng.choice(
        MODES,
        p=[0.50, 0.15, 0.10, 0.10, 0.10, 0.03, 0.02],
    )
    lo, hi = _ETA_RANGE[mode]
    eta_hours = float(rng.uniform(lo, hi))

    # Status: mostly in_transit, small fractions delayed/unresponsive
    status = str(rng.choice(
        ["picked_up", "in_transit", "out_for_delivery", "delayed"],
        p=[0.10, 0.63, 0.20, 0.07],
    ))
    carrier_status = "unresponsive" if rng.random() < 0.03 else "active"
    tier = str(rng.choice(TIERS, p=[0.60, 0.30, 0.10]))

    hours_elapsed = float(rng.uniform(0, eta_hours * 1.3))
    hours_since_update = float(rng.uniform(0, min(hours_elapsed + 0.1, 12)))

    freight_value = float(np.exp(rng.normal(9.5, 1.5)))   # lognormal ~$13k median
    weight_tons   = float(np.exp(rng.normal(2.0, 1.2)))

    # MAPIE-style conformal prediction: noisy estimate + interval
    eta_predicted = eta_hours * float(rng.uniform(0.75, 1.30))
    interval_half = eta_predicted * float(rng.uniform(0.08, 0.35))
    eta_lower = max(eta_predicted - interval_half, 0.0)
    eta_upper = eta_predicted + interval_half

    return Shipment(
        mode=mode,
        status=status,
        carrier_status=carrier_status,
        tier=tier,
        eta_hours=eta_hours,
        hours_elapsed=hours_elapsed,
        hours_since_last_update=hours_since_update,
        freight_value_usd=freight_value,
        weight_tons=weight_tons,
        eta_predicted=eta_predicted,
        eta_lower=eta_lower,
        eta_upper=eta_upper,
    )
