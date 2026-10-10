"""状态持久化：记录已见过的动态/视频/新闻/直播状态，用于增量判断。"""
import json
import os

from .paths import DATA_DIR, WS_HEARTBEAT, WS_PUSHED

__all__ = ["DATA_DIR", "WS_HEARTBEAT", "WS_PUSHED", "State", "atomic_json_write"]


def atomic_json_write(path: str, obj, indent=1) -> None:
    """先写临时文件再替换，避免写到一半被读成坏 JSON。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    os.replace(tmp, path)


class State:
    def __init__(self, path: str):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def save(self) -> None:
        atomic_json_write(self.path, self.data, indent=1)
