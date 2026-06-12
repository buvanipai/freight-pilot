"""
Train a PPO agent on FreightEnv.

Usage:
    python -m rl.train rl/configs/shaped_v2.yaml
    python -m rl.train rl/configs/sparse.yaml --save-dir rl/checkpoints
"""
import argparse
import os
import sys

import yaml
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback, EvalCallback
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.evaluation import evaluate_policy

# Allow running from project root as `python -m rl.train`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from env.freight_env import FreightEnv


class RewardLogger(BaseCallback):
    """Tracks per-episode rewards for post-training analysis."""

    def __init__(self):
        super().__init__()
        self.episode_rewards: list[float] = []
        self._ep_buf = None

    def _on_training_start(self):
        n = self.training_env.num_envs
        self._ep_buf = [0.0] * n

    def _on_step(self) -> bool:
        for i, (r, done) in enumerate(
            zip(self.locals["rewards"], self.locals["dones"])
        ):
            self._ep_buf[i] += r
            if done:
                self.episode_rewards.append(self._ep_buf[i])
                self._ep_buf[i] = 0.0
        return True


def train(config_path: str, save_dir: str = "rl/checkpoints") -> tuple[PPO, list[float]]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    reward_cfg = cfg["reward"]
    tcfg       = cfg.get("train", {})
    name       = cfg.get("name", "ppo_freight")

    vec_env = make_vec_env(
        lambda: FreightEnv(reward_config=reward_cfg),
        n_envs=tcfg.get("n_envs", 4),
    )

    try:
        import tensorboard  # noqa: F401
        tb_log = os.path.join(save_dir, "tb_logs")
    except ImportError:
        tb_log = None

    model = PPO(
        "MlpPolicy",
        vec_env,
        n_steps=tcfg.get("n_steps", 512),
        batch_size=tcfg.get("batch_size", 64),
        n_epochs=tcfg.get("n_epochs", 10),
        gamma=tcfg.get("gamma", 0.99),
        learning_rate=tcfg.get("learning_rate", 3e-4),
        verbose=1,
        tensorboard_log=tb_log,
    )

    logger = RewardLogger()
    model.learn(
        total_timesteps=tcfg.get("total_timesteps", 200_000),
        callback=logger,
    )

    os.makedirs(save_dir, exist_ok=True)
    model_path = os.path.join(save_dir, name)
    model.save(model_path)

    eval_env = FreightEnv(reward_config=reward_cfg)
    mean_r, std_r = evaluate_policy(model, eval_env, n_eval_episodes=100, warn=False)
    print(f"\n[{name}] eval: mean={mean_r:.3f} std={std_r:.3f}")
    print(f"Model saved → {model_path}.zip")

    return model, logger.episode_rewards


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", default="rl/configs/shaped_v2.yaml")
    parser.add_argument("--save-dir", default="rl/checkpoints")
    args = parser.parse_args()
    train(args.config, args.save_dir)
