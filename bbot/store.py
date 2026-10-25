# -*- coding: utf-8 -*-
"""本地状态库（SQLite）：待发队列、已投喂视频、去重、发送流水。

全部落在 data/feiyu.db 一个文件里，删掉就等于清空记忆。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "feiyu.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mkey          TEXT UNIQUE,
    kind          TEXT,
    created       REAL,
    trigger_uid   INTEGER,
    trigger_name  TEXT,
    trigger_text  TEXT,
    context       TEXT,
    bvid          TEXT,
    oid           INTEGER,
    root          INTEGER,
    parent        INTEGER,
    title         TEXT,
    uri           TEXT,
    body          TEXT,
    angle         TEXT,
    status        TEXT DEFAULT 'pending',
    error         TEXT,
    sent_at       REAL,
    note          TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS feed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid        TEXT UNIQUE,
    aid         INTEGER,
    title       TEXT,
    owner       TEXT,
    tname       TEXT,
    duration    INTEGER,
    view_count  INTEGER,
    pic         TEXT,
    url         TEXT,
    score       INTEGER,
    reason      TEXT,
    source      TEXT,
    created     REAL,
    status      TEXT DEFAULT 'new',
    notified    INTEGER DEFAULT 0,
    tags        TEXT DEFAULT '',
    batch       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
CREATE TABLE IF NOT EXISTS tag_pref (
    tag       TEXT PRIMARY KEY,
    weight    REAL DEFAULT 0,
    likes     INTEGER DEFAULT 0,
    dislikes  INTEGER DEFAULT 0,
    updated   REAL
);
CREATE TABLE IF NOT EXISTS seen (
    bvid    TEXT PRIMARY KEY,
    created REAL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_feed_score ON feed(score);
"""

DRAFT_FIELDS = (
    "mkey", "kind", "created", "trigger_uid", "trigger_name", "trigger_text",
    "context", "bvid", "oid", "root", "parent", "title", "uri", "body", "angle",
    "status", "error", "sent_at", "note",
)


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """老库补列：SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查再加。"""
        def cols(table: str) -> set[str]:
            return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

        if "note" not in cols("drafts"):
            self.conn.execute("ALTER TABLE drafts ADD COLUMN note TEXT DEFAULT ''")
        if "tags" not in cols("feed"):
            self.conn.execute("ALTER TABLE feed ADD COLUMN tags TEXT DEFAULT ''")
        if "batch" not in cols("feed"):
            self.conn.execute("ALTER TABLE feed ADD COLUMN batch TEXT DEFAULT ''")
            # 老数据归到「今天」这一批，免得升级后一片空白
            self.conn.execute("UPDATE feed SET batch = date('now', 'localtime') WHERE batch = ''")
        self.conn.commit()

    # ------------------------------------------------------------ drafts

    def add_draft(self, **fields) -> int | None:
        """返回新行 id；这条已经存在（mkey 撞了）就返回 None。"""
        fields = {k: v for k, v in fields.items() if k in DRAFT_FIELDS}
        fields.setdefault("created", time.time())
        fields.setdefault("status", "pending")
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        try:
            cur = self.conn.execute(
                f"INSERT INTO drafts ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def get_draft(self, draft_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def has_draft(self, mkey: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM drafts WHERE mkey = ?", (mkey,)).fetchone()
        return row is not None

    def list_drafts(self, status: str | None = "pending", limit: int = 100) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM drafts WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_draft(self, draft_id: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in DRAFT_FIELDS}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE drafts SET {sets} WHERE id = ?", (*fields.values(), draft_id))
        self.conn.commit()

    # ------------------------------------------------------------ feed

    def add_feed(self, item: dict) -> int | None:
        row = {
            "bvid": item.get("bvid", ""),
            "aid": item.get("aid", 0),
            "title": item.get("title", ""),
            "owner": item.get("owner", ""),
            "tname": item.get("tname", ""),
            "duration": item.get("duration", 0),
            "view_count": item.get("view_count", 0),
            "pic": item.get("pic", ""),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "reason": item.get("reason", ""),
            "source": item.get("source", ""),
            "created": time.time(),
            "status": "new",
            "notified": 0,
            "tags": json.dumps(item.get("tags") or [], ensure_ascii=False),
            "batch": time.strftime("%Y-%m-%d"),
        }
        try:
            cur = self.conn.execute(
                "INSERT INTO feed (bvid, aid, title, owner, tname, duration, view_count, pic, url,"
                " score, reason, source, created, status, notified, tags, batch)"
                " VALUES (:bvid,:aid,:title,:owner,:tname,:duration,:view_count,:pic,:url,"
                " :score,:reason,:source,:created,:status,:notified,:tags,:batch)",
                row,
            )
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def list_feed(self, limit: int = 60, include_hidden: bool = False) -> list[dict]:
        where = "" if include_hidden else "WHERE status != 'hidden'"
        rows = self.conn.execute(
            f"SELECT * FROM feed {where} ORDER BY score DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item.get("tags") or "[]")
            except json.JSONDecodeError:
                item["tags"] = []
            out.append(item)
        return out

    def list_feed_latest_batch(self, limit: int = 10) -> list[dict]:
        """只取「最近一批」的推荐——每天一批，不把历史堆在面板上。"""
        row = self.conn.execute(
            "SELECT batch FROM feed WHERE status != 'hidden' AND batch != ''"
            " ORDER BY batch DESC LIMIT 1"
        ).fetchone()
        if not row:
            return []
        batch = row["batch"]
        rows = self.conn.execute(
            "SELECT * FROM feed WHERE batch = ? AND status != 'hidden'"
            " ORDER BY score DESC, id DESC LIMIT ?",
            (batch, limit),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item.get("tags") or "[]")
            except json.JSONDecodeError:
                item["tags"] = []
            out.append(item)
        return out

    def feed_total(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM feed WHERE status != 'hidden'"
        ).fetchone()
        return int(row["n"])

    def set_feed_tags(self, feed_id: int, tags: list[str]) -> None:
        self.conn.execute(
            "UPDATE feed SET tags = ? WHERE id = ?",
            (json.dumps(tags, ensure_ascii=False), feed_id),
        )
        self.conn.commit()

    def feed_missing_tags(self, limit: int = 40) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM feed WHERE status != 'hidden' AND (tags IS NULL OR tags = '' OR tags = '[]')"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def hide_feed(self, feed_id: int) -> None:
        self.conn.execute("UPDATE feed SET status = 'hidden' WHERE id = ?", (feed_id,))
        self.conn.commit()

    # ------------------------------------------------------------ 口味偏好

    def tag_weight(self, tag: str) -> float:
        row = self.conn.execute("SELECT weight FROM tag_pref WHERE tag = ?", (tag,)).fetchone()
        return float(row["weight"]) if row else 0.0

    def bump_tag(self, tag: str, delta: float) -> float:
        """喜欢 +1，不喜欢 -1；权重限幅在 [-3, 3]，一次点击不会把推荐带偏。"""
        tag = (tag or "").strip()
        if not tag:
            return 0.0
        now = time.time()
        liked = 1 if delta > 0 else 0
        disliked = 1 if delta < 0 else 0
        self.conn.execute(
            "INSERT INTO tag_pref (tag, weight, likes, dislikes, updated) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(tag) DO UPDATE SET"
            "   weight = MAX(-3.0, MIN(3.0, weight + ?)),"
            "   likes = likes + ?, dislikes = dislikes + ?, updated = ?",
            (tag, max(-3.0, min(3.0, delta)), liked, disliked, now,
             delta, liked, disliked, now),
        )
        self.conn.commit()
        return self.tag_weight(tag)

    def list_tag_prefs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tag_pref WHERE ABS(weight) > 0.01 ORDER BY ABS(weight) DESC, updated DESC"
            " LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_tags(self) -> None:
        self.conn.execute("DELETE FROM tag_pref")
        self.conn.commit()

    # ------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, str(value), str(value)),
        )
        self.conn.commit()

    def pending_notify(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM feed WHERE notified = 0 AND status = 'new' ORDER BY score DESC LIMIT 10"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.conn.execute(f"UPDATE feed SET notified = 1 WHERE id IN ({marks})", tuple(ids))
        self.conn.commit()

    # ------------------------------------------------------------ seen

    def is_seen(self, bvid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE bvid = ?", (bvid,)).fetchone() is not None

    def mark_seen(self, bvid: str) -> None:
        try:
            self.conn.execute(
                "INSERT INTO seen (bvid, created) VALUES (?, ?)", (bvid, time.time())
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    # ------------------------------------------------------------ stats

    def sent_since(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status = 'sent' AND sent_at > ?", (cutoff,)
        ).fetchone()
        return int(row["n"])

    def sent_since_kind(self, kind: str, seconds: float) -> int:
        cutoff = time.time() - seconds
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status = 'sent' AND sent_at > ? AND kind = ?",
            (cutoff, kind),
        ).fetchone()
        return int(row["n"])

    def last_sent_at(self, kind: str | None = None) -> float:
        if kind:
            row = self.conn.execute(
                "SELECT MAX(sent_at) AS t FROM drafts WHERE status = 'sent' AND kind = ?", (kind,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(sent_at) AS t FROM drafts WHERE status = 'sent'"
            ).fetchone()
        return float(row["t"] or 0)

    def stats(self) -> dict:
        def count(sql: str, *args) -> int:
            return int(self.conn.execute(sql, args).fetchone()[0])

        return {
            "pending": count("SELECT COUNT(*) FROM drafts WHERE status = 'pending'"),
            "sent_today": self.sent_since(86400),
            "sent_hour": self.sent_since(3600),
            "failed": count("SELECT COUNT(*) FROM drafts WHERE status = 'failed'"),
            "feed": count("SELECT COUNT(*) FROM feed WHERE status != 'hidden'"),
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def draft_key(kind: str, uid, rpid, text: str) -> str:
    """去重键：同一个人、同一条评论，不重复回购。"""
    raw = f"{kind}|{uid}|{rpid}|{(text or '')[:60]}"
    import hashlib

    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
