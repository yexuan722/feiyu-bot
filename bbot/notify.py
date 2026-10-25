# -*- coding: utf-8 -*-
"""投喂通知：webhook（Telegram / Bark / 钉钉 / 飞书 / 自定义）+ B站 私信（beta）。

所有发送都是「尽力而为」：失败只记日志，不阻塞主流程。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .api import BiliClient, BiliError
from .store import Store


def _post_json(url: str, payload: dict, timeout: float = 15) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def push(cfg: dict, title: str, text: str, url: str = "") -> tuple[bool, str]:
    notify = cfg.get("notify", {})
    if not notify.get("webhook_enabled"):
        return False, "webhook 没开"
    endpoint = (notify.get("webhook_url") or "").strip()
    if not endpoint:
        return False, "webhook_url 是空的"
    kind = (notify.get("webhook_type") or "json_text").lower()
    chat_id = notify.get("chat_id") or ""

    try:
        if kind == "telegram":
            base = endpoint.rstrip("/")
            if "api.telegram.org/bot" in base and not base.endswith("/sendMessage"):
                base = f"{base}/sendMessage"
            _post_json(
                base,
                {"chat_id": chat_id, "text": f"{title}\n{text}" + (f"\n{url}" if url else "")},
            )
        elif kind == "bark":
            tail = urllib.parse.quote(f"{title} {text}")
            target = endpoint.rstrip("/") + "/" + tail
            if url:
                target += "?url=" + urllib.parse.quote(url)
            urllib.request.urlopen(target, timeout=15).read()
        elif kind in ("dingtalk", "feishu", "飞书", "钉钉"):
            content = f"{title}\n{text}" + (f"\n{url}" if url else "")
            _post_json(endpoint, {"msgtype": "text", "text": {"content": content}})
        else:
            _post_json(endpoint, {"title": title, "text": text, "url": url})
        return True, "已推送"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def dm_digest(store: Store, client: BiliClient, cfg: dict, limit: int = 3) -> tuple[bool, str]:
    """把挑出来的视频拼成一条私信，发给主号。"""
    notify = cfg.get("notify", {})
    if not notify.get("dm_enabled"):
        return False, "私信没开"
    target = str(notify.get("dm_to_uid") or "").strip()
    if not target:
        return False, "dm_to_uid 没填"

    rows = store.pending_notify()
    if not rows:
        return False, "没有新东西可发"

    lines = ["本鱼今日捞到的鱼："]
    for index, row in enumerate(rows[:limit], 1):
        lines.append(f"{index}. {row['title'][:34]}")
        lines.append(f"   {row['url']}")
    message = "\n".join(lines)
    try:
        client.send_dm(target, message)
    except BiliError as exc:
        return False, f"[{exc.code}] {exc.message}"
    store.mark_notified([row["id"] for row in rows[:limit]])
    return True, f"已私信 {min(limit, len(rows))} 条"


def notify_new(store: Store, client: BiliClient, cfg: dict) -> list[str]:
    """新挑出来的视频走一遍所有开启的通道，返回日志。"""
    logs: list[str] = []
    rows = store.pending_notify()
    if not rows:
        return logs
    top = rows[:3]
    title = f"本鱼捞到 {len(rows)} 条"
    text = "\n".join(f"[{row['score']}] {row['title'][:30]}" for row in top)

    if cfg.get("notify", {}).get("webhook_enabled"):
        ok, message = push(cfg, title, text, top[0]["url"] if top else "")
        logs.append(f"webhook：{message}")
        if ok:
            store.mark_notified([row["id"] for row in rows])

    if cfg.get("notify", {}).get("dm_enabled"):
        ok, message = dm_digest(store, client, cfg)
        logs.append(f"私信：{message}")

    return logs
