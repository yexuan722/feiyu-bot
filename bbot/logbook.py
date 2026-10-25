# -*- coding: utf-8 -*-
"""日志三份：内存环（面板看）、终端（前台看）、文件（后台跑时看）。

后台用 pythonw 跑的时候没有终端，stdout 是 None，print 会炸——所以这里全包了 try。
"""

from __future__ import annotations

import collections
import sys
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"

_LINES: collections.deque = collections.deque(maxlen=200)
_LOCK = threading.Lock()


def _write_file(text: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"feiyu-{time.strftime('%Y-%m-%d')}.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
    except OSError:
        pass


def log(message: str) -> None:
    text = str(message)
    with _LOCK:
        _LINES.append({"t": time.time(), "msg": text})
    try:
        if sys.stdout is not None:
            print(text, flush=True)
    except Exception:  # noqa: BLE001  pythonw 下 stdout 可能是个坏流
        pass
    _write_file(text)


def tail(limit: int = 60) -> list[dict]:
    with _LOCK:
        items = list(_LINES)[-limit:]
    return list(reversed(items))


def log_path() -> Path:
    return LOG_DIR / f"feiyu-{time.strftime('%Y-%m-%d')}.log"
