# -*- coding: utf-8 -*-
"""模型调用：任何 OpenAI 兼容接口都能接（DeepSeek / 硅基流动 / 智谱 / Ollama / OpenRouter）。

没配 key 也不影响主流程——互动会退化成「只排队不生成」，发现会退化成关键词打分。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


def available(cfg: dict) -> bool:
    llm = cfg.get("llm", {})
    return bool((llm.get("base_url") or "").strip() and (llm.get("model") or "").strip())


def endpoint(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def chat(cfg: dict, messages: list[dict], temperature: float | None = None,
         max_tokens: int | None = None, timeout: float | None = None) -> str:
    llm = cfg.get("llm", {})
    if not available(cfg):
        raise LLMError("还没配模型（config.json 的 llm.base_url / llm.model）")
    url = endpoint(llm.get("base_url", ""))
    payload = {
        "model": llm.get("model"),
        "messages": messages,
        "temperature": llm.get("temperature", 0.9) if temperature is None else temperature,
        "max_tokens": max_tokens or llm.get("max_tokens", 400),
        "stream": False,
    }
    extra = llm.get("extra_body")
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if llm.get("api_key"):
        headers["Authorization"] = f"Bearer {llm['api_key']}"

    last = "未知错误"
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout or llm.get("timeout", 60)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices") or []
            if not choices:
                raise LLMError(f"模型没返回 choices：{json.dumps(body, ensure_ascii=False)[:200]}")
            return (choices[0].get("message") or {}).get("content", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt < 3:
            time.sleep(1.2 * attempt)
    raise LLMError(f"{url} 调用失败 → {last}")


def extract_json(raw: str):
    """模型爱在 JSON 外面裹一层 ```json，或者先唠两句，这里硬掏。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"模型返回的不是 JSON：{text[:160]}")


def clean_line(raw: str) -> str:
    """评论正文清洗：去引号、去换行、去掉模型爱加的前缀。"""
    text = (raw or "").strip()
    text = re.sub(r"^```.*?```$", "", text, flags=re.S).strip()
    text = re.split(r"\n\s*\n", text)[0].strip()
    for prefix in ("评论：", "评论:", "回复：", "回复:", "答：", "输出："):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.strip().strip("\"'“”‘’「」").strip()
    text = re.sub(r"\s*\n\s*", " ", text)
    return text
