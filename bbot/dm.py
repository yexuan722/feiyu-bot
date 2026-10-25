# -*- coding: utf-8 -*-
"""私信自动回复。

⚠️ beta：B站 私信接口没公开文档，`dev_id` 是从 buvid3 派生的，字段也可能变。
发不出去时看日志里的 `[-400]` / `[21000]`，那说明签名对不上，不影响评论区那条链。
"""

from __future__ import annotations

from . import interact, llm
from .api import BiliClient, BiliError
from .logbook import log
from .store import Store, draft_key

# 只有这几个来源算「真人发的」：0 未知 / 1 iOS / 2 安卓 / 3 H5 / 4 PC / 7 Web
HUMAN_SOURCES = {0, 1, 2, 3, 4, 7}


def run_once(store: Store, client: BiliClient, cfg: dict, quiet: bool = False,
             explain: bool = False) -> dict:
    result = {"sessions": 0, "new": 0, "sent": 0, "errors": []}
    dm_cfg = cfg.get("dm", {})
    if not dm_cfg.get("enabled", True):
        return result

    me = client.me()
    if not me.get("is_login"):
        result["errors"].append(f"未登录：{me.get('reason', '')}")
        return result
    my_uid = int(me.get("mid") or client.uid or 0)

    try:
        sessions = client.dm_sessions()
    except BiliError as exc:
        result["errors"].append(f"拉私信会话失败 [{exc.code}] {exc.message}")
        return result
    result["sessions"] = len(sessions)

    allowed = {int(x) for x in (dm_cfg.get("allowed_uids") or []) if str(x).strip()}
    blocked = {int(x) for x in (dm_cfg.get("blocked_uids") or []) if str(x).strip()}
    limit = int(dm_cfg.get("max_sessions_per_round", 6))

    for session in sessions[:limit]:
        talker = int(session.get("talker_id") or 0)
        if not talker or talker in blocked:
            continue
        if allowed and talker not in allowed:
            continue

        # 不能靠 unread_count 判断「有没有新消息」：你自己在网页或 APP 里
        # 点开看一眼，未读数就归零了，但对方那条其实还没回。
        # 改成看会话时间有没有往后走。
        session_ts = int(session.get("timestamp") or 0)
        last_scan = float(store.get_meta(f"dm_ts_{talker}", "0") or 0)
        if session_ts and session_ts <= last_scan:
            if explain:
                log(f"  · {talker}: 会话没更新，跳过")
            continue

        try:
            messages = client.dm_messages(talker, 12)
        except BiliError as exc:
            result["errors"].append(f"拉会话消息失败 [{exc.code}] {exc.message}")
            continue
        # 游标往前走，避免每轮都重复拉同一个会话
        store.set_meta(f"dm_ts_{talker}", str(session_ts))
        if not messages:
            continue

        # 最后一句是我说的 → 已经回过了，别追着人唠
        if messages[-1]["from"] == my_uid:
            if explain:
                log(f"  · {talker}: 最后一句是我发的，跳过")
            continue

        # 跳过系统推送/自动回复（互相关注提示、活动通知之类），别对着机器说话
        theirs = [
            m for m in messages
            if m["from"] != my_uid
            and m["type"] == 1
            and m["text"]
            and m.get("source", 0) in HUMAN_SOURCES
        ]
        if not theirs:
            if explain:
                log(f"  · {talker}: 只有系统消息，跳过")
            continue
        last = theirs[-1]

        key = draft_key("dm", talker, last["key"], last["text"])
        if store.has_draft(key):
            if explain:
                log(f"  · {talker}: 这条已经生成过草稿，跳过")
            continue

        body = ""
        if llm.available(cfg):
            recent = [
                f"{'我' if m['from'] == my_uid else '对方'}：{m['text'][:60]}" for m in messages[-6:]
            ]
            try:
                raw = llm.chat(cfg, interact.build_dm_messages(cfg, "", last["text"], recent))
                body = llm.clean_line(raw)[: int(dm_cfg.get("max_reply_chars", 120))]
            except llm.LLMError as exc:
                result["errors"].append(str(exc)[:150])

        draft_id = store.add_draft(
            mkey=key,
            kind="dm",
            trigger_uid=talker,
            trigger_name=f"UID {talker}",
            trigger_text=last["text"],
            context="\n".join(
                f"{'我' if m['from'] == my_uid else '对方'}：{m['text'][:60]}" for m in messages[-4:-1]
            ),
            bvid="",
            oid=talker,
            root=0,
            parent=0,
            title="私信",
            uri="",
            body=body,
            angle="模型生成" if body else "未配模型，手动写一句",
            status="pending",
            note="私信回复",
        )
        if draft_id is None:
            continue
        result["new"] += 1

        if dm_cfg.get("auto_reply") and body and not interact.is_risky(body):
            ok, message = interact.deliver(store, client, cfg, store.get_draft(draft_id) or {})
            if ok:
                result["sent"] += 1
            else:
                result["errors"].append(message[:150])

    if result["new"] and not quiet:
        log(f"  + 私信新增 {result['new']} 条")
    return result
