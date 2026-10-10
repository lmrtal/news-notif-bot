"""项目根目录上的路径。包可以挪动，data/、logs/、config.json 仍在仓库根。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(ROOT, "logs")
CONFIG_PATH = os.path.join(ROOT, "config.json")
WS_HEARTBEAT = os.path.join(DATA_DIR, "ws_heartbeat.json")
WS_PUSHED = os.path.join(DATA_DIR, "ws_pushed.json")
