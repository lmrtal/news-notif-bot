"""LLM 语义过滤：对规则过滤后的候选新闻做主语/实质/传闻判定 + 一句话摘要。

OpenAI 兼容接口（DeepSeek / BigModel / 任意中转站均可），每轮新闻只调一次、
每次几千 token，成本忽略不计。调用失败时静默降级为纯规则判定，绝不阻塞通知。
"""
import json
import logging
import os
import re
from typing import Dict, List, Optional

import requests

log = logging.getLogger("notif.llm")

PROMPT = """你是新闻编辑。下面是关键词「{query}」搜出的新闻标题列表（带编号）。
对每条判断 kind：
- news：以「{query}」为主体的实质事件（发布/合作/人事/财报/回应/发售/诉讼等）
- rumor：爆料、传闻、未经证实的消息
- junk：以下任何一种：只是顺带提及该公司、观点评论/分析/复盘、股价或资金流向播报、
  导购比价、盘点合辑、标题党水文、与该公司无关
并给 news/rumor 写 summary：不超过40字、只说事件本身的一句话（不说"该文报道了"）。
只输出 JSON 数组：[{{"i":1,"kind":"...","summary":"..."}}]，不要输出任何其他文字。

{items}"""


def llm_classify(items: List[Dict[str, str]], query: str, cfg: dict,
                 timeout: int = 25) -> Optional[Dict[int, Dict[str, str]]]:
    """items: [{title, source}]。返回 {编号: {"kind","summary"}}；未配置或失败返回 None。"""
    api = cfg.get("llm") or {}
    key = os.environ.get("LLM_API_KEY") or api.get("api_key") or ""
    if not api.get("enabled") or not key or not items:
        return None
    base = (api.get("base_url") or "https://api.deepseek.com").rstrip("/")
    model = api.get("model") or "deepseek-chat"
    lines = "\n".join(f'{i}. {it["title"]}（{it.get("source") or "未知"}）'
                      for i, it in enumerate(items, 1))
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(query=query, items=lines)}],
        "temperature": 0.1,
        "max_tokens": 1200,
    }
    for attempt in range(2):
        try:
            r = requests.post(f"{base}/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\[.*\]", text, re.S)
            if not m:
                raise ValueError("响应里没有 JSON 数组")
            arr = json.loads(m.group(0))
            out = {}
            for x in arr:
                if isinstance(x, dict) and "i" in x:
                    out[int(x["i"])] = {"kind": x.get("kind") or "news",
                                        "summary": (x.get("summary") or "")[:60]}
            return out or None
        except Exception as e:
            if attempt:
                log.warning("LLM 复核失败，本轮降级为纯规则: %s", str(e)[:120])
                return None
    return None
