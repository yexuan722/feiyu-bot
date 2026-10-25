#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼小号 bot —— 命令行入口。

    python feiyu_bot.py setup     向导：填 Cookie 和模型（推荐第一次用这个）
    python feiyu_bot.py up        起后台（网页 + 自动扫消息/捞视频）
    python feiyu_bot.py serve     只起网页，不自动干活
    python feiyu_bot.py check     体检：Cookie / 模型 / wbi 签名
    python feiyu_bot.py demo      塞点假数据，先看界面长什么样
    python feiyu_bot.py poll      手动扫一遍 @ 和回复
    python feiyu_bot.py dm        手动扫一遍私信
    python feiyu_bot.py discover  手动跑一轮推荐
    python feiyu_bot.py list      看看队列里有什么
    python feiyu_bot.py tags      看看/重置口味权重
    python feiyu_bot.py send --id 3 [--body "临时改一句"]
    python feiyu_bot.py dump      把原始 @ 消息存到 debug/
    python feiyu_bot.py dm-dump   把原始私信存到 debug/（私信接口不对时用）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for stream in (sys.stdout, sys.stderr):
    try:
        if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from bbot import config as config_mod  # noqa: E402
from bbot import discover as discover_mod  # noqa: E402
from bbot import dm as dm_mod  # noqa: E402
from bbot import interact, llm, notify  # noqa: E402
from bbot.api import BiliClient, BiliError  # noqa: E402
from bbot.logbook import log  # noqa: E402
from bbot import logbook  # noqa: E402
from bbot.server import Runtime, Worker, make_server  # noqa: E402
from bbot.store import Store  # noqa: E402


def build(args) -> tuple[dict, Store, BiliClient]:
    cfg = config_mod.load_config(args.config)
    store = Store(args.db)
    client = BiliClient(cfg)
    return cfg, store, client


# --------------------------------------------------------------------- 命令


def _ask(prompt: str, default: str = "") -> str:
    """带默认值的交互输入；管道/非交互环境下拿不到输入就返回默认值。"""
    hint = f"（默认 {default}）" if default else ""
    try:
        raw = input(f"{prompt}{hint}：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return raw or default


def parse_cookie(raw: str) -> dict:
    """把一整条 Cookie 字符串拆成字典。

    能吃的输入：
    - `SESSDATA=xxx; bili_jct=yyy; buvid3=zzz`
    - `Cookie: SESSDATA=xxx; ...`
    - DevTools 里复制的 `a=b&c=d`
    - 换行分隔的四行
    """
    text = (raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    text = text.replace("\r", "").replace("\n", ";").replace("&", ";")
    out: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip().strip(",")
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'").strip()
        if key and value:
            out[key] = value
    return out


def cmd_setup(args) -> int:
    """一步一步把 Cookie 和模型填进 config.json，填完顺手验一次。"""
    print("=" * 60)
    print("接入向导：B站 账号 + AI 模型")
    print("=" * 60)

    cfg = config_mod.load_config(args.config)
    target = Path(args.config) if args.config else config_mod.CONFIG_PATH
    login_ok = False

    # ---------------------------------------------------------- 1. Cookie
    print("\n【第 1 步】B站 小号的 Cookie\n")
    if not args.cookie:
        print("  1. 浏览器里用【小号】登录 bilibili.com（别用无痕窗口）")
        print("  2. F12 开发者工具 → Application(应用) → 左侧 Cookies → https://www.bilibili.com")
        print("  3. 找到这四个：SESSDATA / bili_jct / buvid3 / DedeUserID")
        print("  4. 懒人法：F12 → Network → 点任意一个请求 → Request Headers 里整条 Cookie 复制")
        print()

    raw = args.cookie or _ask("把整条 Cookie 粘这里（直接回车跳过）")
    cookies = parse_cookie(raw)
    for field in ("sessdata", "bili_jct", "buvid3", "dedeuserid"):
        if cookies.get(field):
            cfg["bili"][field] = cookies[field]

    if not cfg["bili"].get("sessdata"):
        cfg["bili"]["sessdata"] = _ask("  没识别到 SESSDATA，手动贴一个（回车跳过）")
    if not cfg["bili"].get("bili_jct"):
        cfg["bili"]["bili_jct"] = _ask("  没识别到 bili_jct，手动贴一个（回车跳过）")

    if not cfg["bili"].get("sessdata"):
        print("\n× 没有 SESSDATA，这一步跳过（没有 Cookie 就发不了评论，只能看投喂）")
    else:
        config_mod.save_config(cfg, target)
        print(f"\n✓ 已写入 {target}")
        print(f"  SESSDATA {config_mod.masked(cfg['bili']['sessdata'])}")

        client = BiliClient(cfg)
        me = client.me(refresh=True)
        if me.get("is_login"):
            login_ok = True
            print(f"✓ 登录成功：{me.get('uname')}（UID {me.get('mid')}）")
            if not cfg["bili"].get("dedeuserid"):
                cfg["bili"]["dedeuserid"] = str(me.get("mid") or "")
                config_mod.save_config(cfg, target)
        else:
            print(f"× 登录失败：{me.get('reason')}")
            print("  常见原因：SESSDATA 少抄了尾巴 / 已过期 / 抄的是别的浏览器的")

    # ---------------------------------------------------------- 2. 模型
    if args.no_llm:
        print("\n· 跳过模型配置")
    else:
        print("\n【第 2 步】接模型（随便一个 OpenAI 兼容接口）")
        print("  DeepSeek 官方：base_url=https://api.deepseek.com/v1  model=deepseek-flash（便宜大碗）")
        print("  本地 Ollama  ：base_url=http://127.0.0.1:11434/v1  model=qwen2.5:7b")
        print("  不想接就空着回车——@ 消息照样排队，你自己写正文就行\n")

        base_url = args.base_url or _ask(
            "  base_url", cfg["llm"].get("base_url") or "https://api.deepseek.com/v1"
        )

        # 最常见的两种手滑：把 key 贴进 base_url、把 base_url 写少 http
        api_key_guess = ""
        if base_url.lower().startswith(("sk-", "sk_")):
            print("  ! 这栏要的是接口地址，不是 key。这串看着像 API Key，本鱼先替你收着")
            api_key_guess = base_url
            base_url = "https://api.deepseek.com/v1"
        elif not base_url.lower().startswith("http"):
            print("  ! 这不像网址，先用默认 https://api.deepseek.com/v1")
            base_url = "https://api.deepseek.com/v1"

        model = args.model or _ask("  模型名", cfg["llm"].get("model") or "deepseek-flash")
        api_key = args.api_key
        if api_key is None:
            if api_key_guess:
                api_key = api_key_guess
                print("  刚才贴错位置的那串，本鱼当 API Key 用了")
            else:
                api_key = _ask("  API Key（回车跳过）")

        # DeepSeek 默认开思考模式，写 40 字评论用不上，白烧输出 token
        is_deepseek = "deepseek.com" in base_url
        if is_deepseek:
            print("  · 顺手关掉思考模式（评论用不上，省输出 token）")

        # 本地跑的模型（Ollama）不用 key，别因为「没填 key」把配置吞了
        is_local = any(host in base_url for host in ("127.0.0.1", "localhost", "0.0.0.0"))
        if api_key or is_local:
            cfg["llm"]["base_url"] = base_url
            cfg["llm"]["model"] = model
            cfg["llm"]["api_key"] = api_key or ""
            if is_deepseek:
                cfg["llm"]["extra_body"] = {"thinking": {"type": "disabled"}}
            config_mod.save_config(cfg, target)
            print(f"\n✓ 已写入模型配置：{model} @ {base_url}")
            try:
                started = time.time()
                answer = llm.chat(
                    cfg,
                    [{"role": "user", "content": "回两个字：收到"}],
                    temperature=0.2,
                    max_tokens=20,
                )
                print(f"✓ 模型通了（{time.time() - started:.1f}s）：{answer.strip()[:30]}")
            except llm.LLMError as exc:
                print(f"× 模型调用失败：{exc}")
                if is_local:
                    print("  Ollama 跑起来了吗？先 ollama serve，再 ollama pull " + model)
                else:
                    print("  key 对不对、有没有额度、base_url 是不是少了 /v1")
        else:
            print("· 没填 key，模型先空着")

    # ---------------------------------------------------------- 3. 收尾
    print("\n" + "=" * 60)
    print("下一步")
    print("=" * 60)
    print("  python feiyu_bot.py check    # 体检，全绿就能开工")
    print("  python feiyu_bot.py up       # 起锅，浏览器会自动打开驾驶舱")
    print("\n提醒：config.json 里是你的账号凭证，别截图别外传；它已经在 .gitignore 里了。")
    return 0 if login_ok else 1


def cmd_check(args) -> int:
    cfg, store, client = build(args)
    st = config_mod.status(cfg)
    print("=" * 58)
    print("配置")
    print("=" * 58)
    print(f"  Cookie      : {'已填' if st['cookie_ready'] else '没填'}  {st['cookie_hint']}")
    print(f"  UID         : {st['uid'] or '(空)'}")
    print(f"  模型        : {st['model'] or '(空)'}  {st['base_url'] or ''}")
    print(f"  模式        : {'全自动' if st['auto_send'] else '半自动（点一下才发）'}")
    print(f"  兴趣        : {'、'.join(st['interests']) or '(空)'}")
    print(f"  数据库      : {store.path}")

    print("\n" + "=" * 58)
    print("B站 登录态")
    print("=" * 58)
    code = 0
    if not st["cookie_ready"]:
        print("  × 没配 Cookie，先按 README 捞一下 SESSDATA 和 bili_jct")
        code = 1
    else:
        me = client.me(refresh=True)
        if me.get("is_login"):
            print(f"  ✓ 已登录：{me.get('uname')}（UID {me.get('mid')}，Lv{me.get('level')}）")
            if st.get("cookie_days_left") is not None:
                left = int(st["cookie_days_left"])
                flag = "⚠️ 快到期了，记得重新粘一次" if left < 14 else ""
                print(f"  · Cookie 到 {st.get('cookie_expires_at')} 过期，还剩 {left} 天 {flag}")
        else:
            print(f"  × 登录失败：{me.get('reason')}")
            print("    常见原因：SESSDATA 过期、复制少了尾巴、IP 变了")
            code = 1
        try:
            img, sub = client._wbi(force=True)
            print(f"  ✓ wbi key 拿到了：{img[:14]}… / {sub[:14]}…")
        except BiliError as exc:
            print(f"  × wbi 失败：{exc.code} {exc.message}")
            code = 1

    print("\n" + "=" * 58)
    print("模型")
    print("=" * 58)
    if not llm.available(cfg):
        print("  · 没配模型：互动只排队不生成，发现走关键词兜底。填 config.json 的 llm 段即可。")
    else:
        try:
            started = time.time()
            answer = llm.chat(
                cfg,
                [{"role": "user", "content": "用五个字回我：今天吃白饭。"}],
                temperature=0.2,
                max_tokens=40,
            )
            print(f"  ✓ 通了（{time.time() - started:.1f}s）：{answer.strip()[:40]}")
        except llm.LLMError as exc:
            print(f"  × 模型调用失败：{exc}")
            code = 1
    return code


def cmd_poll(args) -> int:
    cfg, store, client = build(args)
    result = interact.run_once(store, client, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 1


def cmd_discover(args) -> int:
    cfg, store, client = build(args)
    result = discover_mod.run_once(store, client, cfg)
    for line in notify.notify_new(store, client, cfg):
        print(f"  通知 {line}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_dm(args) -> int:
    cfg, store, client = build(args)
    result = dm_mod.run_once(store, client, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 1


def cmd_dm_dump(args) -> int:
    """私信接口报错时的排障入口：把会话和消息原样落盘。"""
    cfg, store, client = build(args)
    out_dir = HERE / "debug"
    out_dir.mkdir(exist_ok=True)
    try:
        sessions = client.dm_sessions()
    except BiliError as exc:
        print(f"× 拉会话失败：[{exc.code}] {exc.message}")
        print("  这多半说明 dev_id 或 Cookie 不对，私信这条链还用不了")
        return 1
    (out_dir / "dm_sessions.json").write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✓ {len(sessions)} 个会话 → {out_dir / 'dm_sessions.json'}")
    for session in sessions[:3]:
        talker = session["talker_id"]
        try:
            messages = client.dm_messages(talker, 10)
        except BiliError as exc:
            print(f"  × {talker}: [{exc.code}] {exc.message}")
            continue
        path = out_dir / f"dm_{talker}.json"
        path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  · {talker}: {len(messages)} 条 → {path.name}")
    return 0


def cmd_tags(args) -> int:
    cfg, store, client = build(args)
    if args.reset:
        store.reset_tags()
        print("✓ 口味权重已清零")

    if args.fix:
        rows = store.feed_missing_tags(40)
        print(f"有 {len(rows)} 条视频没 tag，开始补…")
        fixed = 0
        for row in rows:
            try:
                tags = client.archive_tags(bvid=row["bvid"], aid=int(row["aid"] or 0))
            except BiliError as exc:
                print(f"  × {row['bvid']}: [{exc.code}] {exc.message[:40]}")
                continue
            if tags:
                store.set_feed_tags(row["id"], tags[:6])
                fixed += 1
                print(f"  ✓ {row['bvid']}: {'、'.join(tags[:4])}")
            else:
                print(f"  · {row['bvid']}: 这视频确实没设 tag")
        print(f"补了 {fixed} 条")

    rows = store.list_tag_prefs(40)
    if not rows:
        print("还没有任何 tag 权重。去驾驶舱里点几个 tag 试试。")
        return 0
    print(f"{'tag':<16}{'权重':>7}{'赞':>5}{'踩':>5}")
    for row in rows:
        print(f"{row['tag']:<16}{row['weight']:>+7.1f}{row['likes']:>5}{row['dislikes']:>5}")
    return 0


TASK_NAME = "FeiyuBot"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
STARTUP_VBS = STARTUP_DIR / "feiyu-bot.vbs"


def _run_task_cmd(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["schtasks", *argv], capture_output=True, text=True, encoding="gbk", errors="replace"
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _task_command() -> str:
    """用 pythonw 起，才不会弹出一个黑窗口。"""
    python_exe = Path(sys.executable)
    pythonw = python_exe.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python_exe
    return f'"{exe}" "{HERE / "feiyu_bot.py"}" up --no-browser'


def _install_run_key(command: str) -> bool:
    """写 HKCU 的 Run 键：当前用户登录后自动跑，不需要管理员。"""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, command)
        return True
    except OSError:
        return False


def _uninstall_run_key() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, TASK_NAME)
        return True
    except (ImportError, OSError):
        return False


def _run_key_value() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, TASK_NAME)[0])
    except (ImportError, OSError):
        return ""


def _install_startup_vbs(command: str) -> bool:
    """注册表写不进去就退回启动文件夹：放个 vbs，用 0 号窗口静默启动。"""
    if not STARTUP_DIR.exists():
        return False
    try:
        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        content = (
            'Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.Run "{command}", 0, False\r\n'
        )
        STARTUP_VBS.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def cmd_install(args) -> int:
    command = _task_command()
    print("=" * 52)
    print("装成开机自启")
    print("=" * 52)
    print(f"  启动命令：{command}")

    if args.task:
        code, out = _run_task_cmd(
            ["/Create", "/TN", TASK_NAME, "/TR", command, "/SC", "ONLOGON", "/RL", "LIMITED", "/F"]
        )
        if code != 0:
            print(f"\n× 计划任务没建成：{out[:300]}")
            print("  这条要管理员权限。去掉 --task 用默认方式（不需要提权）。")
            return 1
        print("\n✓ 已装计划任务（需要管理员的那种）")
        print(f"  立刻启动： schtasks /Run /TN {TASK_NAME}")
        return 0

    if _install_run_key(command):
        print("\n✓ 已写入当前用户的启动项（登录 Windows 就自动跑，无窗口）")
    elif _install_startup_vbs(command):
        print(f"\n✓ 注册表写不进去，改用启动文件夹：{STARTUP_VBS}")
    else:
        print("\n× 两种自动启动都装不上。只能手动：开着终端跑 up。")
        return 1

    print(f"  日志文件：{logbook.LOG_DIR}")
    print(f"  面板地址：http://{args.host}:{args.port}")
    print("\n  现在就想起？直接双击 run.ps1，或：")
    print(f"    python feiyu_bot.py up            # 前台（关窗口=停）")
    print(f"    python feiyu_bot.py serve         # 只要面板")
    print("  卸载：python feiyu_bot.py uninstall")
    return 0


def cmd_uninstall(args) -> int:
    removed = []
    if _uninstall_run_key():
        removed.append("注册表启动项")
    if STARTUP_VBS.exists():
        try:
            STARTUP_VBS.unlink()
            removed.append("启动文件夹快捷方式")
        except OSError:
            pass
    code, _out = _run_task_cmd(["/Delete", "/TN", TASK_NAME, "/F"])
    if code == 0:
        removed.append("计划任务")
    if not removed:
        print("· 没找到任何自启配置，本来就没装")
        return 0
    print("✓ 已移除：" + "、".join(removed))
    print("  （如果 bot 现在正在跑，它不会停；去任务管理器结束 pythonw.exe 或重启）")
    return 0


def cmd_daemon(args) -> int:
    print("=" * 52)
    print("后台常驻状态")
    print("=" * 52)

    run_value = _run_key_value()
    if run_value:
        print("  ✓ 注册表启动项：已装")
        print("    " + run_value[:100])
    else:
        print("  · 注册表启动项：没装")

    if STARTUP_VBS.exists():
        print(f"  ✓ 启动文件夹：{STARTUP_VBS.name}")
    else:
        print("  · 启动文件夹：没有")

    code, out = _run_task_cmd(["/Query", "/TN", TASK_NAME])
    print("  " + ("✓ 计划任务：已装" if code == 0 else "· 计划任务：没装（要管理员才能装）"))

    print(f"\n  日志文件：{logbook.log_path()}")
    print(f"  面板地址：http://{args.host}:{args.port}")
    print("\n  正在运行的 pythonw / python 进程：")
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
        )
        lines = [ln for ln in (proc.stdout or "").splitlines() if "pythonw" in ln]
        print("    " + ("\n    ".join(lines) if lines else "（没有 pythonw 在跑）"))
    except OSError:
        pass
    return 0


def cmd_mode(args) -> int:
    """切换半自动/全自动。后台在跑就直接改内存（立刻生效），没跑就写配置文件。"""
    cfg = config_mod.load_config(args.config)
    payload = {}
    if args.auto:
        payload["auto_send"] = True
    if args.semi:
        payload["auto_send"] = False
    if args.dm_auto:
        payload["dm_auto_reply"] = True
    if args.dm_manual:
        payload["dm_auto_reply"] = False
    if not payload:
        st = config_mod.status(cfg)
        print(f"评论区：{'全自动' if st['auto_send'] else '半自动'}")
        print(f"私信：{'自动回' if cfg.get('dm', {}).get('auto_reply') else '手动审'}")
        print("\n用法：mode --auto | --semi | --dm-auto | --dm-manual")
        return 0

    host = args.host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = args.port or cfg.get("server", {}).get("port", 8765)
    body = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/mode",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        print("✓ " + result.get("message", "已切换") + "（后台正在跑，立刻生效）")
        return 0
    except Exception:  # noqa: BLE001  后台没在跑，退回到改配置文件
        pass

    if "auto_send" in payload:
        cfg.setdefault("mode", {})["auto_send"] = bool(payload["auto_send"])
    if "dm_auto_reply" in payload:
        cfg.setdefault("dm", {})["auto_reply"] = bool(payload["dm_auto_reply"])
    config_mod.save_config(cfg, args.config)
    print("✓ 已写入 config.json（后台没在跑，下次启动生效）")
    return 0


def cmd_list(args) -> int:
    cfg, store, client = build(args)
    rows = store.list_drafts(args.status, 50)
    if not rows:
        print("队列是空的")
        return 0
    for row in rows:
        print(f"#{row['id']:<4} [{row['status']:<7}] {row['trigger_name'] or '某人'}："
              f"{(row['trigger_text'] or '')[:34]}")
        print(f"      → {(row['body'] or '(空)')[:50]}")
    print(f"\n统计：{store.stats()}")
    return 0


def cmd_send(args) -> int:
    cfg, store, client = build(args)
    draft = store.get_draft(args.id)
    if not draft:
        print(f"没有 #{args.id} 这条")
        return 1
    ok, message = interact.deliver(store, client, cfg, draft, body=args.body, ignore_gap=True)
    print(("✓ " if ok else "× ") + message)
    return 0 if ok else 1


def cmd_dump(args) -> int:
    cfg, store, client = build(args)
    out_dir = HERE / "debug"
    out_dir.mkdir(exist_ok=True)
    for name, fetcher in (("at", client.at_me), ("reply", client.reply_me)):
        try:
            items, offset, is_end = fetcher()
        except BiliError as exc:
            print(f"× {name}: {exc.code} {exc.message}")
            continue
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ {name}: {len(items)} 条 → {path}（next={offset!r}, end={is_end}）")
        if items:
            first = items[0]
            print(f"  样例字段：kind={first['kind']} uid={first['uid']} oid={first['oid']} "
                  f"root={first['root']} rpid={first['rpid']} bvid={first['bvid']!r}")
            print(f"  原文：{(first['text'] or '')[:60]}")
    return 0


def cmd_demo(args) -> int:
    """塞假数据，让界面先跑起来。用 data/demo.db，不碰真实队列。"""
    cfg = config_mod.load_config(args.config)
    demo_db = HERE / "data" / "demo.db"
    try:
        if demo_db.exists():
            demo_db.unlink()
    except OSError:
        # Windows 上旧进程还占着文件，换一个名字继续
        demo_db = HERE / "data" / f"demo-{os.getpid()}.db"
        print(f"  (demo.db 被占用了，改用 {demo_db.name})")
    store = Store(demo_db)
    client = BiliClient(cfg)

    now = time.time()
    samples = [
        {
            "mkey": "demo-1", "kind": "at", "created": now - 180,
            "trigger_uid": 90001, "trigger_name": "路过的薯条",
            "trigger_text": "@大肥鱼 这鱼是不是有点胖了",
            "context": "", "bvid": "BV1agHN64ErV", "oid": 117494417197778,
            "root": 111, "parent": 111, "title": "220w播放的片子vs他的12个挑战者，收藏的终极对决",
            "uri": "", "body": "哼，这叫储备粮！冬天要靠它过活的，你懂什么。",
            "angle": "模型生成", "status": "pending", "error": "", "sent_at": 0,
            "note": "回复到被 @ 的那条评论下",
        },
        {
            "mkey": "demo-2", "kind": "reply", "created": now - 900,
            "trigger_uid": 90002, "trigger_name": "干饭第一名",
            "trigger_text": "所以最后到底哪个更快啊，视频没说清楚",
            "context": "本鱼看完只关心一件事：谁家的饭先熟",
            "bvid": "BV1w2Hw6KE1j", "oid": 117494417000001,
            "root": 222, "parent": 223, "title": "写完全国所有省份的love me",
            "uri": "", "body": "唔，这个本鱼真没记住，回去又拖了一遍进度条，好像还是左边那台。",
            "angle": "模型生成", "status": "pending", "error": "", "sent_at": 0,
            "note": "在子回复里定位到具体那条",
        },
        {
            "mkey": "demo-3", "kind": "at", "created": now - 30,
            "trigger_uid": 90003, "trigger_name": "摸鱼专员",
            "trigger_text": "@大肥鱼 出来吃饭",
            "context": "", "bvid": "", "oid": 0, "root": 0, "parent": 0,
            "title": "", "uri": "", "body": "", "angle": "未配模型，手动写一句",
            "status": "pending", "error": "", "sent_at": 0,
            "note": "没定位到评论，发出去会变成视频下的新评论",
        },
        {
            "mkey": "demo-4", "kind": "dm", "created": now - 600,
            "trigger_uid": 90004, "trigger_name": "UID 90004",
            "trigger_text": "你这号是机器人吗，怎么回得这么快",
            "context": "对方：在吗\n我：咕噜，在的",
            "bvid": "", "oid": 90004, "root": 0, "parent": 0,
            "title": "私信", "uri": "",
            "body": "唔，被你看出来了。本鱼是脚本在跑，不过回你的这句是真心的。",
            "angle": "模型生成", "status": "pending", "error": "", "sent_at": 0,
            "note": "私信回复",
        },
    ]
    for sample in samples:
        store.add_draft(**sample)

    videos = []
    try:
        videos = client.popular(pn=1, ps=8)
    except BiliError as exc:
        print(f"  (拉不到真实热门，用示例数据：{exc.message})")
    if not videos:
        videos = [
            {"bvid": "BV1agHN64ErV", "title": "示例视频：220w播放的片子", "owner": "某UP",
             "tname": "生活", "duration": 600, "view_count": 2200000,
             "url": "https://www.bilibili.com/video/BV1agHN64ErV", "source": "热门"},
        ]
    scores = [(9, "手工活，看完想动手"), (8, "信息密度高，没废话"), (8, "有梗，适合摸鱼"),
              (7, "选题怪但好玩"), (7, "科普讲得清楚"), (6, "可能合你口味"),
              (6, "下饭"), (5, "看看再说")]
    tag_pool = [
        ["美食", "手工", "硬核"], ["游戏", "整活", "鬼畜"], ["科普", "干货"],
        ["动画", "杂谈"], ["纪录片", "社会"], ["科技", "编程"],
    ]
    for index, video in enumerate(videos[:8]):
        video = dict(video)
        video["score"], video["reason"] = scores[index % len(scores)]
        video["source"] = "演示"
        video["tags"] = tag_pool[index % len(tag_pool)]
        store.add_feed(video)

    # 预置两个口味标签，方便直接看效果
    store.bump_tag("美食", 2)
    store.bump_tag("鬼畜", -1)

    print(f"✓ 演示数据写进 {demo_db}")
    print(f"  4 条待发 / {min(8, len(videos))} 条投喂")

    runtime = Runtime(cfg, store, client, args.config)
    try:
        httpd = make_server(runtime, args.host, args.port)
    except OSError as exc:
        print(f"× 端口 {args.port} 起不来：{exc}（换个端口试试 --port {args.port + 1}）")
        return 1
    url = f"http://{args.host}:{args.port}"
    print(f"\n驾驶舱：{url}   （Ctrl+C 退出）")
    print("注意：demo 模式下「发送」会真去调 B站，没 Cookie 会失败，这是正常的。")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收摊，嗝。")
    return 0


def cmd_serve(args, with_worker: bool) -> int:
    cfg, store, client = build(args)
    st = config_mod.status(cfg)
    runtime = Runtime(cfg, store, client, args.config)
    try:
        httpd = make_server(runtime, args.host, args.port)
    except OSError as exc:
        log(f"× 端口 {args.port} 起不来：{exc}（可能已经有一个 bot 在跑）")
        print(f"× 端口 {args.port} 起不来：{exc}")
        print(f"  可能已经有一个 bot 在跑了，先开 http://{args.host}:{args.port} 看看；")
        print(f"  或者换个端口：--port {args.port + 1}")
        return 1
    url = f"http://{args.host}:{args.port}"

    if with_worker:
        if st["cookie_ready"]:
            Worker(runtime).start()
        else:
            log("× 没配 Cookie，后台循环先不启动（配好再重启就行）")
    if not st["model_ready"]:
        log("· 模型没配：回复只会排队，需要你自己写一句")

    log(f"驾驶舱：{url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\n收摊，嗝。")
    return 0


GLOBAL = argparse.ArgumentParser(add_help=False)
GLOBAL.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（默认 config.json）")
GLOBAL.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 路径（默认 data/feiyu.db）")
GLOBAL.add_argument("--host", default=argparse.SUPPRESS, help="监听地址，默认 127.0.0.1")
GLOBAL.add_argument("--port", type=int, default=argparse.SUPPRESS, help="端口，默认 8765")
GLOBAL.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS,
                    help="别自动开浏览器")


def main() -> int:
    parser = argparse.ArgumentParser(description="大肥鱼小号 bot", parents=[GLOBAL])
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("up", parents=[GLOBAL], help="后台 + 网页一起跑")
    sub.add_parser("serve", parents=[GLOBAL], help="只起网页")
    sub.add_parser("uninstall", parents=[GLOBAL], help="卸掉开机自启")
    sub.add_parser("daemon", parents=[GLOBAL], help="看后台常驻状态")
    sub.add_parser("check", parents=[GLOBAL], help="体检")
    sub.add_parser("poll", parents=[GLOBAL], help="手动扫一遍消息")
    sub.add_parser("dm", parents=[GLOBAL], help="手动扫一遍私信")
    sub.add_parser("discover", parents=[GLOBAL], help="手动跑一轮推荐")
    sub.add_parser("dump", parents=[GLOBAL], help="把原始消息导出到 debug/")
    sub.add_parser("dm-dump", parents=[GLOBAL], help="把原始私信导出到 debug/")
    sub.add_parser("demo", parents=[GLOBAL], help="用假数据预览界面")

    tags_parser = sub.add_parser("tags", parents=[GLOBAL], help="看/清口味权重")
    tags_parser.add_argument("--reset", action="store_true", help="清零所有 tag 权重")
    tags_parser.add_argument("--fix", action="store_true", help="给缺 tag 的视频补抓一次")

    setup_parser = sub.add_parser("setup", parents=[GLOBAL], help="向导：填 Cookie 和模型")
    setup_parser.add_argument("--cookie", default="", help="整条 Cookie 字符串（可省，交互输入）")
    setup_parser.add_argument("--base-url", default="", help="模型接口地址")
    setup_parser.add_argument("--model", default="", help="模型名")
    setup_parser.add_argument("--api-key", default=None, help="模型 API Key")
    setup_parser.add_argument("--no-llm", action="store_true", help="只配 Cookie，不碰模型")

    list_parser = sub.add_parser("list", parents=[GLOBAL], help="看队列")
    list_parser.add_argument("--status", default="pending",
                             choices=["pending", "sent", "skipped", "failed", "all"])

    send_parser = sub.add_parser("send", parents=[GLOBAL], help="发某一条")
    send_parser.add_argument("--id", type=int, required=True)
    send_parser.add_argument("--body", default=None, help="临时替换正文")

    install_parser = sub.add_parser("install", parents=[GLOBAL], help="装成开机自启（不需要管理员）")
    install_parser.add_argument("--task", action="store_true", help="改用计划任务（需要管理员）")

    mode_parser = sub.add_parser("mode", parents=[GLOBAL], help="切半自动/全自动")
    mode_parser.add_argument("--auto", action="store_true", help="评论区改全自动")
    mode_parser.add_argument("--semi", action="store_true", help="评论区改回半自动")
    mode_parser.add_argument("--dm-auto", action="store_true", help="私信改自动回")
    mode_parser.add_argument("--dm-manual", action="store_true", help="私信改回手动审")

    args = parser.parse_args()
    args.config = getattr(args, "config", None)
    args.db = getattr(args, "db", None)
    args.host = getattr(args, "host", None)
    args.port = getattr(args, "port", None)
    args.no_browser = getattr(args, "no_browser", False)

    if not args.cmd:
        parser.print_help()
        return 0

    if args.host is None:
        args.host = config_mod.load_config(args.config).get("server", {}).get("host", "127.0.0.1")
    if args.port is None:
        args.port = int(config_mod.load_config(args.config).get("server", {}).get("port", 8765))
    if args.db is None:
        args.db = HERE / "data" / "feiyu.db"
    if getattr(args, "status", None) == "all":
        args.status = None

    handlers = {
        "setup": cmd_setup,
        "mode": cmd_mode,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "daemon": cmd_daemon,
        "check": cmd_check,
        "poll": cmd_poll,
        "dm": cmd_dm,
        "discover": cmd_discover,
        "dm-dump": cmd_dm_dump,
        "tags": cmd_tags,
        "list": cmd_list,
        "send": cmd_send,
        "dump": cmd_dump,
        "demo": cmd_demo,
    }
    if args.cmd in handlers:
        return handlers[args.cmd](args)
    if args.cmd == "up":
        return cmd_serve(args, with_worker=True)
    if args.cmd == "serve":
        return cmd_serve(args, with_worker=False)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
