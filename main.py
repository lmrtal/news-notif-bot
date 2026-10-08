"""B站 UP主监控 + 公司新闻监控 → 推送到手机。

用法：
  python main.py             正常运行一次（增量检查并推送新内容）
  python main.py --test-push 发送一条测试通知
  python main.py --reset     清空已记录状态（下次运行为基线，不推送）

首次运行会把当前已有的动态/投稿/新闻记为基线，只推送之后的新内容。
"""
import argparse
import datetime
import json
import logging
import logging.handlers
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from bilibili import BiliClient, BiliSoftBlock       # noqa: E402
from llm import llm_classify                        # noqa: E402
from news import (fetch_rss, google_news,             # noqa: E402
                  official_news, zhipu_article_detail)  # noqa: E402
from pusher import Notifier                          # noqa: E402
from state import State                              # noqa: E402

log = logging.getLogger("notif")

MAX_NOTIFY_PER_RUN = 5   # 每类单次最多推送条数，防止状态丢失后刷屏

# 各数据源检查间隔（分钟）。B站直播接口宽松可高频；动态接口风控严需低频。
# 定时任务每 2 分钟触发一次，脚本内部按此表决定本轮查什么。
DEFAULT_INTERVALS = {"live": 2, "dyn": 5, "video": 10, "news": 5,
                     "official": 5, "rss": 15}

# 标题命中即标记为「传闻」（未经证实），低优先级静默推送，不会被误导
DEFAULT_RUMOR_WORDS = ["爆料", "传闻", "据传", "网传", "消息人士", "知情人士",
                       "被曝", "疑似", "泄露", "流出", "或将"]

# Google News 条目自带的来源名，命中即标注「权威」
DEFAULT_TRUSTED_SOURCES = [
    "财新", "财联社", "证券时报", "上海证券报", "中国证券报", "界面", "澎湃",
    "新华", "人民日报", "中新网", "第一财经", "每日经济新闻", "21世纪经济报道",
    "科创板日报", "36氪", "虎嗅", "钛媒体", "智东西", "量子位", "机器之心",
    "新浪科技", "腾讯科技", "网易科技", "TechWeb", "雷峰网", "IT之家",
]

# 行情噪音过滤：标题含行情词但不含任何实质动作词 => 盘中涨跌播报，丢弃
# （「智谱宣布整改完成，股价拉升翻红」这类有实质内容的会因含动作词而保留）
DEFAULT_MARKET_WORDS = [
    "收评", "午盘", "盘前", "早盘", "高开", "低开", "涨超", "跌超", "涨逾",
    "跌逾", "大涨", "大跌", "拉升", "翻红", "走低", "走高", "创新高", "创新低",
    "涨停", "跌停", "市值", "股价", "恒指", "科指", "港股通", "涨幅", "跌幅",
    "领涨", "领跌", "异动",
    # 价格对比/导购格调（无实质事件的评论稿）
    "起售价", "售价", "定价", "对比", "对标", "吊打", "碾压", "值得买",
    "值不值", "怎么选", "选谁",
]
DEFAULT_SUBSTANCE_WORDS = [
    "发布", "上线", "上架", "宣布", "推出", "开源", "回应", "公告", "业绩",
    "财报", "融资", "合作", "任命", "收购", "新品", "发布会", "整改", "停牌",
    "复牌", "战略", "签约", "重组", "投资", "成立", "突破", "评测", "实测",
    "体验", "招募", "降价", "免费", "涨价", "计划", "披露", "澄清", "否认",
    "证实", "入驻", "接入", "入选", "认证",
    "国行", "发售", "开售", "预售", "上市", "首发", "政策", "调整",
    "败诉", "胜诉", "诉讼", "裁决", "判赔", "赔偿",
]


# 速览合辑 / SEO软文 / 类比框架 / 流量标题党的特征词，命中即丢弃
# （速览类已由「丨/；」分隔符规则覆盖，这里不再收"日报/早报"等词，
#   否则会误杀"北京日报社与XX合作"这类真新闻）
DIGEST_KILL_WORDS = ("盘点", "榜单", "服务商", "沽空", "净买入", "净卖出",
                     "情报站", "美版", "中国版", "平替", "翻版", "冒牌",
                     "王炸", "大招", "抢到赚到", "手慢无", "速抢", "炸裂",
                     "逆天", "杀疯", "真香")

# 同一事件被多家媒体报道的标题相似度阈值（字符二元组 Jaccard）
NEWS_DUP_THRESHOLD = 0.5


def _bigrams(s: str) -> set:
    s = re.sub(r"[\s!！？?：:，,。.、()（）·|｜；;\"'‘’“”]+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar_title(a: str, b: str) -> bool:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return False
    return len(A & B) / len(A | B) >= NEWS_DUP_THRESHOLD


def is_market_noise(title: str, cfg: dict) -> bool:
    """盘中行情播报（智谱涨超X%之类）和转载站关键词堆砌标题（含≥3个竖线）。"""
    if cfg.get("news_market_filter", True):
        market = cfg.get("market_words") or DEFAULT_MARKET_WORDS
        substance = cfg.get("substance_words") or DEFAULT_SUBSTANCE_WORDS
        if any(w in title for w in market) and not any(w in title for w in substance):
            return True
    # "xxx|北京商报|百度百科|大模型|智谱华章|手机新浪网"式堆砌转载标题
    return title.count("|") + title.count("｜") >= 3


def is_offtopic(title: str, query: str, cfg: dict) -> bool:
    """关键词不是标题主语：速览合辑、类比比喻（"美版X"/"X时刻"）、
    顺带点名（出现在后半句）、SEO软文 → 丢弃。"""
    if not cfg.get("news_subject_filter", True):
        return False
    tokens = [t for t in query.split() if t]
    if not tokens:
        return False
    # 速览合辑：丨分隔、分号串联多条；软文/流量词（可用 config 的 digest_kill_words 覆盖）
    if "丨" in title or "；" in title or ";" in title:
        return True
    kill_words = cfg.get("digest_kill_words") or DIGEST_KILL_WORDS
    if any(w in title for w in kill_words):
        return True
    # 类比/比喻用法："DeepSeek时刻"、"美版 DeepSeek"（忽略空格）
    nospace = title.replace(" ", "")
    if any(f"{t}时刻" in nospace for t in tokens):
        return True
    # 主语判定：前10字符内首次出现、或出现≥2次、或含实质动作词
    if any(t in title[:10] for t in tokens):
        return False
    if sum(title.count(t) for t in tokens) >= 2:
        return False
    substance = cfg.get("substance_words") or DEFAULT_SUBSTANCE_WORDS
    return not any(w in title for w in substance)


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


def note_failure(st: State, key: str, err: str) -> None:
    """只累计连续失败次数；告警统一由 flush_fail_warnings 合并发送。"""
    f = st.data.setdefault("fail", {}).setdefault(key, {"count": 0, "warned": ""})
    f["count"] = int(f.get("count", 0)) + 1


def flush_fail_warnings(st: State, notifier: Notifier) -> None:
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


def clear_failure(st: State, key: str) -> None:
    st.data.get("fail", {}).pop(key, None)


def _acquire_run_lock():
    """跨进程锁：手动运行与计划任务重叠时，后启动的实例直接跳过本轮。

    防止两个进程同时读写 state.json 互相覆盖（会丢基线/产生重复通知）。
    崩溃残留的锁 5 分钟后可被抢占。
    """
    lock = os.path.join(BASE, "run.lock")
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


# ---------- B站：直播 ----------

def check_live(client: BiliClient, st: State, notifier: Notifier, uid: str, cfg: dict) -> None:
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
        if ps == 0 and status == 1 and cur:
            notifier.notify(f"🔴 {uname} 开播了",
                            f"{title}\n分区: {cur.get('area', '')}",
                            url=live_url, priority=4, attach=cur.get("cover"),
                            jump=True)
        elif ps == 0 and status == 2 and cur:
            notifier.notify(f"🟠 {uname} 轮播中", title, url=live_url,
                            priority=3, jump=True)
        elif ps in (1, 2) and status == 0:
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


def check_dynamics(client: BiliClient, st: State, notifier: Notifier, uid: str, cfg: dict) -> None:
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

def check_videos(client: BiliClient, st: State, notifier: Notifier, uid: str, cfg: dict) -> None:
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

def _ingest_news(st: State, notifier: Notifier, key: str, label: str,
                 items: list, strict_token: str = "") -> None:
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
    if strict_token:
        # 严格模式：标题必须包含关键词，过滤搜索源带进来的无关新闻
        new = [i for i in new if any(t and t in i["title"] for t in strict_token.split())]
    for it in new[:MAX_NOTIFY_PER_RUN]:
        notifier.notify(f"🟢 {label}", it["title"], url=it["link"], priority=3)
    seen[:] = list(dict.fromkeys(links + seen))[:300]


def check_news(st: State, notifier: Notifier, query: str,
               strict: bool = True, cfg: dict = None) -> None:
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
    rumor_action = cfg.get("rumor_action", "tag")  # tag=标记降级 / drop=直接丢弃

    nmap = st.data.setdefault("news", {})
    first = key not in nmap or not nmap.get(key)
    seen = nmap.setdefault(key, [])
    links = [i["link"] for i in items]
    if first:
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

    # 同一事件跨媒体去重：与本轮已保留项、近48小时已推送标题比对相似度，
    # 只保留第一个报道的（后续不同措辞的转载/跟进不再重复通知）
    recent = st.data.setdefault("news_recent", {}).setdefault(key, [])
    now = time.time()
    recent[:] = [r for r in recent if now - r.get("t", 0) < 48 * 3600]
    uniq, dup = [], 0
    for it, title, src in normals:
        if (any(similar_title(title, r["title"]) for r in recent)
                or any(similar_title(title, u[1]) for u in uniq)):
            dup += 1
            log.info("[新闻] 过滤同事件重复报道: %s", title[:60])
            continue
        uniq.append((it, title, src))

    # 推送：传闻单独静默推；正常新闻 1条=原格式，多条=合并成一条摘要，
    # 避免同一轮冒出连环通知
    for it, title, src in rumors[:MAX_NOTIFY_PER_RUN]:
        body = title + (f"\n来源: {src}" if src else "")
        if it.get("summary"):
            body += f"\n{it['summary']}"
        notifier.notify(f"🟡 {query} 传闻（未经证实）", body,
                        url=it["link"], priority=2)
        recent.append({"t": now, "title": title})
    if len(uniq) == 1:
        it, title, src = uniq[0]
        mark = "（权威）" if any(t in src for t in trusted) else ""
        body = title + (f"\n来源: {src}{mark}" if src else "")
        if it.get("summary"):
            body += f"\n{it['summary']}"
        notifier.notify(f"🟢 {query} 新闻", body, url=it["link"], priority=3)
        recent.append({"t": now, "title": title})
    elif uniq:
        batch = uniq[:MAX_NOTIFY_PER_RUN + 3]
        lines = []
        for i, (it, title, src) in enumerate(batch, 1):
            mark = "（权威）" if any(t in src for t in trusted) else ""
            text = (it.get("summary") or title)
            lines.append(f"{i}. {text}" + (f"（{src}{mark}）" if src else ""))
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


def check_official(st: State, notifier: Notifier, key: str, name: str) -> None:
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
        notifier.notify(f"🔵 {name}", body, url=it["link"],
                        priority=4, attach=attach)
    seen[:] = list(dict.fromkeys(ids + seen))[:100]


def check_rss_feeds(st: State, notifier: Notifier, cfg: dict, due=None) -> None:
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

# ---------- 主流程 ----------

def run(cfg: dict, force: bool = False, scope: str = "all") -> None:
    """scope: all=全部 | bili=仅B站 | news=仅新闻（云端/本地分工部署用）"""
    state_file = os.environ.get("STATE_FILE") or "state.json"
    st = State(os.path.join(BASE, state_file))
    notifier = Notifier(cfg)
    client = None
    if scope in ("all", "bili"):
        client = BiliClient(cfg.get("bili_sessdata") or "",
                            cookie_file=os.path.join(BASE, "bilibili_cookies.json"))
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
    if scope in ("all", "news"):
        for q in cfg.get("news_queries", []):
            if due(f"news:{q}", "news"):
                check_news(st, notifier, q, cfg.get("news_strict_match", True), cfg)
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
        check_rss_feeds(st, notifier, cfg, due)
    flush_fail_warnings(st, notifier)
    st.save()
    if client is not None:
        client.save_cookies()


def setup_logging() -> None:
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
        os.path.join(BASE, "bot.log"), encoding="utf-8",
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
    cfg = load_config()
    notifier = Notifier(cfg)
    if not notifier.channels:
        log.error("未配置任何推送通道，请编辑 config.json 的 push 段")
        sys.exit(1)
    if args.reset:
        p = os.path.join(BASE, "state.json")
        if os.path.exists(p):
            os.remove(p)
        log.info("已清空 state.json")
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
