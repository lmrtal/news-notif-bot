"""RSS 新闻源：Google News 关键词订阅 + 通用 RSS + 智谱官网官方新闻。"""
import html
import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, List
from urllib.parse import quote

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def fetch_rss(url: str, timeout: int = 20) -> List[Dict[str, str]]:
    last: Exception = RuntimeError("unknown")
    for attempt in range(2):  # 重试一次，扛过代理节点瞬断
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
            r.raise_for_status()
            break
        except Exception as e:
            last = e
            if attempt == 0:
                time.sleep(3)
    else:
        raise last
    try:
        items = _parse_xml(r.content)
    except Exception:
        items = _parse_regex(r.content.decode("utf-8", errors="replace"))
    out = [{"title": (i.get("title") or "").strip(),
            "link": (i.get("link") or "").strip(),
            "source": (i.get("source") or "").strip()} for i in items]
    return [i for i in out if i["link"]]


def _parse_xml(data: bytes) -> List[Dict[str, str]]:
    root = ET.fromstring(data)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        items.append({"title": it.findtext("title") or "",
                      "link": it.findtext("link") or "",
                      "source": it.findtext("source") or ""})
    if not items:  # Atom
        ns = "{http://www.w3.org/2005/Atom}"
        for e in root.iter(ns + "entry"):
            link = ""
            for l in e.iter(ns + "link"):
                link = l.get("href") or l.text or ""
                break
            items.append({"title": e.findtext(ns + "title") or "",
                          "link": link, "source": ""})
    return items


def _parse_regex(text: str) -> List[Dict[str, str]]:
    """XML 解析失败时的兜底（部分源会输出不合法实体）。"""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", text, re.S):
        seg = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", seg, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", seg, re.S)
        s = re.search(r"<source[^>]*>([^<]*)</source>", seg)
        items.append({"title": (t.group(1) if t else "").strip(),
                      "link": (l.group(1) if l else "").strip(),
                      "source": (s.group(1) if s else "").strip()})
    return items


def google_news(query: str) -> List[Dict[str, str]]:
    url = ("https://news.google.com/rss/search?q=" + quote(query)
           + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    items = fetch_rss(url, timeout=10)  # Google 不通时快速失败，别拖慢整轮
    for i in items:
        # 去掉 Google News 标题末尾附加的“ - 来源名”后缀（仅无空格的短后缀）
        i["title"] = re.sub(r"\s+-\s+(\S+)$", "", i["title"])
    return items


def zhipu_official_news(timeout: int = 20) -> List[Dict[str, str]]:
    """智谱官网新闻中心（服务端渲染，链接与标题直接在 HTML 里）。

    官方一手信源：新品发布、业绩公告等，可信度最高。
    页面改版导致解析到 0 条时会抛异常，触发失败告警而不是静默失效。
    """
    r = requests.get("https://www.zhipuai.cn/zh/news", timeout=timeout,
                     headers={"User-Agent": UA})
    r.raise_for_status()
    items, got = [], set()
    for m in re.finditer(r'href="/zh/news/(\d+)"(.{0,1500}?)alt="([^"]{4,200})"',
                         r.text, re.S):
        nid, title = m.group(1), html.unescape(m.group(3)).strip()
        if not nid or nid in got or not title:
            continue
        got.add(nid)
        items.append({"id": nid, "title": title,
                      "link": f"https://www.zhipuai.cn/zh/news/{nid}"})
    items.sort(key=lambda x: int(x["id"]), reverse=True)
    if not items:
        raise RuntimeError("智谱官网新闻页解析到 0 条（页面可能改版）")
    return items


def zhipu_article_detail(url: str, title: str = "", timeout: int = 20) -> tuple:
    """智谱官网文章详情页（SSR 渲染），返回 (正文摘录约600字, 首图URL)。

    正文进通知直接阅读；纯图片公告（如业绩长图）正文为空，首图作为通知附件。
    """
    r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    r.raise_for_status()
    m = re.search(r"<article[^>]*>(.*?)</article>", r.text, re.S)
    if not m:
        return "", ""
    art = m.group(1)
    image = ""
    im = re.search(r'<img[^>]+src="([^"]+)"', art)
    if im:
        src = html.unescape(im.group(1))
        if src.startswith("http"):
            image = src
        elif src.startswith("/"):
            image = "https://www.zhipuai.cn" + src
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", art, flags=re.S)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    txt = html.unescape(txt)
    lines = [ln.strip() for ln in txt.splitlines()]
    lines = [ln for ln in lines
             if ln and ln != title
             and not re.match(r"^\d{4}(/\d{1,2}(/\d{1,2})?)?$", ln)]
    return "\n".join(lines)[:600], image


def deepseek_official_news(timeout: int = 20) -> List[Dict[str, str]]:
    """DeepSeek 官网新闻页（SSR 渲染），标题和摘要都在列表页 HTML 里。"""
    r = requests.get("https://www.deepseek.com/news/", timeout=timeout,
                     headers={"User-Agent": UA})
    r.raise_for_status()
    items, got = [], set()
    for m in re.finditer(
            r'href="(/news/([a-z0-9\-]{2,60})/)"(.{0,1500}?)(?:<h[23][^>]*>([^<]{4,150})</h[23]>)',
            r.text, re.S):
        slug, title = m.group(2), html.unescape(m.group(4) or "").strip()
        if not slug or slug in got or not title:
            continue
        got.add(slug)
        tail = r.text[m.end(): m.end() + 500]
        s = re.search(r"<p[^>]*>([^<]{10,300})</p>", tail)
        summary = html.unescape(s.group(1)).strip() if s else ""
        items.append({"id": slug, "title": title, "text": summary[:200],
                      "link": f"https://www.deepseek.com{m.group(1)}"})
    if not items:
        raise RuntimeError("DeepSeek 官网新闻页解析到 0 条（页面可能改版）")
    return items


def apple_official_news(timeout: int = 20) -> List[Dict[str, str]]:
    """Apple Newsroom 官方 RSS（apple.com.cn，Atom 格式）。

    自带标题、摘要（content）、配图（enclosure），无需进详情页。
    """
    r = requests.get("https://www.apple.com.cn/newsroom/rss-feed.rss",
                     timeout=timeout, headers={"User-Agent": UA})
    r.raise_for_status()
    items, got = [], set()
    for m in re.finditer(r"<entry>(.*?)</entry>", r.text, re.S):
        seg = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", seg, re.S)
        l = re.search(r'<link href="([^"]+)"[^>]*/>', seg)  # 第一个 link 是文章链接
        c = re.search(r"<content>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</content>", seg, re.S)
        img = re.search(r'<link href="([^"]+)"[^>]*rel="enclosure"', seg)
        if not (t and l):
            continue
        title = html.unescape(t.group(1)).strip()
        url = html.unescape(l.group(1)).strip()
        slug = url.rstrip("/").split("/")[-1] or url
        if not title or slug in got:
            continue
        got.add(slug)
        image = html.unescape(img.group(1)).strip() if img else ""
        if image and not image.startswith("http"):
            image = "https://www.apple.com.cn" + image
        items.append({"id": slug, "title": title,
                      "text": html.unescape(c.group(1)).strip()[:200] if c else "",
                      "image": image, "link": url})
    if not items:
        raise RuntimeError("Apple Newsroom RSS 解析到 0 条（页面可能改版）")
    return items


def official_news(site: str) -> List[Dict[str, str]]:
    """统一入口：按 key 分发到各公司官网解析器。返回 [{id,title,text,link}]。"""
    parsers = {"zhipu": zhipu_official_news, "deepseek": deepseek_official_news,
               "apple": apple_official_news}
    if site not in parsers:
        raise RuntimeError(f"未知官方源: {site}")
    return parsers[site]()
