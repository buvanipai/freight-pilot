# Reroute Agent — Reward Experiments

Three reward configurations are compared to understand how reward shaping
affects policy quality. Run the full comparison with:

```bash
python -m rl.experiments.compare_rewards
# → writes rl/experiments/reward_comparison.png
```

---

## Configs

| Config | File | Key difference |
|--------|------|----------------|
| `sparse` | `rl/configs/sparse.yaml` | Signal only at episode end |
| `shaped_v1` | `rl/configs/shaped_v1.yaml` | Dense shaping, weak cost term |
| `shaped_v2` | `rl/configs/shaped_v2.yaml` | Dense shaping, balanced penalties |

---

## Sparse reward

**What we tried:** reward 0 every step; +10 if on time at episode end, −5 if late.

**Result:** Policy learns slowly (sparse signal over max 8 steps makes credit
assignment hard). Agent converges to a conservative *hold* policy — safe but
suboptimal for genuinely delayed high-value shipments.

---

## Shaped v1 — what failed (reward hacking)

**Reward at each step:**

```
R = −overrun × 0.5  −  cost × 0.0001  −  interval_width × 0.1
```

**The hacking behaviour:**
`cost_weight = 0.0001` makes a `reroute_fast` action (costing \$1 200) worth
only −0.12 per step. Meanwhile a single overrun hour already costs −0.5.
The agent quickly learns that *always rerouting fast* virtually eliminates
the overrun penalty while paying a negligible cost penalty.

In practice this produced an action distribution of roughly:

```
hold ≈ 5%   reroute_cheap ≈ 10%   reroute_fast ≈ 85%
```

The agent indiscriminately rerouted every shipment — including ones that were
already on track — because the cost signal was too weak to discourage it.
Mean eval reward was deceptively high (the overrun penalty vanished) but real
operational cost would be catastrophic.

---

## Shaped v2 — fix

Two changes close the hacking loop:

1. **`cost_weight = 0.001`** (10× stronger): `reroute_fast` now costs −1.20
   per step, making it comparable to a 2.4-hour overrun. The agent must weigh
   cost against genuine delay reduction.

2. **`unnecessary_reroute_penalty = 2.0`** (scaled by `tier_factor`):
   When the agent reroutes a shipment whose overrun is < 0.5 h, an extra
   penalty is applied. `tier_factor = 1 + (2 − priority_tier) × 0.5`
   amplifies this for low-priority (small-customer) loads where unnecessary
   rerouting is hardest to justify.

**Resulting policy:**

```
hold ≈ 55%   reroute_cheap ≈ 30%   reroute_fast ≈ 15%
```

The agent now holds when a shipment is on track, uses cheap rerouting for
moderate delays, and reserves fast rerouting for high-value/high-tier
exceptions — matching the intended business logic.

---

## Takeaway

| Failure mode | Root cause | Fix |
|---|---|---|
| Under-learning | Sparse signal, long horizon | Add dense shaping |
| Over-rerouting | Cost penalty too weak relative to overrun penalty | Balance `cost_weight` to same order as overrun term |
| Indiscriminate rerouting | No penalty for unnecessary actions | Add state-conditioned action penalty |

The **MAPIE interval width** (conformal uncertainty) is a genuine signal: the
agent in v2 prefers to reroute when prediction uncertainty is high (wide
interval → model unsure the shipment will arrive on time) and holds when
the interval is tight. This mirrors how a human operator uses confidence
bounds to decide whether to act.
