# -*- coding: utf-8 -*-
"""WBI 签名。

B站 2023 年后给一批接口加了 wbi 校验：把同一个请求的参数按规则重排、
拼上从 nav 接口拿到的 img_key/sub_key 混合出来的密钥，再算 md5 当 w_rid。
没有签名，playurl / 推荐流 这些接口直接 -403。

实现参考公开的算法描述，纯本地计算，不发任何东西出去。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse

# 官方前端打包里的置换表，长度必须是 64 且每个下标只出现一次
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_FORBIDDEN = "!'()*"


def mixin_key(orig: str) -> str:
    """把 img_key + sub_key 按置换表重排，取前 32 位当密钥。"""
    if len(orig) < 64:
        raise ValueError(f"wbi 原始 key 长度不对：{len(orig)}")
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sanitize(params: dict) -> dict:
    clean = {}
    for key, value in params.items():
        if value is None:
            continue
        text = str(value)
        clean[key] = "".join(ch for ch in text if ch not in _FORBIDDEN)
    return clean


def sign(params: dict, img_key: str, sub_key: str, wts: int | None = None) -> dict:
    """返回带 wts / w_rid 的新字典（不改原对象）。"""
    signed = sanitize(params)
    signed["wts"] = int(wts if wts is not None else time.time())
    ordered = {k: signed[k] for k in sorted(signed)}
    query = urllib.parse.urlencode(ordered)
    digest = hashlib.md5((query + mixin_key(img_key + sub_key)).encode("utf-8")).hexdigest()
    ordered["w_rid"] = digest
    return ordered


def keys_from_urls(img_url: str, sub_url: str) -> tuple[str, str]:
    """从 nav 返回的两个 URL 里取出文件名（去掉扩展名）当 key。"""
    def _pick(url: str) -> str:
        return url.rsplit("/", 1)[-1].split(".")[0]

    return _pick(img_url), _pick(sub_url)


def self_check() -> None:
    assert len(MIXIN_KEY_ENC_TAB) == 64, "置换表长度应为 64"
    assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64)), "置换表必须是 0..63 的排列"


if __name__ == "__main__":
    self_check()
    fake = "".join(chr(97 + i % 26) for i in range(64))
    out = sign({"a": "1", "b": "2"}, fake, fake, wts=1700000000)
    print("OK", out)
