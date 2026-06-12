"""
Run all three reward configs, compare training curves and action distributions.

Usage (from project root):
    python -m rl.experiments.compare_rewards

Outputs:
    rl/experiments/reward_comparison.png
"""
import os
import sys

import numpy as np
import yaml

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.evaluation import evaluate_policy

from env.freight_env import FreightEnv

CONFIGS = [
    ("sparse",      "rl/configs/sparse.yaml"),
    ("shaped_v1",   "rl/configs/shaped_v1.yaml"),
    ("shaped_v2",   "rl/configs/shaped_v2.yaml"),
]

TIMESTEPS   = 200_000
EVAL_EPS    = 200
ACTION_EVAL = 2_000

_COLORS = ["#e74c3c", "#f39c12", "#2ecc71"]   # red / amber / green


class _EpRewardLogger(BaseCallback):
    def __init__(self):
        super().__init__()
        self.episode_rewards: list[float] = []
        self._buf = None

    def _on_training_start(self):
        self._buf = [0.0] * self.training_env.num_envs

    def _on_step(self) -> bool:
        for i, (r, d) in enumerate(zip(self.locals["rewards"], self.locals["dones"])):
            self._buf[i] += r
            if d:
                self.episode_rewards.append(self._buf[i])
                self._buf[i] = 0.0
        return True


def run_one(name: str, config_path: str) -> dict:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    reward_cfg = cfg["reward"]

    vec_env = make_vec_env(
        lambda: FreightEnv(reward_config=reward_cfg),
        n_envs=4,
    )

    model = PPO(
        "MlpPolicy", vec_env,
        n_steps=512, batch_size=64, n_epochs=10,
        gamma=0.99, learning_rate=3e-4, verbose=0,
    )

    logger = _EpRewardLogger()
    model.learn(total_timesteps=TIMESTEPS, callback=logger)

    eval_env = FreightEnv(reward_config=reward_cfg)
    mean_r, std_r = evaluate_policy(model, eval_env, n_eval_episodes=EVAL_EPS, warn=False)

    # Measure action distribution under deterministic policy
    action_counts = {0: 0, 1: 0, 2: 0}
    obs, _ = eval_env.reset()
    for _ in range(ACTION_EVAL):
        action, _ = model.predict(obs, deterministic=True)
        obs, _, done, _, _ = eval_env.step(int(action))
        action_counts[int(action)] += 1
        if done:
            obs, _ = eval_env.reset()

    print(
        f"  [{name}] mean={mean_r:.3f} ± {std_r:.3f}  "
        f"hold={action_counts[0]/ACTION_EVAL:.1%}  "
        f"cheap={action_counts[1]/ACTION_EVAL:.1%}  "
        f"fast={action_counts[2]/ACTION_EVAL:.1%}"
    )

    return {
        "name":            name,
        "episode_rewards": logger.episode_rewards,
        "mean_reward":     mean_r,
        "std_reward":      std_r,
        "action_dist":     action_counts,
    }


def _smooth(y: list[float], w: int = 60) -> np.ndarray:
    arr = np.asarray(y, dtype=float)
    if len(arr) < w:
        return arr
    return np.convolve(arr, np.ones(w) / w, mode="valid")


def plot_and_save(results: list[dict], out_dir: str = "rl/experiments") -> None:
    os.makedirs(out_dir, exist_ok=True)

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle("Reroute Agent — Reward Config Comparison", fontsize=13)

    # --- Training curves ---
    ax = axes[0]
    for r, c in zip(results, _COLORS):
        ep_r = r["episode_rewards"]
        if ep_r:
            ax.plot(_smooth(ep_r), label=r["name"], color=c, linewidth=2)
    ax.set_title("Training Reward (smoothed, w=60)")
    ax.set_xlabel("Episode")
    ax.set_ylabel("Cumulative episode reward")
    ax.legend()
    ax.grid(alpha=0.3)

    # --- Final eval bar ---
    ax = axes[1]
    names  = [r["name"] for r in results]
    means  = [r["mean_reward"] for r in results]
    stds   = [r["std_reward"]  for r in results]
    ax.bar(names, means, yerr=stds, color=_COLORS, capsize=6, alpha=0.85)
    ax.set_title(f"Eval Performance ({EVAL_EPS} episodes)")
    ax.set_ylabel("Mean reward")
    ax.grid(alpha=0.3, axis="y")

    # --- Action distributions ---
    ax = axes[2]
    x      = np.arange(3)
    width  = 0.25
    labels = ["hold", "reroute_cheap", "reroute_fast"]
    for i, (r, c) in enumerate(zip(results, _COLORS)):
        total = sum(r["action_dist"].values())
        vals  = [r["action_dist"][a] / total for a in range(3)]
        ax.bar(x + i * width, vals, width, label=r["name"], color=c, alpha=0.85)
    ax.set_title(f"Action Distribution (deterministic, {ACTION_EVAL} steps)")
    ax.set_xticks(x + width)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Frequency")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")

    plt.tight_layout()
    out_path = os.path.join(out_dir, "reward_comparison.png")
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    print("Running reward-config comparison experiments (3 × 200k steps)…\n")
    results = []
    for name, cfg_path in CONFIGS:
        print(f"--- {name} ---")
        results.append(run_one(name, cfg_path))
    plot_and_save(results)
