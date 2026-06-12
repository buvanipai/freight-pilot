"""
FreightEnv — Gymnasium environment for the Reroute Agent.

Observation (9-dim float32):
  [0] eta_overrun_hours          — IsolationForest feature 1
  [1] hours_since_last_update    — IsolationForest feature 2
  [2] is_unresponsive            — IsolationForest feature 3
  [3] eta_hours                  — IsolationForest feature 4
  [4] mode_encoded               — IsolationForest feature 5
  [5] status_encoded             — IsolationForest feature 6
  [6] mapie_interval_width       — MAPIE uncertainty signal (eta_upper - eta_lower)
  [7] priority_tier              — customer tier (0=small, 1=medium, 2=large)
  [8] reroute_cost_norm          — normalised reroute cost estimate [0, 1]

Actions:
  0 — hold          (no change, zero cost)
  1 — reroute_cheap (ETA × 0.90, $300 cost)
  2 — reroute_fast  (ETA × 0.60, $1 200 cost)

Episode: one shipment, up to MAX_STEPS=8 decision periods (4 hours each).
Terminates early on simulated delivery (hours_elapsed >= eta_hours).
"""

import numpy as np
import gymnasium as gym
from gymnasium import spaces

from env.data_generator import generate_shipment

# Action → (eta_multiplier, cost_usd)
_ACTION_EFFECTS = {
    0: (1.00,    0),   # hold
    1: (0.90,  300),   # reroute_cheap
    2: (0.60, 1200),   # reroute_fast
}

MAX_STEPS  = 8
TIME_STEP  = 4.0   # hours per decision period

# Observation upper bounds (used to define the Box space)
_OBS_HIGH = np.array(
    [200.0, 48.0, 1.0, 720.0, 6.0, 4.0, 500.0, 2.0, 1.0],
    dtype=np.float32,
)


class FreightEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, reward_config: dict | None = None, seed: int = 42):
        super().__init__()
        self.reward_config = reward_config or {}
        self._rng = np.random.default_rng(seed)

        self.observation_space = spaces.Box(
            low=np.zeros(9, dtype=np.float32),
            high=_OBS_HIGH,
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(3)

        self._shipment  = None
        self._step_count = 0

    # ------------------------------------------------------------------
    # Gymnasium interface
    # ------------------------------------------------------------------

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        self._shipment   = generate_shipment(self._rng)
        self._step_count = 0
        return self._obs(), {}

    def step(self, action: int):
        s   = self._shipment
        cfg = self.reward_config

        eta_mult, cost_usd = _ACTION_EFFECTS[int(action)]
        pre_overrun = s.eta_overrun_hours   # capture before applying action

        # Apply rerouting: shorten ETA if action > 0
        if action > 0:
            s.eta_hours *= eta_mult

        # Advance simulation clock
        s.hours_elapsed           += TIME_STEP
        s.hours_since_last_update  = max(0.0, s.hours_since_last_update - TIME_STEP * 0.5)

        reward = self._reward(action, cost_usd, pre_overrun)

        self._step_count += 1
        delivered = s.hours_elapsed >= s.eta_hours
        done      = delivered or (self._step_count >= MAX_STEPS)

        if done:
            on_time = s.eta_overrun_hours < 1.0
            terminal = (
                cfg.get("on_time_bonus", 5.0)
                if on_time
                else -cfg.get("late_penalty", 3.0)
            )
            reward += terminal

        return self._obs(), reward, done, False, {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _obs(self) -> np.ndarray:
        s = self._shipment
        return np.array([
            s.eta_overrun_hours,
            s.hours_since_last_update,
            float(s.is_unresponsive),
            s.eta_hours,
            float(s.mode_encoded),
            float(s.status_encoded),
            s.mapie_interval_width,
            float(s.priority_tier),
            s.reroute_cost_norm,
        ], dtype=np.float32).clip(0, _OBS_HIGH)

    def _reward(self, action: int, cost_usd: float, pre_overrun: float) -> float:
        s   = self._shipment
        cfg = self.reward_config
        rtype = cfg.get("type", "shaped_v2")

        if rtype == "sparse":
            # All signal deferred to terminal step
            return 0.0

        overrun_pen     = -s.eta_overrun_hours * cfg.get("overrun_weight", 0.5)
        uncertainty_pen = -s.mapie_interval_width * cfg.get("uncertainty_weight", 0.1)

        if rtype == "shaped_v1":
            # cost_weight intentionally weak — see experiments/README.md
            cost_pen = -cost_usd * cfg.get("cost_weight", 0.0001)
            return overrun_pen + cost_pen + uncertainty_pen

        if rtype == "shaped_v2":
            cost_pen = -cost_usd * cfg.get("cost_weight", 0.001)
            # Penalise rerouting when shipment is already on track.
            # tier_factor amplifies penalty for low-priority (small) loads.
            tier_factor = 1.0 + (2 - s.priority_tier) * 0.5
            unnecessary = (
                float(action > 0 and pre_overrun < 0.5)
                * cfg.get("unnecessary_reroute_penalty", 2.0)
                * tier_factor
            )
            return overrun_pen + cost_pen + uncertainty_pen - unnecessary

        raise ValueError(f"Unknown reward type: {rtype!r}")
