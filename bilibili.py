"""B站数据获取：直播状态、用户动态、投稿视频。

直播接口免登录；动态/投稿接口需要 wbi 签名 + buvid3 Cookie
（算法见开源项目 bilibili-API-collect）。可选配置自己的
SESSDATA Cookie 以降低被风控(-352/-403)的概率。
"""
import hashlib
import html
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


class BiliError(RuntimeError):
    pass


class BiliSoftBlock(BiliError):
    """接口返回 code=0 但列表为空——被软风控，数据不可信。"""


class BiliClient:
    def __init__(self, sessdata: str = "", timeout: int = 15,
                 cookie_file: Optional[str] = None):
        self.timeout = timeout
        self.cookie_file = cookie_file
        self._sessdata = sessdata
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._wbi_key: Optional[str] = None
        self._init_cookies(sessdata or "")

    # ---------- 基础请求 ----------

    def _get_json(self, url: str, params=None, headers=None) -> Dict[str, Any]:
        last: Exception = BiliError("unknown")
        for _ in range(2):
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=self.timeout)
                return r.json()
            except Exception as e:  # 网络抖动/非JSON响应，重试一次
                last = e
                time.sleep(2)
        raise BiliError(f"请求失败 {url}: {last}")

    def _init_cookies(self, sessdata: str) -> None:
        """buvid3 是 B站的设备指纹，长期复用同一个才像真实浏览器，
        每次换新的容易被风控(-352)。因此持久化到文件跨运行复用。
        SESSDATA 属于账号凭证，只从配置/环境变量读，绝不落盘。"""
        ck: Dict[str, str] = {}
        if self.cookie_file and os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, encoding="utf-8") as f:
                    ck = json.load(f)
            except Exception:
                ck = {}
        if not ck.get("buvid3"):
            try:
                j = self._get_json("https://api.bilibili.com/x/frontend/finger/spi")
                d = j.get("data") or {}
                if d.get("b_3"):
                    ck["buvid3"] = d["b_3"]
                if d.get("b_2"):
                    ck["buvid4"] = d["b_2"]
            except Exception:
                pass
        if not ck.get("buvid3"):
            ck["buvid3"] = uuid.uuid4().hex.upper() + "infoc"
        if sessdata:
            ck["SESSDATA"] = sessdata
        self.s.cookies.update(ck)
        self.save_cookies()

    def save_cookies(self) -> None:
        if not self.cookie_file:
            return
        have = {c.name: c.value for c in self.s.cookies}
        keep = {k: have[k] for k in ("buvid3", "buvid4") if have.get(k)}
        try:
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(keep, f)
        except Exception:
            pass

    def rotate_device(self) -> None:
        """动态接口被软风控（返回空列表）时，更换 buvid3 设备指纹。"""
        self.s.cookies.clear()
        self._wbi_key = None
        if self.cookie_file and os.path.exists(self.cookie_file):
            try:
                os.remove(self.cookie_file)
            except OSError:
                pass
        self._init_cookies(self._sessdata)

    # ---------- wbi 签名 ----------

    def _wbi_keys(self) -> str:
        if self._wbi_key:
            return self._wbi_key
        j = self._get_json("https://api.bilibili.com/x/web-interface/nav")
        wbi = ((j.get("data") or {}).get("wbi_img")) or {}
        img = re.search(r"([0-9a-f]{32})\.", wbi.get("img_url") or "")
        sub = re.search(r"([0-9a-f]{32})\.", wbi.get("sub_url") or "")
        if not (img and sub):
            raise BiliError("获取 wbi keys 失败")
        raw = img.group(1) + sub.group(1)
        self._wbi_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        return self._wbi_key

    def _wbi_get(self, url: str, params: Dict[str, Any], referer: str) -> Dict[str, Any]:
        mixin = self._wbi_keys()
        headers = {"Referer": referer, "Origin": "https://www.bilibili.com"}
        last: Exception = BiliError("unknown")
        for _ in range(2):  # 风控码(-412/-352)等4秒后重签重试一次
            p = {k: str(v) for k, v in params.items()}
            p["wts"] = str(int(time.time()))
            p = dict(sorted(p.items()))
            for k in p:
                p[k] = "".join(ch for ch in p[k] if ch not in "!'()*")
            qs = urlencode(p, quote_via=quote)
            p["w_rid"] = hashlib.md5((qs + mixin).encode()).hexdigest()
            try:
                j = self._get_json(url, params=p, headers=headers)
            except BiliError as e:
                last = e
                time.sleep(4)
                continue
            if j.get("code") == 0:
                return j.get("data") or {}
            last = BiliError(f"{url} code={j.get('code')} {j.get('message')}")
            if j.get("code") not in (-412, -352, -509, 799):
                break
            time.sleep(4)
        raise last

    # ---------- 直播状态 ----------

    def user_live_brief(self, uid: str) -> Dict[str, Any]:
        j = self._get_json("https://api.live.bilibili.com/live_user/v1/Master/info",
                           params={"uid": uid, "source": "95"},
                           headers={"Referer": "https://live.bilibili.com/"})
        if j.get("code") != 0:
            raise BiliError(f"Master/info code={j.get('code')}")
        d = j.get("data") or {}
        return {"room_id": d.get("room_id") or 0,
                "uname": ((d.get("info") or {}).get("uname")) or ""}

    def room_info(self, room_id) -> Dict[str, Any]:
        j = self._get_json("https://api.live.bilibili.com/room/v1/Room/get_info",
                           params={"room_id": room_id},
                           headers={"Referer": "https://live.bilibili.com/"})
        if j.get("code") != 0:
            raise BiliError(f"Room/get_info code={j.get('code')}")
        d = j.get("data") or {}
        area = "/".join(x for x in (d.get("parent_area_name"), d.get("area_name")) if x)
        return {"live_status": d.get("live_status", 0),  # 0未开播 1直播中 2轮播中
                "title": d.get("title") or "",
                "area": area,
                "cover": d.get("keyframe") or d.get("user_cover") or "",
                "uname": d.get("uname") or "",
                "room_id": room_id}

    # ---------- 用户动态 ----------

    def dynamics(self, uid: str) -> List[Dict[str, Any]]:
        data = self._wbi_get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            {"host_mid": uid, "timezone_offset": -480},
            referer=f"https://space.bilibili.com/{uid}/dynamic")
        out = []
        for it in data.get("items") or []:
            try:
                d = self._parse_dynamic(it, uid)
            except Exception:
                continue
            if d:
                out.append(d)
        # 转存最近一次原始数据，便于离线排查解析问题（不提交git）
        try:
            debug = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data", "debug_feed.json")
            os.makedirs(os.path.dirname(debug), exist_ok=True)
            with open(debug, "w", encoding="utf-8") as f:
                json.dump(data.get("items") or [], f, ensure_ascii=False)
        except Exception:
            pass
        if not out:
            raise BiliSoftBlock("动态列表为空（疑似软风控）")
        return out

    @staticmethod
    def _parse_dynamic(it: Dict[str, Any], uid: str = "") -> Optional[Dict[str, Any]]:
        did = it.get("id_str") or str(it.get("id") or "")
        if not did:
            return None
        mods = it.get("modules") or {}
        author = (mods.get("module_author") or {}).get("name") or ""
        md = mods.get("module_dynamic") or {}
        desc = ((md.get("desc") or {}).get("text")) or ""
        major = md.get("major") or {}
        arc = major.get("archive") or {}
        draw = major.get("draw") or {}
        article = major.get("article") or {}
        opus = major.get("opus") or {}
        dtype = it.get("type") or ""
        orig, orig_self = "", False
        if dtype == "DYNAMIC_TYPE_FORWARD":
            o = it.get("orig") or {}
            orig = o.get("id_str") or ""
            try:
                orig_self = (int(((o.get("modules") or {}).get("module_author")
                                  or {}).get("mid") or 0) == int(uid or 0))
            except (TypeError, ValueError):
                orig_self = False
        if arc:
            kind, text, bvid = "投稿视频", arc.get("title") or "", arc.get("bvid") or ""
        elif dtype == "DYNAMIC_TYPE_FORWARD":
            # 只显示她自己新加的评论；去掉 B站自动拼接的 //@ 转发链，避免二次转发看着像重复
            fwd = desc.split("//@")[0].strip()
            kind, text, bvid = "转发动态", (fwd or desc[:200]), ""
        elif draw or opus:
            if draw:  # 旧版图文
                pics = len(draw.get("items") or [])
                text = desc
            else:  # 新版 opus：纯文字动态也走这个格式，文字在 summary/paragraphs 里
                pics = len(opus.get("pics") or opus.get("pictures") or [])
                text = ((opus.get("summary") or {}).get("text")) or ""
                if not text:
                    text = "\n".join(
                        (p.get("text") or {}).get("content_str") or ""
                        for p in (opus.get("paragraphs") or [])
                        if (p.get("text") or {}).get("content_str"))
                text = text or opus.get("title") or desc
            if not pics and not (text or "").strip():
                # B站偶尔返回空壳动态（图片审核中/仅粉丝可见/已删除），接口不给内容
                kind, text = "动态", "(内容暂不可见，点开查看)"
            else:
                kind = "图文动态" if pics else "文字动态"
                if pics and not (text or "").strip():
                    text = f"[图片x{pics}]"
                text = (text or "")[:400]
            bvid = ""
        elif article or dtype == "DYNAMIC_TYPE_ARTICLE":
            kind, text, bvid = "专栏文章", (article.get("title") or desc)[:400], ""
        elif dtype in ("DYNAMIC_TYPE_LIVE_RCMD", "DYNAMIC_TYPE_LIVE"):
            # 开播提醒由直播状态监控负责，这里只记 id 不通知，避免重复
            return {"id": did, "kind": "", "text": "", "author": author,
                    "bvid": "", "url": "", "orig": "", "orig_self": False}
        elif dtype == "DYNAMIC_TYPE_WORD":
            kind, text, bvid = "文字动态", desc[:400], ""
        else:
            kind, text, bvid = "动态", desc[:400], ""
        return {"id": did, "kind": kind, "text": (text or "").strip()[:400],
                "author": author, "bvid": bvid,
                "url": f"https://t.bilibili.com/{did}",
                "orig": orig, "orig_self": orig_self}

    # ---------- 投稿视频 ----------

    def videos(self, uid: str) -> List[Dict[str, Any]]:
        data = self._wbi_get(
            "https://api.bilibili.com/x/space/wbi/arc/search",
            {"mid": uid, "ps": 12, "pn": 1, "order": "pubdate",
             "platform": "web", "web_location": "1550101"},
            referer=f"https://space.bilibili.com/{uid}/video")
        vlist = ((data.get("list") or {}).get("vlist")) or []
        out = []
        for v in vlist:
            bvid = v.get("bvid") or ""
            pic = v.get("pic") or ""
            if pic.startswith("//"):
                pic = "https:" + pic
            out.append({"bvid": bvid,
                        "title": html.unescape(v.get("title") or ""),
                        "url": f"https://www.bilibili.com/video/{bvid}",
                        "length": v.get("length") or "",
                        "pic": pic})
        return out
