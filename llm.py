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
并给 news/rumor 写 summary——要求精确完整、无需点开原文：
把标题信息改写成一句自包含的陈述句（≤60字），必须保留标题中出现的
所有数字、日期、金额、百分比、型号、产品名；不得用"新机/该产品/该公司"
等模糊指代；标题中没有的信息不得编造；标题过于模糊无具体信息时写
"（标题未提供细节）"。
只输出 JSON 数组：[{{"i":1,"kind":"...","summary":"..."}}]，不要输出任何其他文字。

{items}"""


DEDUP_PROMPT = """以下是最近已推送过的新闻标题：
{recent}

以下是候选新标题（编号A/B/C…）：
{cands}

判断：哪些候选满足以下任一条件——
1) 与任一已推送标题报道的是同一事件（新措辞/转载/媒体跟进；有实质性新进展的算不同事件，应保留）
2) 与其他候选彼此是同一事件（只保留编号靠前的那个）
只输出 JSON：{{"dup":["A","C"]}}（dup=应丢弃的编号）；没有则输出 {{"dup":[]}}，不要输出其他文字。"""


def _message_text(msg: dict) -> str:
    """思考型模型有时把 JSON 留在 reasoning_content，正文是空的。"""
    text = (msg.get("content") or "").strip()
    if text:
        return text
    return (msg.get("reasoning_content") or "").strip()


def _chat(cfg: dict, content: str, timeout: int) -> Optional[str]:
    """调一次 OpenAI 兼容接口。未配置返回 None；调用失败抛异常给上层重试。"""
    api = cfg.get("llm") or {}
    key = os.environ.get("LLM_API_KEY") or api.get("api_key") or ""
    if not api.get("enabled") or not key:
        return None
    base = (api.get("base_url") or "https://api.deepseek.com").rstrip("/")
    model = api.get("model") or "deepseek-chat"
    # /no_think：DeepSeek-flash 不加这句会把 token 耗在自言自语上，不出 JSON
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content.rstrip() + "\n/no_think"}],
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    r = requests.post(f"{base}/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json=body, timeout=timeout)
    r.raise_for_status()
    return _message_text(r.json()["choices"][0]["message"])


def llm_dedup(titles: List[str], recent_titles: List[str], cfg: dict,
              timeout: int = 25) -> Optional[set]:
    """语义去重：识别措辞完全不同但报道同一事件的候选（对最近已推 + 批内彼此）。

    titles: 候选标题列表；recent_titles: 最近已推送标题（新→旧或旧→新均可，
    内部取最新的 15 条参与比对）。
    返回应丢弃的候选下标集合；未配置/失败返回 None（降级为词面相似度）。
    """
    if not titles or not recent_titles:
        return None
    letters = [chr(ord("A") + i) for i in range(len(titles))]
    content = DEDUP_PROMPT.format(
        recent="\n".join(f"{i}. {t}" for i, t in
                         enumerate(recent_titles[-15:], 1)),  # 取最新15条
        cands="\n".join(f"{L}. {t}" for L, t in zip(letters, titles)))
    for attempt in range(2):
        try:
            text = _chat(cfg, content, timeout)
            if text is None:
                return None
            m = re.search(r"\{[^{}]*\"dup\"[^{}]*\}", text, re.S)
            if not m:
                raise ValueError("无JSON: " + text[:80].replace("\n", " "))
            dup = json.loads(m.group(0)).get("dup") or []
            return {letters.index(x) for x in dup if x in letters}
        except Exception as e:
            if attempt:
                log.warning("LLM 去重失败，本轮只用词面相似度: %s", str(e)[:120])
                return None
    return None


def llm_classify(items: List[Dict[str, str]], query: str, cfg: dict,
                 timeout: int = 25) -> Optional[Dict[int, Dict[str, str]]]:
    """items: [{title, source}]。返回 {编号: {"kind","summary"}}；未配置或失败返回 None。"""
    if not items:
        return None
    lines = "\n".join(f'{i}. {it["title"]}（{it.get("source") or "未知"}）'
                      for i, it in enumerate(items, 1))
    content = PROMPT.format(query=query, items=lines)
    for attempt in range(2):
        try:
            text = _chat(cfg, content, timeout)
            if text is None:
                return None
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
