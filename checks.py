"""各数据源的增量检查与通知生成。

main.run() 按 due() 调度调用这里的检查函数；每个函数读 State 增量对比后推送。
"""
import datetime
import json
import logging
import os
import re
import time

from bilibili import BiliClient, BiliSoftBlock
from filters import (DEFAULT_RUMOR_WORDS, DEFAULT_TRUSTED_SOURCES,
                     is_market_noise, is_offtopic, similar_title)
from llm import llm_classify, llm_dedup
from news import (fetch_rss, google_news, official_news,
                  zhipu_article_detail)
from state import State

log = logging.getLogger("notif.checks")

MAX_NOTIFY_PER_RUN = 5   # 每类单次最多推送条数，防止状态丢失后刷屏


# ---------- 失败告警 ----------

def note_failure(st: State, key: str, err: str) -> None:
    """只累计连续失败次数；告警统一由 flush_fail_warnings 合并发送。"""
    f = st.data.setdefault("fail", {}).setdefault(key, {"count": 0, "warned": ""})
    f["count"] = int(f.get("count", 0)) + 1


def clear_failure(st: State, key: str) -> None:
    st.data.get("fail", {}).pop(key, None)


def flush_fail_warnings(st: State, notifier) -> None:
    """连续失败约 1 小时（12 次 × 5 分钟）的数据源合并成一条告警。

    全局每天最多发一条（不论多少个数据源先后到达阈值）；
    各源的实时失败详情在控制台看。
    """
    today = datetime.date.today().isoformat()
    if st.data.get("fail_warned_date") == today:
        return  # 今天已经告警过，不再打扰
    bad, keys = [], []
    for key, f in st.data.get("fail", {}).items():
        if int(f.get("count", 0)) >= 12 and f.get("warned") != today:
            bad.append(f"· {key} 连续失败 {f['count']} 次")
            f["warned"] = today
            keys.append(key)
    if bad:
        hint = ""
        if any(k.startswith("google:") for k in keys):
            hint = "\n\nGoogle 相关失败通常是代理节点断流/网络波动，一般会自动恢复。"
        notifier.notify("🟤 数据源异常", "\n".join(bad) + hint, priority=3)
        st.data["fail_warned_date"] = today


# ---------- B站：直播 ----------

_WS_HB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "ws_heartbeat.json")


def _ws_alive() -> bool:
    """直播 WebSocket 常驻进程（live_ws.py）心跳是否新鲜（<3分钟）。
    活着时直播通知由 WS 毫秒级推送，轮询只负责更新状态不重复推。"""
    try:
        hb = json.load(open(_WS_HB, encoding="utf-8"))
        return time.time() - float(hb.get("ts", 0)) < 180
    except Exception:
        return False


def check_live(client: BiliClient, st: State, notifier, uid: str, cfg: dict) -> None:
    brief = client.user_live_brief(uid)
    uname = brief["uname"]
    room_id = brief["room_id"]
    cur = client.room_info(room_id) if room_id else None
    if cur:
        uname = cur.get("uname") or uname
    status = cur["live_status"] if cur else 0
    title = cur["title"] if cur else ""
    prev = st.data.get("live", {}).get(uid)
    live_url = f"https://live.bilibili.com/{room_id}" if room_id else ""
    if prev is None:
        log.info("[%s] 直播状态基线: live_status=%s", uname, status)
    else:
        ps = prev.get("status", 0)
        ws_alive = _ws_alive()
        if ps == 0 and status == 1 and cur:
            if ws_alive:
                log.info("[%s] 开播由WS长连接推送，轮询仅记录", uname)
            else:
                notifier.notify(f"🔴 {uname} 开播了",
                                f"{title}\n分区: {cur.get('area', '')}",
                                url=live_url, priority=4, attach=cur.get("cover"),
                                jump=True)
        elif ps == 0 and status == 2 and cur and not ws_alive:
            notifier.notify(f"🟠 {uname} 轮播中", title, url=live_url,
                            priority=3, jump=True)
        elif ps in (1, 2) and status == 0 and not ws_alive:
            notifier.notify(f"⚫ {uname} 下播了",
                            f"刚结束的直播: {prev.get('title') or '未知'}",
                            url=f"https://space.bilibili.com/{uid}", priority=2)
    st.data.setdefault("live", {})[uid] = {
        "status": status, "title": title, "room_id": room_id, "uname": uname}


# ---------- B站：动态 ----------

def _rsshub_dynamic_fallback(cfg: dict, uid: str) -> list:
    """官方动态接口被风控时，依次尝试 RSSHub 实例兜底；全部失败返回 None。"""
    for base in cfg.get("rsshub_bases", []):
        try:
            rs = fetch_rss(f"{base.rstrip('/')}/bilibili/user/dynamic/{uid}")
            out = []
            for r in rs:
                m = re.search(r"/(\d+)", r.get("link") or "")
                if not m:
                    continue
                out.append({"id": m.group(1), "kind": "动态",
                            "text": (r.get("title") or "")[:80], "author": "",
                            "bvid": "", "url": r.get("link") or ""})
            if out:
                log.info("动态接口失败，RSSHub 兜底成功(%s)", base)
                return out
        except Exception:
            continue
    return None


def check_dynamics(client: BiliClient, st: State, notifier, uid: str, cfg: dict) -> None:
    try:
        items = client.dynamics(uid)
    except BiliSoftBlock:
        # 软风控：更换 buvid3 设备指纹重试（30 分钟最多一次），仍不行走 RSSHub 兜底
        items = []
        if time.time() - st.data.get("rotate_ts", 0) > 1800:
            st.data["rotate_ts"] = time.time()
            try:
                client.rotate_device()
                items = client.dynamics(uid)
                log.info("更换设备指纹后动态恢复，本轮 %d 条", len(items))
            except Exception as e:
                log.warning("更换设备指纹重试仍失败: %s", e)
        if not items:
            items = _rsshub_dynamic_fallback(cfg, uid)
            if items is None:
                raise
    except Exception:
        items = _rsshub_dynamic_fallback(cfg, uid)
        if items is None:
            raise
    author = next((i["author"] for i in items if i.get("author")), "") or uid
    dyn = st.data.setdefault("dyn", {})
    first = uid not in dyn or not dyn.get(uid)
    seen = dyn.setdefault(uid, [])
    ids = [i["id"] for i in items if i.get("id")]
    if first:
        seen[:] = ids[:30]
        log.info("[%s] 动态基线: %d 条", author, len(seen))
        return
    new = [i for i in items if i["id"] not in seen][:MAX_NOTIFY_PER_RUN]
    vseen = st.data.setdefault("video", {}).setdefault(uid, [])
    fwd_seen = st.data.setdefault("fwd_orig", {}).setdefault(uid, [])
    for it in new:
        if not it.get("kind"):
            continue
        if it["kind"] == "转发动态" and it.get("orig"):
            # 转发自己的动态（内容已通知过）或重复转发同一条原动态：只记不发，
            # 同一个内容最多 ping 一次
            if it.get("orig_self") or it["orig"] in fwd_seen:
                if it["orig"] not in fwd_seen:
                    fwd_seen.insert(0, it["orig"])
                    del fwd_seen[60:]
                continue
            fwd_seen.insert(0, it["orig"])
            del fwd_seen[60:]
        if it["kind"] == "投稿视频" and it.get("bvid"):
            if it["bvid"] in vseen:  # 与投稿监控互通，同一条视频只通知一次
                continue
            vseen.insert(0, it["bvid"])
        notifier.notify(f"🟣 {it['author'] or author} 发了{it['kind']}",
                        it.get("text") or "(无文字内容)",
                        url=it.get("url") or None, priority=3)
    seen[:] = list(dict.fromkeys(ids + seen))[:80]


# ---------- B站：投稿 ----------

def check_videos(client: BiliClient, st: State, notifier, uid: str, cfg: dict) -> None:
    items = client.videos(uid)
    uname = (st.data.get("live", {}).get(uid) or {}).get("uname") or uid
    vids = st.data.setdefault("video", {})
    first = uid not in vids or not vids.get(uid)
    seen = vids.setdefault(uid, [])
    bvids = [v["bvid"] for v in items if v.get("bvid")]
    if first:
        seen[:] = bvids[:20]
        log.info("[%s] 投稿基线: %d 条", uname, len(seen))
        return
    new = [v for v in items if v["bvid"] and v["bvid"] not in seen][:MAX_NOTIFY_PER_RUN]
    for v in new:
        body = v["title"] + (f"\n时长: {v['length']}" if v.get("length") else "")
        notifier.notify(f"🟣 {uname} 投稿了新视频", body, url=v["url"], priority=3)
    seen[:] = list(dict.fromkeys(bvids + seen))[:60]


# ---------- 新闻 ----------

def _ingest_news(st: State, notifier, key: str, label: str, items: list) -> None:
    nmap = st.data.setdefault("news", {})
    first = key not in nmap or not nmap.get(key)
    seen = nmap.setdefault(key, [])
    links = [i["link"] for i in items]
    if first:
        seen[:] = links[:60]
        log.info("[新闻] 基线 %s: %d 条", key, len(seen))
        return
    new = [i for i in items if i["link"] and i["link"] not in seen][:MAX_NOTIFY_PER_RUN]
    for it in new:
        notifier.notify(f"🟢 {label}", it["title"], url=it["link"], priority=3)
    seen[:] = list(dict.fromkeys(links + seen))[:200]


def check_news(st: State, notifier, query: str, strict: bool = True,
               cfg: dict = None) -> None:
    cfg = cfg or {}
    key = f"google:{query}"
    try:
        items = google_news(query)
    except Exception as e:
        log.warning("%s 获取失败: %s", key, e)
        note_failure(st, key, str(e))
        return
    clear_failure(st, key)

    rumor_words = cfg.get("rumor_words") or DEFAULT_RUMOR_WORDS
    trusted = cfg.get("trusted_sources") or DEFAULT_TRUSTED_SOURCES
    rumor_action = cfg.get("rumor_action", "tag")  # tag=标记降级推送 / drop=丢弃

    nmap = st.data.setdefault("news", {})
    first = key not in nmap or not nmap.get(key)
    seen = nmap.setdefault(key, [])
    links = [i["link"] for i in items]
    if first:
        # 基线取大一点，覆盖整个结果集——Google News 结果集会轮换，
        # 只记前几条的话，排位靠后的文章轮换上来会被误判为"新"
        seen[:] = links[:120]
        log.info("[新闻] 基线 %s: %d 条", key, len(seen))
        return
    new = [i for i in items if i["link"] and i["link"] not in seen]
    if strict:
        # 严格模式：标题必须包含关键词，过滤搜索源带进来的无关新闻
        new = [i for i in new if any(t and t in i["title"] for t in query.split())]

    dropped = 0
    normals, rumors = [], []
    for it in new:
        title, src = it["title"], it.get("source") or ""
        if is_market_noise(title, cfg):
            dropped += 1
            log.info("[新闻] 过滤行情/转载噪音: %s", title[:60])
            continue
        if is_offtopic(title, query, cfg):
            dropped += 1
            log.info("[新闻] 过滤非主语/合辑/软文: %s", title[:60])
            continue
        if any(w in title for w in rumor_words):
            if rumor_action == "drop":
                dropped += 1
                continue
            rumors.append((it, title, src))
        else:
            normals.append((it, title, src))

    # LLM 语义复核：规则漏网的顺带提及/评论稿在这里杀掉，并生成一句话摘要。
    # verdict 为 None（未配置/接口失败）时自动降级为纯规则结果。
    if normals:
        verdict = llm_classify(
            [{"title": t, "source": s} for _, t, s in normals[:12]], query, cfg)
        if verdict:
            kept = []
            for idx, (it, title, src) in enumerate(normals[:12], 1):
                v = verdict.get(idx) or {}
                it["summary"] = v.get("summary") or ""
                if v.get("kind") == "junk":
                    dropped += 1
                    log.info("[新闻] LLM判定丢弃: %s", title[:60])
                    continue
                if v.get("kind") == "rumor":
                    rumors.append((it, title, src))
                    continue
                kept.append((it, title, src))
            normals = kept + normals[12:]

    # 同事件去重（三层）：词面相似度 + LLM 语义判定，正式新闻和传闻都过闸
    recent = st.data.setdefault("news_recent", {}).setdefault(key, [])
    now = time.time()
    recent[:] = [r for r in recent if now - r.get("t", 0) < 48 * 3600]

    # 第一层：LLM 语义去重——识别措辞完全不同但同一事件的转载/跟进
    # （传闻尤其需要：爆料被各家改写得面目全非，词面相似度抓不住）
    all_cands = [t for _, t, _ in normals] + [t for _, t, _ in rumors]
    if all_cands and recent:
        recent_titles = [r["title"] for r in recent]
        dupset = llm_dedup(all_cands, recent_titles, cfg)
        if dupset:
            n0, r0 = len(normals), len(rumors)
            normals = [x for i, x in enumerate(normals) if i not in dupset]
            rumors = [x for i, x in enumerate(rumors)
                      if n0 + i not in dupset]
            killed = n0 + r0 - len(normals) - len(rumors)
            dropped += killed
            log.info("[新闻] LLM语义去重 %d 条", killed)

    # 第二层：词面相似度去重（对最近已推 + 本轮已保留）
    def _dup_of(title, kept):
        return (any(similar_title(title, r["title"]) for r in recent)
                or any(similar_title(title, u[1]) for u in kept))

    uniq, dup = [], 0
    for it, title, src in normals:
        if _dup_of(title, uniq):
            dup += 1
            log.info("[新闻] 过滤同事件重复报道: %s", title[:60])
            continue
        uniq.append((it, title, src))
    uniq_r = []
    for it, title, src in rumors:
        if _dup_of(title, uniq + uniq_r):
            dup += 1
            log.info("[新闻] 过滤重复传闻: %s", title[:60])
            continue
        uniq_r.append((it, title, src))

    # 推送：传闻单独静默推；正常新闻 1条=原格式，多条=合并成一条摘要，
    # 避免同一轮冒出连环通知
    for it, title, src in uniq_r[:MAX_NOTIFY_PER_RUN]:
        body = title
        if it.get("summary"):
            body += f"\n{it['summary']}"
        body += f"\n来源: {src or '谷歌聚合'}"
        notifier.notify(f"🟡 {query} 传闻（未经证实）", body,
                        url=it["link"], priority=2)
        recent.append({"t": now, "title": title})
    if len(uniq) == 1:
        it, title, src = uniq[0]
        mark = "·权威" if any(t in src for t in trusted) else ""
        body = title
        if it.get("summary"):
            body += f"\n{it['summary']}"
        body += f"\n来源: {src or '谷歌聚合'}{mark}"
        notifier.notify(f"🟢 {query} 新闻", body, url=it["link"], priority=3)
        recent.append({"t": now, "title": title})
    elif uniq:
        batch = uniq[:MAX_NOTIFY_PER_RUN + 3]
        lines = []
        for i, (it, title, src) in enumerate(batch, 1):
            mark = "·权威" if any(t in src for t in trusted) else ""
            text = (it.get("summary") or title)
            lines.append(f"{i}. {text}（{src or '谷歌聚合'}{mark}）")
        notifier.notify(f"🟢 {query} 新闻 ×{len(batch)}", "\n".join(lines),
                        url=batch[0][0]["link"], priority=3)
        for it, title, src in batch:
            recent.append({"t": now, "title": title})
    del recent[:-40]
    if dup:
        log.info("[新闻] %s 本轮同事件重复 %d 条已合并", query, dup)
    if dropped:
        log.info("[新闻] %s 本轮过滤噪音 %d 条", query, dropped)
    seen[:] = list(dict.fromkeys(links + seen))[:300]


def check_rss_feeds(st: State, notifier, cfg: dict, due=None) -> None:
    for feed in cfg.get("rss_feeds", []):
        if isinstance(feed, str):
            url, name = feed, feed
        else:
            url = feed.get("url", "")
            name = feed.get("name", url)
        if not url:
            continue
        if due and not due(f"rss:{url}", "rss"):
            continue
        key = f"rss:{url}"
        try:
            items = fetch_rss(url)
        except Exception as e:
            log.warning("%s 获取失败: %s", key, e)
            note_failure(st, key, str(e))
            continue
        clear_failure(st, key)
        _ingest_news(st, notifier, key, name, items)


# ---------- 官网一手源 ----------

def check_official(st: State, notifier, key: str, name: str) -> None:
    """公司官网一手信源，最高优先级推送。

    通知即详情：列表页带摘要的直接进正文（DeepSeek）；
    不带的抓详情页取正文摘录或首图（智谱）。
    """
    items = official_news(key)
    skey = f"official:{key}"
    nmap = st.data.setdefault("news", {})
    first = skey not in nmap or not nmap.get(skey)
    seen = nmap.setdefault(skey, [])
    ids = [i["id"] for i in items]
    if first:
        seen[:] = ids[:40]
        log.info("[%s] 基线: %d 条官方新闻", name, len(seen))
        return
    new = [i for i in items if i["id"] not in seen][:MAX_NOTIFY_PER_RUN]
    for it in new:
        body, attach = it["title"], None
        if it.get("text"):
            body = f"{it['title']}\n\n{it['text']}"
        if it.get("image"):  # 列表页/Feed 自带配图（Apple/DeepSeek）
            attach = it["image"] or None
        elif key == "zhipu":  # 智谱列表页没有，需进详情页取正文/首图
            try:
                text, image = zhipu_article_detail(it["link"], it["title"])
                if text:
                    body = f"{it['title']}\n\n{text}"
                attach = image or None
            except Exception:
                pass  # 详情抓不到就只推标题，不影响通知本身
        body += f"\n\n来源: {name}"
        notifier.notify(f"🔵 {name}", body, url=it["link"],
                        priority=4, attach=attach)
    seen[:] = list(dict.fromkeys(ids + seen))[:100]
