# -*- coding: utf-8 -*-
"""本地驾驶舱：一个网页，看得见待发、点得动发送、刷得到投喂。

只绑 127.0.0.1，不往公网开。刷新页面就能看到最新状态。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as config_mod
from . import discover, dm, interact, notify
from .api import BiliClient
from .logbook import log, tail
from .store import Store

UI_PATH = Path(__file__).resolve().parent / "ui.html"


def _age(ts: float) -> str:
    if not ts:
        return ""
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


class Runtime:
    def __init__(self, cfg: dict, store: Store, client: BiliClient):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.lock = threading.Lock()

    # -------------------------------------------------------------- 读

    def _draft_view(self, draft: dict) -> dict:
        bvid = draft.get("bvid") or ""
        kind = draft.get("kind") or ""
        label = {"at": "@我", "reply": "回复我", "dm": "私信"}.get(kind, kind or "?")
        return {
            "id": draft["id"],
            "kind": kind,
            "kind_label": label,
            "who": draft.get("trigger_name") or "某人",
            "trigger_text": draft.get("trigger_text") or "",
            "context": draft.get("context") or "",
            "title": draft.get("title") or ("私信" if kind == "dm" else "（没取到视频标题）"),
            "bvid": bvid,
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
            "body": draft.get("body") or "",
            "angle": draft.get("angle") or "",
            "note": draft.get("note") or "",
            "age": _age(draft.get("created") or 0),
            "error": draft.get("error") or "",
            "status": draft.get("status") or "pending",
        }

    def state(self) -> dict:
        daily_count = int(self.cfg.get("discover", {}).get("daily_count", 10))
        return {
            "status": config_mod.status(self.cfg),
            "stats": self.store.stats(),
            "limits": self.cfg.get("limits", {}),
            "dm": {
                "enabled": bool(self.cfg.get("dm", {}).get("enabled", True)),
                "auto_reply": bool(self.cfg.get("dm", {}).get("auto_reply", False)),
            },
            "discover_cfg": {
                "mode": self.cfg.get("discover", {}).get("mode", "daily"),
                "daily_at": self.cfg.get("discover", {}).get("daily_at", "09:00"),
                "daily_count": self.cfg.get("discover", {}).get("daily_count", 10),
            },
            "tag_prefs": [
                {"tag": row["tag"], "weight": round(float(row["weight"]), 2)}
                for row in self.store.list_tag_prefs(16)
            ],
            "drafts": [self._draft_view(d) for d in self.store.list_drafts("pending", 50)],
            "failed": [self._draft_view(d) for d in self.store.list_drafts("failed", 10)],
            "feed": [
                {
                    "id": row["id"],
                    "bvid": row["bvid"],
                    "title": row["title"],
                    "owner": row["owner"],
                    "tname": row["tname"],
                    "duration": row["duration"],
                    "view_count": row["view_count"],
                    "url": row["url"],
                    "score": row["score"],
                    "reason": row["reason"],
                    "source": row["source"],
                    "tags": row.get("tags") or [],
                    "age": _age(row["created"]),
                }
                for row in self.store.list_feed_latest_batch(daily_count)
            ],
            "feed_total": self.store.feed_total(),
            "logs": tail(50),
            "now": time.time(),
        }

    # -------------------------------------------------------------- 写

    def send(self, draft_id: int, body: str = "") -> dict:
        with self.lock:
            draft = self.store.get_draft(int(draft_id))
            if not draft:
                return {"ok": False, "message": "这条草稿不存在了"}
            if draft.get("status") == "sent":
                return {"ok": False, "message": "已经发过了"}
            text = (body or draft.get("body") or "").strip()
            if not text:
                return {"ok": False, "message": "内容是空的"}
            if text != (draft.get("body") or ""):
                self.store.update_draft(int(draft_id), body=text)
            ok, message = interact.deliver(
                self.store, self.client, self.cfg, draft, body=text, ignore_gap=True
            )
            log(f"  {'✓' if ok else '×'} 手动发送 #{draft_id}：{message}")
            return {"ok": ok, "message": message}

    def skip(self, draft_id: int) -> dict:
        self.store.update_draft(int(draft_id), status="skipped")
        return {"ok": True, "message": "已跳过"}

    def retry(self, draft_id: int) -> dict:
        self.store.update_draft(int(draft_id), status="pending", error="")
        return {"ok": True, "message": "已放回队列"}

    def hide(self, feed_id: int) -> dict:
        self.store.hide_feed(int(feed_id))
        return {"ok": True, "message": "不感兴趣，已隐藏"}

    def poll(self) -> dict:
        log("· 扫一遍 @ 和回复")
        return interact.run_once(self.store, self.client, self.cfg)

    def poll_dm(self) -> dict:
        log("· 扫一遍私信")
        return dm.run_once(self.store, self.client, self.cfg)

    def discover(self) -> dict:
        log("· 去首页捞视频")
        result = discover.run_once(self.store, self.client, self.cfg)
        discover.mark_daily(self.store)
        for line in notify.notify_new(self.store, self.client, self.cfg):
            log(f"  · 通知 {line}")
        return result

    def bump_tag(self, tag: str, delta: float) -> dict:
        weight = self.store.bump_tag(tag, delta)
        log(f"  · tag「{tag}」权重 {weight:+.1f}")
        return {"ok": True, "message": f"「{tag}」权重 {weight:+.1f}", "weight": weight}

    def reset_tags(self) -> dict:
        self.store.reset_tags()
        log("  · 口味偏好已清零")
        return {"ok": True, "message": "口味偏好已清零"}


class Worker(threading.Thread):
    """后台循环：定时扫消息 / 扫私信 / 到点捞视频。出错不退出，歇一会儿再来。"""

    def __init__(self, runtime: Runtime):
        super().__init__(daemon=True, name="feiyu-worker")
        self.rt = runtime
        self._stop = threading.Event()
        self.last_poll = 0.0
        self.last_dm = 0.0
        self.last_discover = 0.0

    def stop(self) -> None:
        self._stop.set()

    def _safe(self, fn, name: str) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {name} 出错：{type(exc).__name__}: {exc}")

    def run(self) -> None:
        cfg = self.rt.cfg
        poll_seconds = max(30.0, float(cfg.get("mode", {}).get("poll_seconds", 180)))
        dm_cfg = cfg.get("dm", {})
        dm_seconds = max(60.0, float(dm_cfg.get("poll_seconds", 300)))
        disc_cfg = cfg.get("discover", {})
        interval_seconds = max(300.0, float(disc_cfg.get("interval_minutes", 180)) * 60)

        plan = (
            f"推荐：每天 {disc_cfg.get('daily_at', '09:00')} 挑 {disc_cfg.get('daily_count', 10)} 条"
            if (disc_cfg.get("mode") or "daily") == "daily"
            else f"推荐：每 {int(interval_seconds // 60)} 分钟跑一次"
        )
        log(f"循环启动：扫消息 {int(poll_seconds)}s · 私信 {int(dm_seconds)}s · {plan}")

        first = True
        while not self._stop.is_set():
            now = time.time()
            if first or now - self.last_poll >= poll_seconds:
                self._safe(self.rt.poll, "扫消息")
                self.last_poll = time.time()
            if dm_cfg.get("enabled", True) and (first or now - self.last_dm >= dm_seconds):
                self._safe(self.rt.poll_dm, "扫私信")
                self.last_dm = time.time()
            if (disc_cfg.get("mode") or "daily") == "daily":
                if discover.daily_due(self.rt.store, cfg):
                    self._safe(self.rt.discover, "每日推荐")
                    self.last_discover = time.time()
            elif first or now - self.last_discover >= interval_seconds:
                self._safe(self.rt.discover, "捞视频")
                self.last_discover = time.time()
            first = False
            self._stop.wait(5.0)


class Handler(BaseHTTPRequestHandler):
    runtime: Runtime | None = None
    server_version = "FeiyuDesk/0.1"

    def log_message(self, *args) -> None:  # 别把每个请求都喷到终端
        pass

    # -------------------------------------------------------------- 输出

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        try:
            body = UI_PATH.read_text(encoding="utf-8").encode("utf-8")
        except OSError:
            self._json({"ok": False, "message": "ui.html 不见了"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------- 路由

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._html()
        elif path == "/api/state":
            self._json(self.runtime.state())
        elif path == "/api/health":
            self._json({"ok": True})
        else:
            self._json({"ok": False, "message": "没有这个页面"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}

        try:
            if path == "/api/send":
                reply = self.runtime.send(int(payload.get("id", 0)), payload.get("body", ""))
            elif path == "/api/skip":
                reply = self.runtime.skip(int(payload.get("id", 0)))
            elif path == "/api/retry":
                reply = self.runtime.retry(int(payload.get("id", 0)))
            elif path == "/api/hide":
                reply = self.runtime.hide(int(payload.get("id", 0)))
            elif path == "/api/poll":
                result = self.runtime.poll()
                if result.get("errors"):
                    reply = {"ok": False, "message": "；".join(result["errors"])[:220]}
                else:
                    reply = {"ok": True,
                             "message": f"扫完：新 {result['new']} / 共 {result['scanned']}"}
            elif path == "/api/discover":
                result = self.runtime.discover()
                reply = {"ok": True,
                         "message": f"捞到 {result['picked']} 条（看了 {result['fetched']} 个）"}
            elif path == "/api/dm":
                result = self.runtime.poll_dm()
                if result.get("errors"):
                    reply = {"ok": False, "message": "；".join(result["errors"])[:220]}
                else:
                    reply = {"ok": True,
                             "message": f"私信：新 {result['new']} 条 / 会话 {result['sessions']} 个"}
            elif path == "/api/tag":
                reply = self.runtime.bump_tag(
                    str(payload.get("tag") or ""), float(payload.get("delta", 1))
                )
            elif path == "/api/reset-tags":
                reply = self.runtime.reset_tags()
            else:
                reply = {"ok": False, "message": "没有这个接口"}
        except Exception as exc:  # noqa: BLE001
            reply = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        self._json(reply)


def make_server(runtime: Runtime, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"runtime": runtime})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
