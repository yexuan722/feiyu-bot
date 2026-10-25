# -*- coding: utf-8 -*-
"""内存日志环：终端打印一份，本地驾驶舱页面也能看到一份。"""

from __future__ import annotations

import collections
import threading
import time

_LINES: collections.deque = collections.deque(maxlen=200)
_LOCK = threading.Lock()


def log(message: str) -> None:
    text = str(message)
    with _LOCK:
        _LINES.append({"t": time.time(), "msg": text})
    print(text, flush=True)


def tail(limit: int = 60) -> list[dict]:
    with _LOCK:
        items = list(_LINES)[-limit:]
    return list(reversed(items))
