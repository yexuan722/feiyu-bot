# -*- coding: utf-8 -*-
"""发现链：热门 + 首页推荐 → 去重过滤 → 打分 → 按口味加权 → 每天挑固定条数。

每天一批（默认 10 条），带上 tag。你在面板上点感兴趣的 tag，那个 tag 下次加分，
但加分有上限，还掺了随机抖动——不会变成回音壁。
"""

from __future__ import annotations

import random
from datetime import datetime

from . import llm
from .api import BiliClient, BiliError
from .logbook import log
from .store import Store

SYSTEM = """你是 B站 内容策展助手。给用户挑「值得点开」的视频。

判断标准（按重要性）：
1. 有信息量或真有趣：能学到东西、有梗、有手艺、有故事，看完不亏。
2. 不要纯标题党、不要带货推广、不要直播回放、不要低质搬运合集。
3. 和用户兴趣相关加分，但兴趣之外真正好的东西也要能挑出来。

输出：只输出 JSON 数组，不要解释，不要代码围栏。
每个元素：{"i": 序号, "score": 0-10 的整数, "why": "15 字以内的理由"}
score 低于 5 的直接也给出来，别省略条目。"""


def _heuristic_score(video: dict, interests: list[str], block: list[str]) -> tuple[int, str]:
    title = f"{video.get('title', '')} {video.get('desc', '')} {video.get('tname', '')}"
    if any(word in title for word in block):
        return 0, "看起来像推广/低质"
    hits = [word for word in interests if word and word in title]
    if hits:
        return min(9, 5 + 2 * len(hits)), f"命中兴趣：{'、'.join(hits[:2])}"
    return 4, "没配模型，只能粗筛"


def _llm_score(cfg: dict, videos: list[dict], interests: list[str],
               block: list[str]) -> dict[int, tuple[int, str]]:
    out: dict[int, tuple[int, str]] = {}
    interests_text = "、".join(interests) if interests else "（用户没填，按大众口味）"
    for start in range(0, len(videos), 15):
        chunk = videos[start : start + 15]
        lines = []
        for offset, video in enumerate(chunk):
            lines.append(
                f"{start + offset}. [{video.get('tname', '')}] {video.get('title', '')}"
                f" | UP:{video.get('owner', '')} | {video.get('duration', 0)}秒"
                f" | 播放:{video.get('view_count', 0)}"
            )
        prompt = (
            f"用户兴趣：{interests_text}\n"
            f"屏蔽词：{'、'.join(block) if block else '无'}\n\n"
            "候选视频：\n" + "\n".join(lines) + "\n\n给每条打分，输出 JSON 数组。"
        )
        try:
            raw = llm.chat(
                cfg,
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1200,
            )
            parsed = llm.extract_json(raw)
        except (llm.LLMError, ValueError) as exc:
            log(f"  ! 打分失败，这批退回关键词兜底：{exc}")
            for offset, video in enumerate(chunk):
                out[start + offset] = _heuristic_score(video, interests, block)
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("i"))
                score = int(float(entry.get("score", 0)))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(videos):
                out[index] = (max(0, min(10, score)), str(entry.get("why", ""))[:40])
    return out


def collect_candidates(client: BiliClient) -> list[dict]:
    videos: list[dict] = []
    try:
        for page in (1, 2):
            videos.extend(client.popular(pn=page, ps=30))
    except BiliError as exc:
        log(f"  ! 热门拉取失败：{exc.code} {exc.message}")
    try:
        videos.extend(client.recommend(ps=20))
    except BiliError as exc:
        log(f"  ! 推荐流拉取失败（wbi 或风控）：{exc.code} {exc.message}")

    seen: set[str] = set()
    unique = []
    for video in videos:
        bvid = video.get("bvid") or ""
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        unique.append(video)
    return unique


def _tag_bonus(store: Store, tags: list[str], cfg: dict) -> tuple[float, str]:
    """口味加分：每个 tag 的权重 × 系数，总和封顶，不让单个 tag 一手遮天。"""
    discover_cfg = cfg.get("discover", {})
    scale = float(discover_cfg.get("tag_weight_scale", 0.5))
    cap = float(discover_cfg.get("tag_bonus_max", 1.5))
    raw = sum(store.tag_weight(tag) * scale for tag in tags if tag)
    bonus = max(-cap, min(cap, raw))
    if abs(bonus) < 0.05:
        return 0.0, ""
    liked = [f"{tag}{store.tag_weight(tag):+.1f}" for tag in tags if store.tag_weight(tag) > 0]
    disliked = [tag for tag in tags if store.tag_weight(tag) < 0]
    if bonus > 0:
        hint = f"按口味加了点分（{'、'.join(liked[:2])}）"
    else:
        hint = f"你屏蔽过这类（{'、'.join(disliked[:2])}）"
    return bonus, hint


def _fill_tags(client: BiliClient, videos: list[dict], limit: int) -> None:
    """只给「有希望入选」的候选补 tag，避免一天几百个请求。"""
    for index, video in enumerate(videos):
        if index >= limit:
            break
        aid = int(video.get("aid") or 0)
        if not aid:
            continue
        try:
            tags = client.tags(aid)
        except BiliError:
            continue
        if tags:
            video["tags"] = tags[:8]


def run_once(store: Store, client: BiliClient, cfg: dict, quiet: bool = False) -> dict:
    discover_cfg = cfg.get("discover", {})
    interests = [str(item) for item in cfg.get("interests", [])]
    block = [str(item) for item in discover_cfg.get("block_keywords", [])]
    min_duration = int(discover_cfg.get("min_duration_seconds", 60))
    max_duration = int(discover_cfg.get("max_duration_seconds", 3600))
    min_score = int(discover_cfg.get("min_score", 6))
    count = int(discover_cfg.get("daily_count") or discover_cfg.get("top_n") or 10)
    jitter_range = float(discover_cfg.get("randomness", 1.2))
    tag_limit = int(discover_cfg.get("tag_fetch_limit", 30))

    result = {"fetched": 0, "scored": 0, "picked": 0, "errors": []}
    candidates = collect_candidates(client)
    result["fetched"] = len(candidates)

    fresh = []
    for video in candidates:
        bvid = video.get("bvid") or ""
        duration = int(video.get("duration") or 0)
        if store.is_seen(bvid):
            continue
        if duration and not (min_duration <= duration <= max_duration):
            store.mark_seen(bvid)
            continue
        title = video.get("title", "")
        if any(word in title for word in block):
            store.mark_seen(bvid)
            continue
        fresh.append(video)

    if not fresh:
        if not quiet:
            log("  · 今天没有新面孔，等明天")
        return result

    if llm.available(cfg):
        scores = _llm_score(cfg, fresh, interests, block)
    else:
        scores = {i: _heuristic_score(v, interests, block) for i, v in enumerate(fresh)}
    result["scored"] = len(scores)

    # 先按模型分排一轮，只给有机会的候选补 tag（省请求）
    ranked = sorted(
        ((i, scores.get(i, (0, ""))) for i in range(len(fresh))),
        key=lambda pair: pair[1][0],
        reverse=True,
    )
    shortlist = [i for i, (score, _) in ranked if score >= min_score - 2][:tag_limit]
    # _fill_tags 直接改的是 fresh 里的 dict，补完 tag 原处就能用
    _fill_tags(client, [fresh[i] for i in shortlist], tag_limit)

    picked = []
    for index, video in enumerate(fresh):
        store.mark_seen(video.get("bvid", ""))
        base, why = scores.get(index, (0, ""))
        if base <= 0:
            continue
        tags = list(video.get("tags") or [])
        if video.get("tname"):
            tags.append(video["tname"])
        bonus, hint = _tag_bonus(store, tags, cfg)
        # 随机抖动：让同分视频轮流上位，别每次都是同一批
        jitter = random.uniform(-jitter_range, jitter_range)
        final = base + bonus + jitter
        if base < min_score - 1 or final < min_score:
            continue
        video = dict(video)
        video["score"] = int(round(max(0, min(10, final))))
        video["reason"] = " · ".join(part for part in (why, hint) if part)[:60]
        video["tags"] = tags[:6]
        if store.add_feed(video):
            picked.append(video)

    picked.sort(key=lambda v: v["score"], reverse=True)
    picked = picked[:count]
    result["picked"] = len(picked)
    if picked and not quiet:
        log(f"  + 今天挑了 {len(picked)} 条：")
        for video in picked:
            tags = "、".join(video.get("tags", [])[:3])
            log(f"    [{video['score']}] {video['title'][:34]} | {tags}")
    return result


def daily_due(store: Store, cfg: dict) -> bool:
    """daily 模式：到点且今天还没跑过。"""
    discover_cfg = cfg.get("discover", {})
    if (discover_cfg.get("mode") or "daily") != "daily":
        return False
    at = str(discover_cfg.get("daily_at") or "09:00")
    try:
        hour, minute = (int(part) for part in at.split(":", 1))
    except (ValueError, TypeError):
        hour, minute = 9, 0
    now = datetime.now()
    last = store.get_meta("last_daily_date")
    if last == now.strftime("%Y-%m-%d"):
        return False
    if not last:
        # 从没跑过：第一次启动就先给一批，别让人干等到第二天
        return True
    return (now.hour, now.minute) >= (hour, minute)


def mark_daily(store: Store) -> None:
    store.set_meta("last_daily_date", datetime.now().strftime("%Y-%m-%d"))
