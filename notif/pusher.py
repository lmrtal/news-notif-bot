"""推送通道：ntfy（默认，安卓/iOS 均可）、Bark、Telegram、企业微信群机器人。

点击行为设计：
- 默认（jump=False）：点通知打开推送应用内详情页，直接阅读完整正文；
  通知上带「查看原文」动作按钮，需要时再跳浏览器/APP。
- jump=True：仅用于「开播」这类必须直接跳转外部页面的事件。
- 通道兼容：企业微信/Telegram 不支持动作按钮，链接以纯文本附在正文末尾。

每次推送会追加记录到 pushes.jsonl（控制台「最近推送」数据源，超量自动裁剪）。
"""
import json
import logging
import os
import time
from typing import Dict, List, Optional

import requests

from .paths import DATA_DIR

log = logging.getLogger("notif.push")

_PUSH_LOG = os.path.join(DATA_DIR, "pushes.jsonl")


def _record_push(title: str, body: str, errs: List[str]) -> None:
    try:
        os.makedirs(os.path.dirname(_PUSH_LOG), exist_ok=True)
        if os.path.exists(_PUSH_LOG) and os.path.getsize(_PUSH_LOG) > 400_000:
            keep = open(_PUSH_LOG, encoding="utf-8").read().splitlines()[-300:]
            open(_PUSH_LOG, "w", encoding="utf-8").write("\n".join(keep) + "\n")
        with open(_PUSH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "title": title,
                                "body": body[:200],
                                "ok": not errs, "errs": errs},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


class Notifier:
    def __init__(self, cfg: Dict):
        self.channels: List = []
        p = cfg.get("push") or {}
        n = p.get("ntfy") or {}
        if n.get("enabled", True) and n.get("topic"):
            self.channels.append(("ntfy", n))
        b = p.get("bark") or {}
        if b.get("enabled") and b.get("url"):
            self.channels.append(("bark", b))
        t = p.get("telegram") or {}
        if t.get("enabled") and t.get("bot_token") and t.get("chat_id"):
            self.channels.append(("telegram", t))
        w = p.get("wecom_webhook") or {}
        if w.get("enabled") and w.get("url"):
            self.channels.append(("wecom_webhook", w))

    def notify(self, title: str, body: str, url: Optional[str] = None,
               priority: int = 3, attach: Optional[str] = None,
               jump: bool = False) -> List[str]:
        """priority: ntfy 1~5（4=高亮响铃，2=静默），其余通道忽略。
        jump=True 时点击通知直接打开 url（用于开播直达直播间）。
        返回失败通道列表。"""
        errs = []
        for name, c in self.channels:
            try:
                getattr(self, "_" + name)(c, title, body, url, priority, attach, jump)
            except Exception as e:
                log.warning("推送失败 %s: %s", name, e)
                errs.append(f"{name}: {e}")
        _record_push(title, body, errs)
        return errs

    def _ntfy(self, c, title, body, url, priority, attach, jump):
        payload = {"topic": c["topic"], "title": title, "message": body,
                   "priority": priority, "markdown": True}
        if url and jump:
            payload["click"] = url
        elif url:
            payload["actions"] = [{"action": "view", "label": "查看原文",
                                   "url": url, "clear": False}]
        if attach:
            payload["attach"] = attach
        server = c.get("server", "https://ntfy.sh")
        r = requests.post(server, json=payload, timeout=20)
        if r.status_code >= 400 and attach:
            payload.pop("attach", None)  # 附件抓取失败时降级为纯文本重发
            r = requests.post(server, json=payload, timeout=20)
        r.raise_for_status()

    def _bark(self, c, title, body, url, priority, attach, jump):
        payload = {"title": title,
                   "body": body + (f"\n\n{url}" if url else ""), "group": "监控"}
        if url and jump:
            payload["url"] = url
        r = requests.post(c["url"].rstrip("/"), json=payload, timeout=15)
        r.raise_for_status()

    def _telegram(self, c, title, body, url, priority, attach, jump):
        text = f"<b>{title}</b>\n{body}"
        if url:
            text += f"\n{url}"
        r = requests.post(f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
                          json={"chat_id": c["chat_id"], "text": text,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": False},
                          timeout=15)
        r.raise_for_status()

    def _wecom_webhook(self, c, title, body, url, priority, attach, jump):
        content = f"**{title}**\n{body}"
        if url:
            content += f"\n查看: {url}"
        r = requests.post(c["url"], json={"msgtype": "markdown",
                                          "markdown": {"content": content}},
                          timeout=15)
        r.raise_for_status()
