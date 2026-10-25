# -*- coding: utf-8 -*-
"""互动链：有人 @ 小号 / 回复小号 → 生成回复 → 进队列 → （可选）自动发。

默认半自动：只入队，等人点。auto_send=true 才真的自己发，而且带限速。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import llm
from .api import BiliClient, BiliError
from .logbook import log
from .store import Store, draft_key

PERSONA_PATH = Path(__file__).resolve().parent / "persona.md"

SYSTEM_RULES = """你正在 B站 评论区，以小号「大肥鱼」的身份回复别人。

输出要求：
- 只输出一条回复正文，10～40 字，口语化，像真人随手打的。
- 先正面回应对方说的内容，再（可选）带一句大肥鱼的梗。
- 不要引号、不要前缀、不要解释、不要换行、不要 emoji 堆砌。
- 对方如果问事实性问题，答不上来就说不知道，不要编。"""

RISKY_WORDS = (
    "政治", "政府", "台独", "港独", "六四", "辱华", "去死", "死全家",
    "傻逼", "智障", "滚出", "举报你", "开盒",
)


def load_persona(cfg: dict) -> str:
    rel = cfg.get("persona_file") or "bbot/persona.md"
    path = Path(rel)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / rel
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return PERSONA_PATH.read_text(encoding="utf-8").strip()


def is_risky(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in RISKY_WORDS)


def _describe_trigger(trigger: dict) -> str:
    if trigger.get("kind") == "at":
        head = "有人 @ 了你。"
    else:
        head = "有人回复了你的评论。"
    lines = [head, f"对方：{trigger.get('nickname') or '某人'}"]
    if trigger.get("text"):
        lines.append(f"对方原话：「{trigger['text'][:200]}」")
    if trigger.get("context"):
        lines.append(f"你原来那条评论：「{trigger['context'][:200]}」")
    return "\n".join(lines)


def build_messages(cfg: dict, trigger: dict, video: dict | None) -> list[dict]:
    persona = load_persona(cfg)
    parts = []
    if video:
        parts.append(f"视频标题：{video.get('title', '')}")
        if video.get("owner"):
            parts.append(f"UP主：{video['owner']}")
        if video.get("desc"):
            parts.append(f"简介：{video['desc'][:200]}")
        if video.get("tname"):
            parts.append(f"分区：{video['tname']}")
    parts.append(_describe_trigger(trigger))
    parts.append(
        "任务：写一条回复。要求：接住对方那句话，人设别掉，40 字以内。"
        "如果对方只是 @ 你但没说什么具体内容，就 playful 地反问一句或接个梗。"
    )
    return [
        {"role": "system", "content": persona + "\n\n" + SYSTEM_RULES},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_dm_messages(cfg: dict, nickname: str, text: str, recent: list[str] | None = None) -> list[dict]:
    """私信回复的提示词。比评论区稍微放松一点，但一样不许编。"""
    persona = load_persona(cfg)
    rules = """你正在 B站 私信里回复一个人，身份是小号「大肥鱼」。

输出要求：
- 只输出回复正文，20～60 字，口语化，像真人打字。
- 私信比评论区随意一点，但别提钱、别提账号、别发链接、别要联系方式。
- 对方如果是求助/提问：能给答案就给，给不了就说不知道。
- 不要引号、不要前缀、不要换行、不要 emoji 堆砌。"""

    parts = [f"对方昵称：{nickname or '某人'}", f"对方说：「{text[:300]}」"]
    if recent:
        parts.append("前几句聊天记录（供参考，别复读）：\n" + "\n".join(recent[-4:]))
    parts.append("任务：回一条私信，40 字左右，人设别掉，也别太自来熟。")
    return [
        {"role": "system", "content": persona + "\n\n" + rules},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def fetch_triggers(client: BiliClient, limit_per_feed: int = 20) -> list[dict]:
    """拉「@我的」和「回复我的」。任何一个失败都不影响另一个。"""
    triggers: list[dict] = []
    for name, fetcher in (("at", client.at_me), ("reply", client.reply_me)):
        try:
            items, _offset, _is_end = fetcher()
        except BiliError as exc:
            log(f"  ! 拉 {name} 失败：{exc.code} {exc.message}")
            continue
        triggers.extend(items[:limit_per_feed])
    triggers.sort(key=lambda t: t.get("timestamp", 0))
    return triggers


def video_context(client: BiliClient, trigger: dict) -> dict | None:
    bvid = trigger.get("bvid") or ""
    aid = int(trigger.get("aid") or 0)
    if not (bvid or aid):
        return None
    try:
        info = client.view(bvid=bvid, aid=aid)
    except BiliError as exc:
        log(f"  ! 取视频信息失败：{exc.message}")
        return {"title": trigger.get("title") or "", "owner": "", "desc": "", "tname": ""}
    return info


def resolve_target(client: BiliClient, oid: int, trigger: dict) -> tuple[int, int, str]:
    """算出「回复挂在谁底下」：root = 顶层评论，parent = 被回的那条。

    B站 的发评论接口如果不给 root/parent，就会变成一条新的顶层评论——
    也就是「回复跑到了视频底下」。实测 @ 消息里 rpid 藏在 source_id，
    这里再兜一层：没有 parent 就退回 root，别让它裸奔成新评论。
    """
    root = int(trigger.get("root") or 0)
    parent = int(trigger.get("rpid") or 0)
    uid = int(trigger.get("uid") or 0)
    text = (trigger.get("text") or "").strip()

    if parent:
        return (root or parent), parent, "回复到被 @ 的那条评论下"

    if root:
        # 只知道顶层评论：去它的子回复里找具体那一条
        try:
            children = client.reply_thread(oid, root)
        except BiliError:
            children = []
        needle = text[:20]
        for child in children:
            if child.get("uid") == uid and needle and needle in (child.get("message") or ""):
                return root, int(child["rpid"] or 0), "在子回复里定位到具体那条"
        return root, root, "回复到这条评论下"

    found = client.find_comment(oid, uid, text)
    if found:
        return found["root"], found["rpid"], "按内容在评论区反查到的"

    return 0, 0, "没定位到评论，发出去会变成视频下的新评论"


def run_once(store: Store, client: BiliClient, cfg: dict, quiet: bool = False) -> dict:
    """扫一轮消息，把新的 @ 变成草稿。返回本轮统计。"""
    result = {"scanned": 0, "new": 0, "skipped": 0, "auto_sent": 0, "errors": []}
    me = client.me()
    if not me.get("is_login"):
        result["errors"].append(f"未登录：{me.get('reason', '检查 Cookie')}")
        return result
    my_uid = int(me.get("mid") or client.uid or 0)

    triggers = fetch_triggers(client)
    result["scanned"] = len(triggers)
    first_run = len(store.list_drafts(status=None, limit=1)) == 0
    window = float(cfg.get("mode", {}).get("first_run_window_minutes", 120)) * 60
    now = time.time()
    limits = cfg.get("limits", {})

    for trigger in triggers:
        uid = int(trigger.get("uid") or 0)
        if uid and uid == my_uid:
            result["skipped"] += 1
            continue
        if trigger.get("timestamp") and first_run and now - trigger["timestamp"] > window:
            result["skipped"] += 1
            continue
        if not (trigger.get("bvid") or trigger.get("oid")):
            result["skipped"] += 1
            continue

        key = draft_key(trigger.get("kind", ""), uid, trigger.get("rpid"), trigger.get("text", ""))
        if store.has_draft(key):
            result["skipped"] += 1
            continue

        video = video_context(client, trigger)
        oid = int(trigger.get("oid") or trigger.get("aid") or (video or {}).get("aid") or 0)
        root, parent, note = resolve_target(client, oid, trigger)
        if not parent:
            log(f"  ! 这条没定位到评论：{(trigger.get('text') or '')[:20]}")
        body, angle = "", ""
        if llm.available(cfg):
            try:
                raw = llm.chat(cfg, build_messages(cfg, trigger, video))
                body = llm.clean_line(raw)[: int(limits.get("max_reply_chars", 80))]
                angle = "模型生成"
            except llm.LLMError as exc:
                angle = f"模型失败：{exc}"[:120]
                result["errors"].append(str(exc)[:160])
        else:
            angle = "未配模型，手动写一句"

        draft_id = store.add_draft(
            mkey=key,
            kind=trigger.get("kind", ""),
            trigger_uid=uid,
            trigger_name=trigger.get("nickname", ""),
            trigger_text=trigger.get("text", ""),
            context=trigger.get("context", ""),
            bvid=trigger.get("bvid", ""),
            oid=oid,
            root=root,
            parent=parent,
            title=(video or {}).get("title") or trigger.get("title", ""),
            uri=trigger.get("uri", ""),
            body=body,
            angle=angle,
            status="pending",
            note=note,
        )
        if draft_id is None:
            result["skipped"] += 1
            continue
        result["new"] += 1

        if cfg.get("mode", {}).get("auto_send") and body and not is_risky(body):
            ok, message = can_send(store, cfg)
            if not ok:
                store.update_draft(draft_id, error=f"自动发送被限速：{message}")
                continue
            draft = store.get_draft(draft_id) or {}
            sent_ok, send_msg = deliver(store, client, cfg, draft)
            if sent_ok:
                result["auto_sent"] += 1
            else:
                result["errors"].append(send_msg[:160])

    if result["new"] and not quiet:
        log(f"  + 新增 {result['new']} 条待发")
    return result


def can_send(store: Store, cfg: dict, ignore_gap: bool = False) -> tuple[bool, str]:
    limits = cfg.get("limits", {})
    max_hour = int(limits.get("max_per_hour", 6))
    max_day = int(limits.get("max_per_day", 30))
    gap = float(limits.get("min_gap_seconds", 60))
    hour_count = store.sent_since_kind("at", 3600) + store.sent_since_kind("reply", 3600)
    day_count = store.sent_since_kind("at", 86400) + store.sent_since_kind("reply", 86400)
    if hour_count >= max_hour:
        return False, f"一小时内已发 {max_hour} 条"
    if day_count >= max_day:
        return False, f"一天已发 {max_day} 条"
    last = max(store.last_sent_at("at"), store.last_sent_at("reply"))
    if not ignore_gap and last and time.time() - last < gap:
        wait = int(gap - (time.time() - last))
        return False, f"还要等 {wait} 秒（最小间隔 {int(gap)}s）"
    return True, "ok"


def deliver(store: Store, client: BiliClient, cfg: dict, draft: dict,
            body: str | None = None, ignore_gap: bool = False) -> tuple[bool, str]:
    """真正发出去。失败时把 B站 的原始报错存进草稿，界面上能看到原因。

    手动点发送时 ignore_gap=True：人工点两下是人的决定，但仍守每小时/每天上限。
    """
    draft_id = int(draft["id"])
    message = (body if body is not None else draft.get("body") or "").strip()
    if not message:
        return False, "内容是空的，发什么呀"

    if (draft.get("kind") or "") == "dm":
        return _deliver_dm(store, client, cfg, draft, message)

    oid = int(draft.get("oid") or 0)
    if not oid:
        return False, "不知道这条评论挂在哪个视频下（oid 缺失），只能在网页里手动回"

    ok, why = can_send(store, cfg, ignore_gap=ignore_gap)
    if not ok:
        return False, f"限速：{why}"

    root = int(draft.get("root") or 0)
    parent = int(draft.get("parent") or 0)
    note = draft.get("note") or ""

    # 没有 parent 就等于发一条新的顶层评论——这不是「回复」，用户会看到它跑到视频底下
    if not parent:
        found = client.find_comment(
            oid, int(draft.get("trigger_uid") or 0), draft.get("trigger_text") or ""
        )
        if found:
            root, parent = found["root"], found["rpid"]
            note = "发送前反查到了目标评论"
            store.update_draft(draft_id, root=root, parent=parent, note=note)
        elif not cfg.get("mode", {}).get("allow_top_level_reply", False):
            return False, (
                "找不到被 @ 的那条评论，发出去会变成视频下的新评论。"
                "已在 config.json 里留了开关：mode.allow_top_level_reply=true 才允许这么发"
            )

    referer = (
        f"https://www.bilibili.com/video/{draft['bvid']}"
        if draft.get("bvid")
        else "https://www.bilibili.com/"
    )
    try:
        data = client.post_comment(
            oid=oid,
            message=message,
            root=root,
            parent=parent,
            referer=referer,
        )
    except BiliError as exc:
        store.update_draft(draft_id, status="failed", error=f"[{exc.code}] {exc.message}"[:300])
        return False, f"[{exc.code}] {exc.message}"

    store.update_draft(
        draft_id,
        status="sent",
        body=message,
        sent_at=time.time(),
        error="",
        root=root,
        parent=parent,
        note=note,
    )
    return True, f"已发到 {'评论 #' + str(parent) if parent else '视频下'}（rpid={data.get('rpid') or '未知'}）"


def _deliver_dm(store: Store, client: BiliClient, cfg: dict, draft: dict,
                message: str) -> tuple[bool, str]:
    draft_id = int(draft["id"])
    uid = int(draft.get("oid") or 0)
    if not uid:
        return False, "不知道这条私信回给谁"

    dm_cfg = cfg.get("dm", {})
    max_hour = int(dm_cfg.get("max_per_hour", 5))
    gap = float(dm_cfg.get("min_gap_seconds", 30))
    if store.sent_since_kind("dm", 3600) >= max_hour:
        return False, f"私信限速：一小时最多 {max_hour} 条"
    last = store.last_sent_at("dm")
    if last and time.time() - last < gap:
        return False, f"私信还要等 {int(gap - (time.time() - last))} 秒"

    try:
        result = client.send_dm(uid, message)
    except BiliError as exc:
        store.update_draft(draft_id, status="failed", error=f"[{exc.code}] {exc.message}"[:300])
        return False, f"[{exc.code}] {exc.message}"

    store.update_draft(draft_id, status="sent", body=message, sent_at=time.time(), error="")
    variant = result.get("variant")
    return True, f"私信已发（dev_id 方案 {variant}）"
