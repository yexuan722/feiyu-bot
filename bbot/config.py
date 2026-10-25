# -*- coding: utf-8 -*-
"""配置加载：config.json + 环境变量覆盖。

优先级：环境变量 > config.json > 内置默认值。
敏感字段（Cookie / API Key）永远只在本地，不打印、不上传。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

DEFAULTS: dict = {
    "bili": {
        "sessdata": "",
        "bili_jct": "",
        "buvid3": "",
        "dedeuserid": "",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "timeout": 20,
    },
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.9,
        "max_tokens": 400,
        "timeout": 60,
        # 原样塞进请求体的额外参数，比如 DeepSeek 关思考模式：
        # {"thinking": {"type": "disabled"}}
        "extra_body": {},
    },
    "mode": {
        # false = 半自动：生成后进审批队列，你在驾驶舱点一下才发
        # true  = 全自动：只回「@我 / 回复我」，带限速
        "auto_send": False,
        "poll_seconds": 180,
        "discover_minutes": 60,
        # 首次运行只处理这么多分钟内的消息，免得一上来把历史 @ 全回一遍
        "first_run_window_minutes": 120,
    },
    "limits": {
        "max_per_hour": 6,
        "max_per_day": 30,
        "min_gap_seconds": 60,
        "max_reply_chars": 80,
    },
    "discover": {
        "top_n": 8,
        "min_score": 6,
        "min_duration_seconds": 60,
        "max_duration_seconds": 3600,
        "block_keywords": ["广告", "抽奖", "推广", "带货", "直播回放"],
    },
    "interests": [
        "游戏实况",
        "独立游戏",
        "硬核科普",
        "美食",
        "鬼畜",
        "编程/技术",
        "动画杂谈",
    ],
    "notify": {
        "webhook_url": "",
        # json_text / telegram / bark / 钉钉 / 飞书 都能塞
        "webhook_type": "json_text",
        "chat_id": "",
        "dm_to_uid": "",
        "dm_enabled": False,
        "webhook_enabled": False,
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "persona_file": "bbot/persona.md",
}

_ENV_MAP = {
    "FEIYU_SESSDATA": ("bili", "sessdata"),
    "FEIYU_BILI_JCT": ("bili", "bili_jct"),
    "FEIYU_BUID3": ("bili", "buvid3"),
    "FEIYU_UID": ("bili", "dedeuserid"),
    "FEIYU_API_KEY": ("llm", "api_key"),
    "FEIYU_BASE_URL": ("llm", "base_url"),
    "FEIYU_MODEL": ("llm", "model"),
    "FEIYU_AUTO_SEND": ("mode", "auto_send"),
    "FEIYU_PORT": ("server", "port"),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(value: str):
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    if low.isdigit():
        return int(low)
    return value


def load_config(path: Path | str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    target = Path(path) if path else CONFIG_PATH
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config.json 不是合法 JSON：{exc}") from exc
        cfg = _deep_merge(cfg, raw)
    for env_key, (section, field) in _ENV_MAP.items():
        if os.environ.get(env_key):
            cfg.setdefault(section, {})[field] = _coerce(os.environ[env_key])
    return cfg


def save_config(cfg: dict, path: Path | str | None = None) -> None:
    target = Path(path) if path else CONFIG_PATH
    target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def masked(value: str, keep: int = 4) -> str:
    """日志里只露头尾，别把 Cookie 泄到终端/截图里。"""
    value = (value or "").strip()
    if not value:
        return "(空)"
    if len(value) <= keep * 2:
        return value[:2] + "*" * max(1, len(value) - 2)
    return f"{value[:keep]}…{value[-keep:]}（{len(value)} 字符）"


def status(cfg: dict) -> dict:
    llm = cfg.get("llm", {})
    bili = cfg.get("bili", {})
    return {
        "cookie_ready": bool(bili.get("sessdata") and bili.get("bili_jct")),
        "cookie_hint": masked(bili.get("sessdata", "")),
        "uid": bili.get("dedeuserid") or "",
        "model_ready": bool(llm.get("base_url") and llm.get("model")),
        "model": llm.get("model") or "",
        "base_url": llm.get("base_url") or "",
        "auto_send": bool(cfg.get("mode", {}).get("auto_send")),
        "dm_enabled": bool(cfg.get("notify", {}).get("dm_enabled")),
        "webhook_enabled": bool(cfg.get("notify", {}).get("webhook_enabled")),
        "interests": cfg.get("interests", []),
    }
