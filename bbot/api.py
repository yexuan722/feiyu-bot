# -*- coding: utf-8 -*-
"""B站 网页接口客户端（零依赖，只用标准库）。

⚠️ 重要：这里调的都是 B站 App/网页 自用的内部接口，没有公开文档、随时会变，
而且用脚本调它们本身就违反《哔哩哔哩用户协议》。用之前先想清楚：这个号封了
你心疼不心疼。代码里所有写操作（发评论 / 发私信）都默认关着，由配置决定。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import wbi

API = "https://api.bilibili.com"
VC_API = "https://api.vc.bilibili.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AID_RE = re.compile(r"(?:video|av)/(\d+)")


class BiliError(RuntimeError):
    """B站 返回了非 0 的 code。"""

    def __init__(self, code: int, message: str, url: str = "", data=None):
        self.code = code
        self.message = message
        self.url = url
        self.data = data
        super().__init__(f"[{code}] {message}")

    @property
    def is_login_error(self) -> bool:
        return self.code in (-101, -400, -403, 61000)


def _as_text(value) -> str:
    """评论内容在接口里有时是字符串，有时是 {"message": "..."}，统一成文字。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "content", "text", "comment"):
            if isinstance(value.get(key), str):
                return value[key]
        return json.dumps(value, ensure_ascii=False)[:500]
    if isinstance(value, list):
        return " ".join(_as_text(v) for v in value)
    return str(value)


def _first(data: dict, *names, default=None):
    """按优先级取第一个有意义的字段（0 / 空串都算没有）。"""
    if not isinstance(data, dict):
        return default
    for name in names:
        if name in data:
            value = data[name]
            if value not in (None, "", 0, "0", [], {}):
                return value
    return default


class BiliClient:
    def __init__(self, cfg: dict):
        bili = cfg.get("bili", {})
        self.cfg = cfg
        self.sessdata = (bili.get("sessdata") or "").strip()
        self.jct = (bili.get("bili_jct") or "").strip()
        self.buvid3 = (bili.get("buvid3") or "").strip()
        self.uid = str(bili.get("dedeuserid") or "").strip()
        self.ua = (bili.get("user_agent") or DEFAULT_UA).strip()
        self.timeout = float(bili.get("timeout") or 20)
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_at = 0.0
        self._me: dict | None = None

    # ---------------------------------------------------------------- 底层

    def _cookie(self) -> str:
        parts = []
        if self.sessdata:
            parts.append(f"SESSDATA={self.sessdata}")
        if self.jct:
            parts.append(f"bili_jct={self.jct}")
        if self.buvid3:
            parts.append(f"buvid3={self.buvid3}")
        if self.uid:
            parts.append(f"DedeUserID={self.uid}")
        parts.append("buvid_fp=unknown")
        return "; ".join(parts)

    def _headers(self, referer: str = "https://www.bilibili.com/") -> dict:
        return {
            "User-Agent": self.ua,
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": self._cookie(),
        }

    def _wbi(self, force: bool = False) -> tuple[str, str]:
        if self._wbi_keys and not force and time.time() - self._wbi_at < 600:
            return self._wbi_keys
        data = self._request(f"{API}/x/web-interface/nav", referer="https://www.bilibili.com/")
        img = ((data or {}).get("wbi_img") or {}).get("img_url") or ""
        sub = ((data or {}).get("wbi_img") or {}).get("sub_url") or ""
        if not img or not sub:
            raise BiliError(-1, "拿不到 wbi key（nav 接口返回异常）")
        self._wbi_keys = wbi.keys_from_urls(img, sub)
        self._wbi_at = time.time()
        return self._wbi_keys

    def _request(
        self,
        url: str,
        params: dict | None = None,
        form: dict | None = None,
        use_wbi: bool = False,
        referer: str = "https://www.bilibili.com/",
        timeout: float | None = None,
    ):
        params = dict(params or {})
        if use_wbi:
            img, sub = self._wbi()
            params = wbi.sign(params, img, sub)

        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers = self._headers(referer)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        else:
            full = url
            if params:
                full = f"{url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(full, headers=self._headers(referer), method="GET")

        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise BiliError(exc.code, f"HTTP {exc.code} {detail}", url) from exc
        except urllib.error.URLError as exc:
            raise BiliError(-1, f"网络不通：{exc.reason}", url) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BiliError(-1, f"返回不是 JSON：{raw[:200]}", url) from exc

        code = payload.get("code")
        if code != 0:
            raise BiliError(int(code or -1), str(payload.get("message") or "未知错误"), url, payload)
        return payload.get("data")

    # ---------------------------------------------------------------- 登录态

    def me(self, refresh: bool = False) -> dict:
        if self._me and not refresh:
            return self._me
        if not self.sessdata:
            self._me = {"is_login": False, "reason": "没配 SESSDATA"}
            return self._me
        try:
            data = self._request(f"{API}/x/web-interface/nav", referer="https://www.bilibili.com/")
        except BiliError as exc:
            self._me = {"is_login": False, "reason": f"{exc.code} {exc.message}"}
            return self._me
        info = {
            "is_login": bool(data.get("isLogin")),
            "mid": data.get("mid"),
            "uname": data.get("uname"),
            "level": data.get("level_info", {}).get("current_level"),
            "vip": (data.get("vipStatus") or 0) == 1,
        }
        if not info["is_login"]:
            info["reason"] = "Cookie 过期或没带对"
        elif not self.uid and info.get("mid"):
            self.uid = str(info["mid"])
        self._me = info
        return info

    # ---------------------------------------------------------------- 读：消息

    def at_me(self, offset: str = "") -> tuple[list[dict], str, bool]:
        params = {"build": "0", "mobi_app": "web", "platform": "web", "limit": "20"}
        if offset:
            params["offset"] = offset
        try:
            data = self._request(f"{API}/x/msgfeed/at", params=params)
        except BiliError as exc:
            # 有的版本给消息接口也上了 wbi 校验，补个签名再试一次
            if exc.code not in (-403, -352, 412):
                raise
            data = self._request(f"{API}/x/msgfeed/at", params=params, use_wbi=True)
        return self._parse_feed(data, "at")

    def reply_me(self, offset: str = "") -> tuple[list[dict], str, bool]:
        params = {"build": "0", "mobi_app": "web", "platform": "web", "limit": "20"}
        if offset:
            params["offset"] = offset
        try:
            data = self._request(f"{API}/x/msgfeed/reply", params=params)
        except BiliError as exc:
            if exc.code not in (-403, -352, 412):
                raise
            data = self._request(f"{API}/x/msgfeed/reply", params=params, use_wbi=True)
        return self._parse_feed(data, "reply")

    def _parse_feed(self, data: dict | None, kind: str) -> tuple[list[dict], str, bool]:
        data = data or {}
        items = data.get("items") or []
        cursor = data.get("cursor") or {}
        next_offset = str(_first(cursor, "id", "offset", default="") or "")
        is_end = bool(cursor.get("is_end"))
        return [self._normalize(node, kind) for node in items], next_offset, is_end

    @staticmethod
    def _normalize(node: dict, kind: str) -> dict:
        item = node.get("item") or {}
        user = node.get("user") or {}
        reply = node.get("reply") or {}

        text = _as_text(_first(item, "source_content", "content", "message"))
        if not text:
            text = _as_text(_first(reply, "content", "message"))
        context = _as_text(_first(item, "target_reply_content", "target_content", "origin_content"))
        uri = str(_first(item, "uri", "native_uri", default="") or "")
        title = str(_first(item, "title", default="") or "")

        oid = _first(item, "subject_id", "business_id", "oid", "source_id", default=0)
        root = _first(item, "root_id", "source_id", default=0)
        rpid = _first(item, "target_id", "id", default=0)

        bvid = ""
        match = _BV_RE.search(uri)
        if match:
            bvid = match.group(1)
        aid = 0
        match = _AID_RE.search(uri)
        if match:
            aid = int(match.group(1))

        return {
            "kind": kind,
            "uid": user.get("mid") or user.get("uid") or 0,
            "nickname": user.get("nickname") or user.get("uname") or "某人",
            "text": text.strip(),
            "context": context.strip(),
            "title": title.strip(),
            "uri": uri,
            "oid": int(oid or 0),
            "root": int(root or 0),
            "rpid": int(rpid or 0),
            "bvid": bvid,
            "aid": aid,
            "timestamp": int(node.get("timestamp") or item.get("timestamp") or 0),
            "raw": node,
        }

    # ---------------------------------------------------------------- 读：视频

    def view(self, bvid: str = "", aid: int = 0) -> dict:
        params = {"bvid": bvid} if bvid else {"aid": aid}
        data = self._request(f"{API}/x/web-interface/view", params=params) or {}
        return {
            "bvid": data.get("bvid", bvid),
            "aid": data.get("aid", aid),
            "title": data.get("title", ""),
            "desc": (data.get("desc") or "")[:800],
            "tname": data.get("tname", ""),
            "duration": data.get("duration", 0),
            "owner": (data.get("owner") or {}).get("name", ""),
            "owner_mid": (data.get("owner") or {}).get("mid", 0),
            "view_count": (data.get("stat") or {}).get("view", 0),
            "like": (data.get("stat") or {}).get("like", 0),
            "pic": data.get("pic", ""),
            "pubdate": data.get("pubdate", 0),
            "url": f"https://www.bilibili.com/video/{data.get('bvid', bvid)}",
        }

    def tags(self, aid: int) -> list[str]:
        if not aid:
            return []
        try:
            data = self._request(f"{API}/x/tag/archive/tags", params={"aid": aid}) or []
        except BiliError:
            return []
        out = []
        for tag in data if isinstance(data, list) else []:
            name = tag.get("tag_name")
            if name:
                out.append(name)
        return out[:12]

    def hot_replies(self, oid: int, ps: int = 6) -> list[str]:
        """热评，用来判断这个视频聊的是什么梗。失败就算了，不影响主流程。"""
        if not oid:
            return []
        try:
            data = self._request(
                f"{API}/x/v2/reply/main",
                params={"type": 1, "oid": oid, "mode": 3, "next": 0, "ps": ps},
            ) or {}
        except BiliError:
            return []
        out = []
        for reply in (data.get("replies") or []):
            message = _as_text((reply.get("content") or {}))
            if message:
                out.append(message.strip()[:120])
        return out[:ps]

    # ---------------------------------------------------------------- 读：发现

    def popular(self, pn: int = 1, ps: int = 30) -> list[dict]:
        data = self._request(f"{API}/x/web-interface/popular", params={"pn": pn, "ps": ps}) or {}
        out = []
        for video in (data.get("list") or []):
            out.append(
                {
                    "bvid": video.get("bvid", ""),
                    "aid": video.get("aid", 0),
                    "title": video.get("title", ""),
                    "desc": (video.get("desc") or "")[:300],
                    "tname": video.get("tname", ""),
                    "duration": video.get("duration", 0),
                    "owner": (video.get("owner") or {}).get("name", ""),
                    "view_count": (video.get("stat") or {}).get("view", 0),
                    "pic": video.get("pic", ""),
                    "url": video.get("short_link_v2") or f"https://www.bilibili.com/video/{video.get('bvid','')}",
                    "source": "热门",
                }
            )
        return out

    def recommend(self, ps: int = 20) -> list[dict]:
        """首页推荐流，走 wbi 签名。没登录或风控会失败，调用方自行 fallback。"""
        data = self._request(
            f"{API}/x/web-interface/wbi/index/top/feed/rcmd",
            params={"ps": ps, "fresh_type": 3, "feed_version": "V8", "homepage_ver": 1, "y_num": 4},
            use_wbi=True,
            referer="https://www.bilibili.com/",
        ) or {}
        out = []
        for video in (data.get("item") or []):
            if video.get("goto") not in (None, "av"):
                continue
            bvid = video.get("bvid") or video.get("id") or ""
            if not str(bvid).startswith("BV"):
                continue
            out.append(
                {
                    "bvid": bvid,
                    "aid": video.get("aid", 0),
                    "title": video.get("title", ""),
                    "desc": (video.get("desc") or "")[:300],
                    "tname": video.get("tname", ""),
                    "duration": video.get("duration", 0),
                    "owner": (video.get("owner") or {}).get("name", ""),
                    "view_count": (video.get("stat") or {}).get("view", 0),
                    "pic": video.get("pic", ""),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "source": "推荐",
                }
            )
        return out

    # ---------------------------------------------------------------- 写

    def post_comment(self, oid: int, message: str, root: int = 0, parent: int = 0,
                     type_id: int = 1, referer: str = "https://www.bilibili.com/") -> dict:
        if not (self.jct and self.sessdata):
            raise BiliError(-101, "没配 Cookie（SESSDATA / bili_jct）")
        form = {
            "oid": oid,
            "type": type_id,
            "message": message,
            "plat": 1,
            "csrf": self.jct,
        }
        if root:
            form["root"] = root
        if parent:
            form["parent"] = parent
        try:
            data = self._request(f"{API}/x/v2/reply/add", form=form, referer=referer)
        except BiliError as exc:
            if exc.code not in (-403, -352, 412):
                raise
            # 风控接口偶尔要求签名，死马当活马医
            data = self._request(f"{API}/x/v2/reply/add", form=form, referer=referer, use_wbi=True)
        rpid = 0
        if isinstance(data, dict):
            rpid = (data.get("rpid") or (data.get("reply") or {}).get("rpid") or 0)
        return {"rpid": rpid, "raw": data}

    def delete_comment(self, oid: int, rpid: int, type_id: int = 1) -> bool:
        """发错话了能自己删——B站 有这个接口，但一样有频率限制。"""
        if not self.jct:
            raise BiliError(-101, "没配 bili_jct")
        self._request(
            f"{API}/x/v2/reply/del",
            form={"oid": oid, "type": type_id, "rpid": rpid, "csrf": self.jct},
        )
        return True

    # ---------------------------------------------------------------- 私信（beta）

    def _dev_id(self, variant: int = 0) -> str:
        """私信接口要 dev_id。B站 前端是从 buvid3 的 md5 派生的，这里是常见实现；
        不同版本可能对不上，所以调用方会依次试两种拼法。"""
        seed = self.buvid3 or self.sessdata or "feiyu"
        digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
        if variant == 1:
            digest = digest.translate(str.maketrans("0123456789", "ABCDEFGHIJ"))
        return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

    def send_dm(self, receiver_uid: int | str, text: str) -> dict:
        """给小号/主号发私信。这是 beta：接口签名随版本变，失败别惊讶。"""
        if not (self.jct and self.sessdata):
            raise BiliError(-101, "没配 Cookie")
        if not self.uid:
            raise BiliError(-101, "缺 DedeUserID，不知道发件人是谁")
        last: BiliError | None = None
        for variant in (0, 1):
            form = {
                "msg[sender_uid]": self.uid,
                "msg[receiver_id]": str(receiver_uid),
                "msg[receiver_type]": 1,
                "msg[msg_type]": 1,
                "msg[dev_id]": self._dev_id(variant),
                "msg[timestamp]": int(time.time()),
                "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
                "csrf": self.jct,
                "csrf_token": self.jct,
            }
            try:
                data = self._request(
                    f"{VC_API}/web_im/v1/web_im/send_msg",
                    form=form,
                    referer="https://message.bilibili.com/",
                )
                return {"ok": True, "variant": variant, "raw": data}
            except BiliError as exc:
                last = exc
                if not exc.is_login_error and exc.code not in (-400, 21000):
                    raise
                continue
        raise last or BiliError(-1, "私信发送失败")
