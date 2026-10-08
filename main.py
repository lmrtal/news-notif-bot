"""B站 UP主监控 + 公司新闻监控 → 推送到手机。

混合部署：本机跑 --scope bili（B站，风控吃国内IP+SESSDATA），
GitHub Actions 跑 --scope news（新闻，云端直连 Google）。

用法：
  python main.py [--scope all|bili|news]   正常运行（增量检查并推送）
  python main.py --force                   忽略检查间隔立即全查
  python main.py --test-push               发送测试通知
  python main.py --reset                   清空状态（下次运行为基线）

结构：main=调度与CLI，checks=各源检查，filters=新闻过滤词表与规则，
bilibili/news/llm/pusher/state=数据与推送层，console=本地控制台，
deploy_cloud=一键同步代码到 GitHub 云端仓库。
运行时文件在 data/（状态/cookie/推送记录），日志在 logs/。
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from bilibili import BiliClient                      # noqa: E402
from checks import (check_dynamics, check_live,      # noqa: E402
                    check_news, check_official,
                    check_rss_feeds, check_videos,
                    clear_failure, flush_fail_warnings,
                    note_failure)
from pusher import Notifier                          # noqa: E402
from state import State                              # noqa: E402

DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")

log = logging.getLogger("notif")

# 各数据源检查间隔（分钟）。B站直播接口宽松可高频；动态接口风控严需低频。
# 定时任务每 2 分钟触发一次，脚本内部按此表决定本轮查什么。
DEFAULT_INTERVALS = {"live": 2, "dyn": 5, "video": 10, "news": 5,
                     "official": 5, "rss": 15}


def load_config() -> dict:
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量优先于配置文件（供 GitHub Actions Secrets 使用）
    if os.environ.get("NTFY_TOPIC"):
        cfg.setdefault("push", {}).setdefault("ntfy", {})["topic"] = os.environ["NTFY_TOPIC"]
    if os.environ.get("BILI_SESSDATA"):
        cfg["bili_sessdata"] = os.environ["BILI_SESSDATA"]
    if os.environ.get("BARK_URL"):
        b = cfg.setdefault("push", {}).setdefault("bark", {})
        b.update({"enabled": True, "url": os.environ["BARK_URL"]})
    return cfg


# ---------- 跨进程锁 ----------

def _acquire_run_lock():
    """手动运行与计划任务重叠时，后启动的实例直接跳过本轮。

    防止两个进程同时读写状态文件互相覆盖。崩溃残留的锁 5 分钟后可被抢占。
    """
    lock = os.path.join(DATA_DIR, "run.lock")
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(int(time.time())).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - float(open(lock, encoding="ascii").read() or 0)
            except Exception:
                age = 1e9
            if age > 300:
                try:
                    os.remove(lock)
                    continue
                except OSError:
                    pass
            return None
    return None


def _release_run_lock(lock) -> None:
    if lock:
        try:
            os.remove(lock)
        except OSError:
            pass


# ---------- 调度 ----------

def run(cfg: dict, force: bool = False, scope: str = "all") -> None:
    """scope: all=全部 | bili=仅B站 | news=仅新闻（云端/本地分工部署用）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    state_file = os.path.join(DATA_DIR, os.environ.get("STATE_FILE") or "state.json")
    st = State(state_file)
    notifier = Notifier(cfg)
    client = None
    if scope in ("all", "bili"):
        client = BiliClient(cfg.get("bili_sessdata") or "",
                            cookie_file=os.path.join(DATA_DIR,
                                                     "bilibili_cookies.json"))
    intervals = dict(DEFAULT_INTERVALS)
    intervals.update(cfg.get("intervals") or {})

    def due(key: str, kind: str) -> bool:
        if force:
            return True
        lc = st.data.setdefault("last_check", {})
        now = time.time()
        iv = intervals.get(kind, 10)
        # 退避：某数据源连续失败(≥12次)时拉长到至少30分钟，减少对被风控IP的
        # 请求压力（动态软风控、Google节点断流时尤其重要），成功后自动恢复
        fails = st.data.get("fail", {})
        if kind == "dyn" and any(k.startswith("bili_动态_")
                                 and int(v.get("count", 0)) >= 12
                                 for k, v in fails.items()):
            iv = max(iv, 30)
        if kind == "news" and any(k.startswith("google:")
                                  and int(v.get("count", 0)) >= 12
                                  for k, v in fails.items()):
            iv = max(iv, 30)
        if now - float(lc.get(key, 0)) >= iv * 60:
            lc[key] = now
            return True
        return False

    if scope in ("all", "bili"):
        for u in cfg.get("bili_users", []):
            uid = str(u.get("uid") or u)
            for kind, name, fn in (("live", "直播", check_live),
                                   ("dyn", "动态", check_dynamics),
                                   ("video", "投稿", check_videos)):
                if not due(f"{kind}:{uid}", kind):
                    continue
                time.sleep(1.5)  # 请求间留间隔，降低风控概率
                try:
                    fn(client, st, notifier, uid, cfg)
                except Exception as e:
                    if kind == "video":
                        # arc/search 接口风控最严(-412 高频出现)；新视频必然出现在
                        # 动态流里，投稿检查只是补充，失败可忽略
                        log.info("UP主 %s 投稿检查失败(已忽略，动态源兜底): %s", uid, e)
                    else:
                        log.warning("UP主 %s %s检查失败: %s", uid, name, e)
                        note_failure(st, f"bili_{name}_{uid}", str(e))
                else:
                    clear_failure(st, f"bili_{name}_{uid}")
    # 跨公司合并：同一轮的多条 🟢/🟡 新闻合并成最多一条通知，
    # 避免「一到某个时间点连环弹几条」；官方🔵与告警🟤不参与合并
    class _NewsQueueNotifier:
        def __init__(self, inner):
            self.inner = inner
            self.queue = []

        def notify(self, title, body, url=None, priority=3, attach=None, jump=False):
            if priority >= 4 or jump or title.startswith(("🔵", "🟤")):
                return self.inner.notify(title, body, url, priority, attach, jump)
            self.queue.append({"title": title, "body": body, "url": url,
                               "priority": priority})
            return []

        def flush(self):
            if len(self.queue) <= 1:
                for it in self.queue:
                    self.inner.notify(it["title"], it["body"], it["url"],
                                      it["priority"])
            elif self.queue:
                lines, url = [], None
                for it in self.queue:
                    if it["url"] and not url:
                        url = it["url"]
                    title = it["title"][2:]          # 去掉行首圆点emoji
                    blines = [l for l in it["body"].split("\n") if l.strip()]
                    if "×" in title:                  # 公司级多条摘要 → 逐条展开
                        label = title.split(" ×")[0]
                        for ln in blines:
                            lines.append(f"▪ {label}｜{ln}")
                    else:                             # 单条 → 标题+来源压成一行
                        head = blines[0] if blines else title
                        src = next((l for l in blines[1:] if l.startswith("来源")), "")
                        src = src.replace("来源:", "").replace("来源：", "").strip()
                        if src:
                            head = f"{head}（{src}）"
                        lines.append(f"▪ {title}｜{head}")
                pr = 2 if all(i["priority"] <= 2 for i in self.queue) else 3
                self.inner.notify(f"🟢 新闻速报（{len(self.queue)}条）",
                                  "\n".join(lines), url=url, priority=pr)
            self.queue = []

    if scope in ("all", "news"):
        qn = _NewsQueueNotifier(notifier)
        for q in cfg.get("news_queries", []):
            if due(f"news:{q}", "news"):
                check_news(st, qn, q, cfg.get("news_strict_match", True), cfg)
        for page in cfg.get("official_pages", []):
            key = page.get("key", "")
            name = page.get("name", key)
            if not key or not due(f"official:{key}", "official"):
                continue
            try:
                check_official(st, notifier, key, name)
            except Exception as e:
                log.warning("%s 官网新闻获取失败: %s", name, e)
                note_failure(st, f"official:{key}", str(e))
            else:
                clear_failure(st, f"official:{key}")
        check_rss_feeds(st, qn, cfg, due)
        qn.flush()
    flush_fail_warnings(st, notifier)
    st.save()
    if client is not None:
        client.save_cookies()


# ---------- CLI ----------

def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if sys.stdout is not None:  # pythonw 运行时无 stdout
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), encoding="utf-8",
        maxBytes=2_000_000, backupCount=1)
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main() -> None:
    ap = argparse.ArgumentParser(description="B站/新闻监控推送")
    ap.add_argument("--test-push", action="store_true", help="发送一条测试通知")
    ap.add_argument("--force", action="store_true",
                    help="忽略各数据源的检查间隔，立即全部检查一遍")
    ap.add_argument("--scope", choices=["all", "bili", "news"], default="all",
                    help="all=全部 | bili=仅B站 | news=仅新闻（混合部署分工用）")
    ap.add_argument("--reset", action="store_true", help="清空状态，下次运行为基线")
    args = ap.parse_args()
    setup_logging()
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = load_config()
    notifier = Notifier(cfg)
    if not notifier.channels:
        log.error("未配置任何推送通道，请编辑 config.json 的 push 段")
        sys.exit(1)
    if args.reset:
        os.makedirs(DATA_DIR, exist_ok=True)
        p = os.path.join(DATA_DIR, os.environ.get("STATE_FILE") or "state.json")
        if os.path.exists(p):
            os.remove(p)
        log.info("已清空状态文件")
        return
    if args.test_push:
        notifier.notify("⚪ 测试通知",
                        "点开这条通知会打开应用内详情页，直接阅读全文。\n"
                        "收到即表示通道正常。", priority=4)
        notifier.notify("⚪ 测试通知（带按钮）",
                        "这条的正文较长，用来验证下拉展开和详情页效果。\n"
                        "通知底部应有「查看原文」按钮，点了才跳浏览器。",
                        url="https://www.zhipuai.cn/zh/news", priority=3)
        log.info("测试通知已发送（共 2 条）")
        return
    lock = _acquire_run_lock()
    if not lock:
        log.info("上一轮检查尚未结束，跳过本轮")
        return
    try:
        run(cfg, force=args.force, scope=args.scope)
        log.info("本次检查完成")
    finally:
        _release_run_lock(lock)


if __name__ == "__main__":
    main()
