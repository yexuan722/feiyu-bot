# -*- coding: utf-8 -*-
"""看门狗：每 N 秒确认 bot 还活着，断了就自己拉起来。

它比 bot 更底层：开机自启指向的是它，它再负责 bot。
所以 bot 崩了、被杀、端口被占没起来，都会被重新拉起。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "feiyu_bot.py"
DATA = ROOT / "data"
LOG_DIR = DATA / "logs"
PID_FILE = DATA / "watchdog.pid"
STOP_FLAG = DATA / "stop.flag"

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"watchdog-{time.strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _pythonw() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def _pid_alive(pid: int) -> bool:
    """只在 Windows 上安全地查「这个 PID 还活着吗」。

    ⚠️ 千万别用 os.kill(pid, 0)：在 Windows 上它等价于 TerminateProcess，
    是去杀进程，不是探活。
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def _take_lock() -> bool:
    """同一个看门狗只允许跑一个，不然两只狗会抢着重启。"""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            other = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            log(f"已经有看门狗在跑（PID {other}），本进程退出")
            return False
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def bot_pids() -> list[int]:
    """找「跑着 feiyu_bot.py up」的 pythonw 进程，别把看门狗自己算进去。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        out = []
        for item in data:
            line = str(item.get("CommandLine") or "")
            if str(BOT) in line and "watchdog" not in line:
                out.append(int(item.get("ProcessId") or 0))
        return [pid for pid in out if pid]
    except Exception:  # noqa: BLE001
        return []


def api_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=6) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def start_bot() -> bool:
    try:
        subprocess.Popen(
            [str(_pythonw()), str(BOT), "up", "--no-browser"],
            cwd=str(ROOT),
            creationflags=DETACHED,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as exc:
        log(f"启动失败：{exc}")
        return False


def check_once(port: int) -> str:
    if STOP_FLAG.exists():
        return "paused"
    if api_ok(port):
        return "ok"
    pids = bot_pids()
    if pids:
        return f"hung:{pids[0]}"
    if port_open(port):
        # 端口有人占着但接口不通，多半是别的程序，别乱杀
        return "port_busy"
    log("bot 不在了，重新拉起")
    return "started" if start_bot() else "start_failed"


def run(interval: int = 180) -> int:
    sys.path.insert(0, str(ROOT))
    from bbot import config as cfg_mod

    cfg = cfg_mod.load_config()
    port = int(cfg.get("server", {}).get("port", 8765))

    if not _take_lock():
        return 0
    log(f"看门狗上线：每 {interval} 秒检查一次，端口 {port}")
    try:
        while True:
            result = check_once(port)
            if result == "started":
                log("已重新拉起，等 20 秒再看")
                time.sleep(20)
                log("复查：" + ("正常" if api_ok(port) else "还是没起来"))
            elif result.startswith("hung:"):
                log(f"进程 {result.split(':')[1]} 在跑但接口不通，等下一轮")
            elif result == "port_busy":
                log(f"端口 {port} 被别的程序占着，不动手")
            elif result == "start_failed":
                log("拉起失败，等下一轮")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("看门狗收到 Ctrl+C，退出")
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
    return 0
