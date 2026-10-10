"""读取 config.json，环境变量覆盖敏感字段（给 GitHub Actions 用）。"""
import json
import os

from .paths import CONFIG_PATH


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("NTFY_TOPIC"):
        cfg.setdefault("push", {}).setdefault("ntfy", {})["topic"] = os.environ["NTFY_TOPIC"]
    if os.environ.get("BILI_SESSDATA"):
        cfg["bili_sessdata"] = os.environ["BILI_SESSDATA"]
    if os.environ.get("BARK_URL"):
        b = cfg.setdefault("push", {}).setdefault("bark", {})
        b.update({"enabled": True, "url": os.environ["BARK_URL"]})
    return cfg
