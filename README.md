# 大肥鱼小号 bot

把自己的 B站 小号接上 AI：**有人 @ 它，它就接话**；闲着的时候去首页**捞好玩的视频**，送到你手上。

跑在自己电脑上，零第三方服务，Cookie 和 Key 都只在本机。

![驾驶舱](docs/preview.png)

---

## 先说清楚：它能干到哪一步

| 想要 | 现在能做到 | 风险 |
|---|---|---|
| 评论区有人 @ 小号，AI 接话 | ✅ 拉到 @ / 回复 → 生成 → 你点一下发 | 中：自动发评论违反用户协议 |
| 自己不用盯着 | ✅ `auto_send: true` + 限速 | 高：可能被限流、评论被吞、封号 |
| 发现好玩视频 | ✅ 热门 + 推荐流 + AI 打分 | 低：只读接口 |
| 让我在 B站 里收到推荐 | ✅ 本地面板 / 小号私信（beta） | 私信有频率限制 |
| 在别人视频下 @ 你主号 | ❌ 故意不做 | 极高：属于引流，最容易被举报 |

**为什么不做「评论区 @ 你」**：B站 对 `@` 引流抓得很紧，一条被举报就可能小号连坐主号。这个功能本鱼不给你写，别骂我。

**还有两条要说死：**

1. 脚本调 B站 内部接口**违反《哔哩哔哩用户协议》**。B站 真要处理，理由充分，没得辩。
2. 评论区是 AI 生成内容，B站 对 AI 内容有标注要求。评论场景不好逐条标，所以**别让它假装真人经历**，也别拿它去带节奏、刷屏。

默认配置是**最保守**的那一档：只排队，不自动发。

---

## 三分钟跑起来（先不花钱、不登录）

```powershell
cd feiyu-bot
python feiyu_bot.py demo
```

会开一个网页 `http://127.0.0.1:8765`，里面是假数据，你能先看清界面长什么样、按钮怎么点。
`demo` 用的是 `data/demo.db`，不会污染真实队列。

看完按 `Ctrl+C` 收摊。

---

## 最快路径：接入向导

```powershell
python feiyu_bot.py setup
```

它只问两件事，填完当场验：

1. **整条 Cookie**：F12 → Network → 点任意请求 → Request Headers 里的 `Cookie:` 整行复制过来。
   向导会自己拆出 `SESSDATA / bili_jct / buvid3 / DedeUserID`，不用你一个个抄。
2. **模型**：`base_url` / `model` / `api_key`，回车跳过也行（那就只排队、不生成）。
   默认帮你填好 `https://api.deepseek.com/v1` + `deepseek-flash`，并且关掉思考模式。
   注意 **`base_url` 那一栏填的是网址，不是 key**（贴错了向导会提醒并自动挪到 key 那栏）。

不想交互，一条命令塞完也行：

```powershell
python feiyu_bot.py setup `
  --cookie "SESSDATA=xxx; bili_jct=yyy; buvid3=zzz; DedeUserID=123" `
  --model "deepseek-flash" `
  --api-key "sk-xxxx"
```

看到 `✓ 登录成功：你的昵称` + `✓ 模型通了` 就算接好了。

---

## 手动接（想自己填配置）

### 1. 建配置

```powershell
Copy-Item config.example.json config.json
```

### 2. 捞 Cookie

1. 用**小号**登录 B站 网页版（Edge / Chrome 都行，别用无痕，无痕关掉就没了）。
2. `F12` 打开开发者工具 → `Application`（应用）→ 左侧 `Cookies` → `https://www.bilibili.com`。
3. 抄这四个值到 `config.json` 的 `bili` 段：

| 字段 | 在哪 | 说明 |
|---|---|---|
| `sessdata` | Cookie 名 `SESSDATA` | 登录凭证，最敏感 |
| `bili_jct` | Cookie 名 `bili_jct` | CSRF token，发评论必须带 |
| `buvid3` | Cookie 名 `buvid3` | 设备指纹，私信要用 |
| `dedeuserid` | Cookie 名 `DedeUserID` | 小号的 UID |

4. 体检：

```powershell
python feiyu_bot.py check
```

看到 `✓ 已登录：xxx（UID ...）` 就通了。

> ⚠️ `SESSDATA` 等于账号密码。别贴群里、别截图、别传到任何在线工具。
> 丢了赶紧去 B站 网页版「退出登录」让旧凭证失效。

### 3. 填模型（可选，不填也能跑）

任何 OpenAI 兼容接口都行，改 `config.json` 的 `llm` 段：

```json
"llm": {
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-...",
  "model": "deepseek-flash",
  "extra_body": { "thinking": { "type": "disabled" } }
}
```

**`model` 名字是原样传给接口的**，服务端认不认是它的事——写错了直接 400/404，不会自动纠错。不同平台命名规则不一样，别混用。

**常见便宜选项**（价格随时变，以官网为准）：

| 平台 | base_url | model | 大概价钱 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | 输入 1～2 元/百万 token，输出 4～8 元；**夜里和周末半价** |
| DeepSeek 高端 | 同上 | `deepseek-v4-pro` | 贵 4～5 倍，写评论没必要 |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` 等 | 有免费模型 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 有免费档 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` | 免费，费电 |

`extra_body` 是原样塞进请求体的额外参数。DeepSeek 的思考模式**默认是开的**，写 40 字评论用不着它，只会拖慢速度、多烧输出 token，所以示例里关掉了。换别的平台如果报参数错误，把这一行删掉即可。

没配模型时：@ 消息照样进队列，但正文是空的，要你自己写一句；发现视频退化成关键词匹配。

### 4. 起锅

```powershell
python feiyu_bot.py up
```

浏览器会自己打开 `http://127.0.0.1:8765`。

---

## 驾驶舱怎么用

左边 **待发队列**：

- 每张卡是「谁、说了什么、在哪个视频下、你原来那条是什么」
- 正文可以直接改（滚动到字数上限会禁用发送）
- `发送` 真发；`跳过` 进垃圾桶，不再出现
- 发失败了会掉到下面的 **发送失败** 区，红框里是 B站 的原话，点 `重试` 放回队列

右边 **本鱼捞的**：

- 每天固定一批（默认 10 条），按 AI 打分排序，`9 分` 以上是橙色
- 点标题直跳 B站；`没意思` 以后不再出现
- 标题下面是 **tag**：**左键点** = 以后多推这类，**右键点** = 少推这类
- 下方「口味权重」显示当前偏好，可以一键清零

底部 **运行日志**：后台每轮干了什么，出错也在里面。

### 回复位置

草稿卡上会有一行 `📍` 说明这条要发到哪：

| 提示 | 意思 |
|---|---|
| 回复到被 @ 的那条评论下 | 正常，挂在目标评论底下 |
| 在子回复里定位到具体那条 | 挂在楼中楼的具体那层 |
| 没定位到评论，发出去会变成视频下的新评论 | 危险，会变成顶层评论 |

最后那种情况**默认不让发**，会直接报错拦下来。真想把评论发在视频底下，把 `mode.allow_top_level_reply` 改成 `true`。

---

## 私信自动回复

```json
"dm": { "enabled": true, "auto_reply": false }
```

- `enabled`：扫不扫私信（扫了也只是排队，不代表会自动回）
- `auto_reply`：**私信自动回**，和评论区的 `auto_send` 分开控制

规则：

- 只看「最后一句是对方说的」会话，回完就不追
- **跳过系统消息**（互相关注提示、活动通知、自动回复），只接真人发的（`msg_source` 0/1/2/3/4/7）
- 白名单/黑名单用 `allowed_uids` / `blocked_uids`

### 接口实况（2026-10 实测）

| 动作 | 接口 | 状态 |
|---|---|---|
| 会话列表 | `session_svr/v1/session_svr/get_sessions` | ✅ 通了 |
| 消息记录 | `svr_sync/v1/svr_sync/fetch_session_msgs` | ✅ 通了 |
| 发私信 | `web_im/v1/web_im/send_msg` | ✅ 签名通过（见下） |

发私信这个接口**坑最多**：URL 上要带 wbi 签名参数 `w_sender_uid` / `w_receiver_id` / `w_dev_id` / `w_rid` / `wts`，正文里还要再带一份 `msg[...]`，**两边缺一不可**。另外 `dev_id` 就是个 UUID v4，跟 `buvid3` 没关系（之前那版猜错了，已修）。

验证方式：给「自己」发一条，接口返回 `21026 不能给自己发送消息哦~` —— 说明鉴权、签名、参数全对，只是被这一条业务规则拦下。

**发不出去的常见码**：

| code | 含义 |
|---|---|
| `21047` | 对方没关注/没回复过你，最多只能发 1 条 |
| `21020` / `21046` | 发太快了，歇一歇 |
| `25003` | 对方隐私设置不接收 |
| `-400` | 参数不对（多半是签名或 dev_id） |

---

## 两种模式

```json
"mode": { "auto_send": false }
```

| | 半自动（默认） | 全自动 |
|---|---|---|
| 触发 | @我 / 回复我 | 同左，但**不会**主动去别人评论区留言 |
| 发布 | 你点「发送」 | 生成即发 |
| 限速 | 手动点也守 6 条/小时、30 条/天 | 同左，另加 60 秒最小间隔 |
| 适合 | 想控制语气、怕号没了 | 号不重要，纯玩 |

全自动也不会给「敏感词命中」的草稿自动发（`bbot/interact.py` 里的 `RISKY_WORDS`）。

---

## 配置项

| 键 | 默认 | 作用 |
|---|---|---|
| `mode.auto_send` | `false` | 全自动开关 |
| `mode.poll_seconds` | `180` | 多久扫一次 @ 和回复（最低 30 秒） |
| `mode.allow_top_level_reply` | `false` | 定位不到评论时，允不允许退回「发在视频下」 |
| `mode.first_run_window_minutes` | `120` | 第一次跑只处理这么久以内的 @，避免把陈年消息全回一遍 |
| `limits.max_per_hour` / `max_per_day` | 6 / 30 | 发送上限，**建议别改大** |
| `limits.min_gap_seconds` | 60 | 自动发送的最小间隔 |
| `limits.max_reply_chars` | 80 | 正文长度上限 |
| `dm.enabled` | `true` | 扫不扫私信 |
| `dm.auto_reply` | `false` | 私信自动回（独立开关） |
| `dm.poll_seconds` | `300` | 多久扫一次私信 |
| `dm.max_per_hour` / `min_gap_seconds` | 5 / 30 | 私信限速 |
| `dm.allowed_uids` / `blocked_uids` | `[]` | 私信白名单/黑名单 |
| `discover.mode` | `daily` | `daily` 每天一批，`interval` 按分钟轮询 |
| `discover.daily_at` | `09:00` | 每天几点跑（本地时间） |
| `discover.daily_count` | `10` | 每天固定挑几条 |
| `discover.interval_minutes` | `180` | `interval` 模式的间隔 |
| `discover.min_score` | 6 | 低于这个分数不投喂 |
| `discover.block_keywords` | 广告/抽奖/… | 标题命中就跳过 |
| `discover.tag_weight_scale` | `0.5` | tag 权重换算成分数的系数 |
| `discover.tag_bonus_max` | `1.5` | 单个 tag 加分**封顶**，防止变成回音壁 |
| `discover.randomness` | `1.2` | 随机抖动 ±，保底留一点新鲜感 |
| `interests` | 见示例 | 喂给模型的口味描述，越具体越准 |
| `notify.webhook_enabled` + `webhook_url` + `webhook_type` | 关 | 推送到手机 |
| `notify.dm_enabled` + `dm_to_uid` | 关 | 小号私信**推送推荐**给你（和上面的自动回复是两码事） |
| `server.port` | 8765 | 换个端口防冲突 |

环境变量也能覆盖：`FEIYU_SESSDATA` / `FEIYU_BILI_JCT` / `FEIYU_BUID3` / `FEIYU_UID` / `FEIYU_API_KEY` / `FEIYU_BASE_URL` / `FEIYU_MODEL` / `FEIYU_AUTO_SEND` / `FEIYU_PORT`。临时测试可以这样，长期还是写 `config.json`。

---

## 投喂：两种通道

### webhook（推荐）

```json
"notify": {
  "webhook_enabled": true,
  "webhook_url": "https://api.telegram.org/bot<token>",
  "webhook_type": "telegram",
  "chat_id": "你的 chat id"
}
```

`webhook_type` 支持：

| 值 | 说明 |
|---|---|
| `json_text` | 通用：POST `{"title","text","url"}`，自己接 n8n/自建服务用 |
| `telegram` | `webhook_url` 填到 `/bot<token>`，`chat_id` 填你的 |
| `bark` | `webhook_url` 填 `https://api.day.app/<你的key>` |
| `dingtalk` / `feishu` | 群机器人 webhook，发纯文本 |

### 私信推送

```json
"notify": { "dm_enabled": true, "dm_to_uid": "你主号的UID" }
```

这是「把推荐私信发给你」，和上面的「**私信自动回复**」是两码事，两个开关互不影响。
用的是同一个发私信接口，签名已经调通。注意 `21047` 规则：对方没关注你、也没回复过你之前，**只能发 1 条**——所以 `dm_to_uid` 填和你互关的号才靠谱。

---

## 常见故障

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `check` 里登录失败 | SESSDATA 过期 / 复制少了尾部 `%2C` 后面的部分 | 重新登录再抄一遍 |
| 评论发了但别人看不见、只有自己能看到 | 进审核 / 被风控吞了 | 停一天别发，别连续试 |
| `[-352]` 风控校验失败 | 触发风控 | 停手，换网络环境，别硬刚 |
| `[-101]` 未登录 | Cookie 没带对 | 重抄 `SESSDATA` + `bili_jct` |
| 推荐流拉取失败 | wbi 签名或未登录 | 热门还能用，不影响主流程 |
| 模型报 401 | key 错或没额度 | 换个 key / 换模型 |
| 拉不到 @ 消息 | 字段变了 | `python feiyu_bot.py dump`，看 `debug/at.json` 里字段长什么样 |
| 回复跑到视频底下 | 目标 rpid 没取到，`parent=0` | 已修：rpid 现在从 `source_id` 取；实在没有会拦下来不让发 |
| 私信字段对不上 | 接口改版 | `python feiyu_bot.py dm-dump` 看原始返回 |

`dump` / `dm-dump` 是排障神器：把原始 JSON 落盘，字段改名了一眼就能看出来。

---

## 常用命令

```powershell
python feiyu_bot.py up          # 起锅（网页 + 后台）
python feiyu_bot.py check       # 体检
python feiyu_bot.py poll        # 手动扫 @ / 回复
python feiyu_bot.py dm          # 手动扫私信
python feiyu_bot.py discover    # 手动跑一轮推荐
python feiyu_bot.py list        # 看队列
python feiyu_bot.py tags        # 看口味权重（--reset 清零）
python feiyu_bot.py send --id 3 # 发某条
python feiyu_bot.py dm-dump     # 导出私信原始数据
```

---

## 文件结构

```
feiyu-bot/
├── feiyu_bot.py          # 命令行入口
├── config.example.json   # 配置模板
├── run.ps1               # 双击就跑 up
├── bbot/
│   ├── api.py            # B站 接口（含发评论、私信）
│   ├── wbi.py            # WBI 签名
│   ├── interact.py       # @ 消息 → 回复（含回复位置定位）
│   ├── dm.py             # 私信自动回复
│   ├── discover.py       # 每日推荐、打分、tag 加权
│   ├── notify.py         # webhook / 私信
│   ├── store.py          # SQLite 队列与去重
│   ├── server.py         # 本地 HTTP 服务
│   ├── ui.html           # 驾驶舱页面
│   ├── config.py         # 配置加载
│   ├── llm.py            # 模型调用
│   ├── logbook.py        # 日志
│   └── persona.md        # 人设（改语气改这里，别动代码）
└── data/feiyu.db         # 队列、已投喂、去重记录（删了等于失忆）
```

---

## 本鱼的建议

1. **先跑一周半自动**，看看它写的回复像不像人，再考虑开全自动。
2. **别把 `max_per_hour` 调到 10 以上**。真人不会一小时回十条 @。
3. **`persona.md` 比提示词工程管用**。觉得语气飘了，改文件里的规矩，不用碰代码。
4. **准备一个备用小号**。真被封了不至于心态爆炸。
5. 这套东西的价值在「发现 + 措辞」，不在「自动发」。自动发那一步，收益很小，风险很大。
