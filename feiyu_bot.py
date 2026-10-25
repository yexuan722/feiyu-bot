#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼小号 bot —— 命令行入口。

    python feiyu_bot.py up        起后台（网页 + 自动扫消息/捞视频）
    python feiyu_bot.py serve     只起网页，不自动干活
    python feiyu_bot.py check     体检：Cookie / 模型 / wbi 签名
    python feiyu_bot.py demo      塞点假数据，先看界面长什么样
    python feiyu_bot.py poll      手动扫一遍 @ 和回复
    python feiyu_bot.py discover  手动捞一轮视频
    python feiyu_bot.py list      看看队列里有什么
    python feiyu_bot.py send --id 3 [--body "临时改一句"]
    python feiyu_bot.py dump      把原始消息存到 debug/ 方便对字段
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from bbot import interact, llm, notify  # noqa: E402
from bbot.api import BiliClient, BiliError  # noqa: E402
from bbot.logbook import log  # noqa: E402
from bbot.server import Runtime, Worker, make_server  # noqa: E402
from bbot.store import Store  # noqa: E402


def build(args) -> tuple[dict, Store, BiliClient]:
    cfg = config_mod.load_config(args.config)
    store = Store(args.db)
    client = BiliClient(cfg)
    return cfg, store, client


# --------------------------------------------------------------------- 命令


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
    if demo_db.exists():
        demo_db.unlink()
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
        },
        {
            "mkey": "demo-3", "kind": "at", "created": now - 30,
            "trigger_uid": 90003, "trigger_name": "摸鱼专员",
            "trigger_text": "@大肥鱼 出来吃饭",
            "context": "", "bvid": "", "oid": 0, "root": 0, "parent": 0,
            "title": "", "uri": "", "body": "", "angle": "未配模型，手动写一句",
            "status": "pending", "error": "", "sent_at": 0,
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
    for index, video in enumerate(videos[:8]):
        video = dict(video)
        video["score"], video["reason"] = scores[index % len(scores)]
        video["source"] = "演示"
        store.add_feed(video)

    print(f"✓ 演示数据写进 {demo_db}")
    print(f"  3 条待发 / {min(8, len(videos))} 条投喂")

    runtime = Runtime(cfg, store, client)
    httpd = make_server(runtime, args.host, args.port)
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
    runtime = Runtime(cfg, store, client)
    httpd = make_server(runtime, args.host, args.port)
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
    sub.add_parser("check", parents=[GLOBAL], help="体检")
    sub.add_parser("poll", parents=[GLOBAL], help="手动扫一遍消息")
    sub.add_parser("discover", parents=[GLOBAL], help="手动捞一轮视频")
    sub.add_parser("dump", parents=[GLOBAL], help="把原始消息导出到 debug/")
    sub.add_parser("demo", parents=[GLOBAL], help="用假数据预览界面")

    list_parser = sub.add_parser("list", parents=[GLOBAL], help="看队列")
    list_parser.add_argument("--status", default="pending",
                             choices=["pending", "sent", "skipped", "failed", "all"])

    send_parser = sub.add_parser("send", parents=[GLOBAL], help="发某一条")
    send_parser.add_argument("--id", type=int, required=True)
    send_parser.add_argument("--body", default=None, help="临时替换正文")

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
        "check": cmd_check,
        "poll": cmd_poll,
        "discover": cmd_discover,
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
