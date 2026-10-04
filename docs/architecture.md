# 架构

## 一句话

单进程 Python，三条线程（HTTP / 采集 / 巡检），一个 SQLite 文件，前端是一个没有构建步骤的内嵌单页。

零第三方依赖是硬约束：目标机器上只需要一个 `python3`。

## 模块

| 模块 | 职责 |
| --- | --- |
| `cpapanel/config.py` | 配置默认值、加载与覆盖（`panel.config.json` + `CPAPANEL_*` 环境变量） |
| `cpapanel/cpa.py` | **上游契约的唯一实现**：路径表（v0/v8）、前缀探测、鉴权、错误映射、所有上游调用 |
| `cpapanel/models.py` | 数据归一化与**凭证状态判定**（六态 + 证据强度） |
| `cpapanel/store.py` | SQLite 数据访问层（表结构、迁移、查询） |
| `cpapanel/collector.py` | 用量采集线程：拉队列 → 落库 → 聚合 → 缺口检测 |
| `cpapanel/inspector.py` | 巡检线程：快照 → 判定 → 计划 → **熔断闸门** → 执行 |
| `cpapanel/notifier.py` | 告警通道（Webhook / Telegram），带事件白名单与去抖 |
| `cpapanel/pricing.py` | 内置价格表 + 覆盖合并，费用估算 |
| `cpapanel/security.py` | 口令哈希、令牌生成与校验 |
| `cpapanel/runtime.py` | 组装（`build()`）与生命周期 |
| `cpapanel/__main__.py` | CLI 入口（10 个子命令） |
| `cpapanel/web/server.py` | HTTP 路由、鉴权、67 个面板端点、静态资源 |
| `cpapanel/web/static/` | 内嵌单页 UI（`index.html` / `app.js` / `style.css`，无外链） |
| `cpapanel/util.py`、`log.py`、`__init__.py` | 工具、日志、版本 |

依赖方向是单向的：`web` → `runtime` → (`collector`/`inspector`) → (`cpa`/`models`/`store`)。

## 三条数据流

### 1. 采集（`collector.py`，默认每 15 秒、每轮取 50 条）

```
GET /observability/usage/queue?count=N
  → normalize_usage_record()   统一字段别名、拍平嵌套、判定错误、生成幂等键
  → insert_usage_event()       幂等键冲突则跳过（重复采集不会重复计数）
  → usage_daily                按天聚合（面板的趋势图读它，不扫明细）
  → 缺口检测                    两次成功采集间隔 > 阈值 → collector.gap 告警
```

**为什么必须秒级采**：上游队列是 `PopOldest` 语义 —— **取出即删，且约 60 秒后静默丢弃**。
所以采集要么在跑，要么数据永久丢失（[upstream-api.md](upstream-api.md) 有完整说明）。

### 2. 巡检（`inspector.py`，默认每 900 秒）

```
GET /credentials（分页取全量）
  → sync_credentials()     新增 / 消失 / 状态变化 → credential_events
  → classify_credential()  六态 + evidence_level（strong/weak）
  → credential_samples     每次巡检都留一条观测，用于回溯「这个号什么时候开始坏的」
  → _plan_for()            按状态与配置生成动作（disable/enable/delete/standby/promote/mark）
  → _plan_pool()           备用池水位：目标数不足时从备用池补位
  → 熔断闸门                 就绪率过低 → 本轮一项都不执行，只留计划
  → 执行（仅 apply）         逐个动作 → actions 表 + 审计
  → notifications           unauthorized / low_pool / circuit_open
```

**dry-run 与 apply 走的是同一条代码路径**，唯一区别是 `allow_execute` 是否为真 ——
这样「你看到的计划」和「真正执行的东西」不可能不一致。

### 3. 面板请求（`web/server.py`）

```
ThreadingHTTPServer
  → @route 匹配（方法 + 正则）
  → 鉴权：会话 Cookie（浏览器）或 Authorization: Bearer <面板令牌>（脚本）
  → 写请求校验 X-CPA-Panel: 1        ← 简单而有效的 CSRF 防护
  → handler → store / cpa client
```

## 凭证状态机

```
                  ┌──────────────┐
                  │  disabled    │←── 人工禁用（最高优先级，不再做其他判定）
                  └──────────────┘
    判定顺序 ↓
   ┌────────────────┐   字段级/强文案    ┌────────────────┐
   │  unauthorized  │←─────────────────│                │
   └────────────────┘                  │                │
   ┌────────────────┐   quota.signals  │ classify_      │
   │ quota_exhausted│←─────────────────│ credential()   │
   └────────────────┘                  │                │
   ┌────────────────┐   unavailable +  │                │
   │   cooling      │←── 未来时间点 ────│                │
   └────────────────┘                  │                │
   ┌────────────────┐   status=error   │                │
   │    unknown     │←── 且原因未知 ────│                │
   └────────────────┘                  └────────────────┘
   ┌────────────────┐
   │    healthy     │←── 以上都不命中
   └────────────────┘
```

判定结果附带 **`evidence_level`**：

- `strong`：字段级证据 —— `status` 字段、结构化 `quota.signals`、订阅到期时间戳
- `weak`：只有 `status_message` 里的关键词

巡检只对 `strong` 的号执行不可逆动作；`weak` 的只写 `mark` 动作 + 告警。
另外，带瞬态词的文案（`timeout` / `connection` / `429` / `rate limit` / `502`…）
永远不会被判成 `unauthorized` 或 `quota_exhausted`，而是归入 `cooling` 等它自愈。

## 熔断

```
ready = 总数 - unauthorized - quota_exhausted      ← 冷却与已禁用不算「坏」
ready_ratio = ready / 总数
ready_ratio < inspector.min_ready_ratio（默认 0.5）  →  本轮放弃全部维护动作
```

它的目标不是「保护上游」，而是**保护你自己**：池子集体掉线时，
自动巡检会把「集体失效」理解成「这些号都该删」，一轮就能清掉半个池子 ——
而其中很多号只是被上游风控临时挡了，过几天就恢复。

熔断触发时会写 `inspection.circuit_open` 审计与通知，巡检结果里 `mode = "circuit_break"`，CLI 输出的计划全部标记为「计划」而非「执行」。

## 数据库

`data/panel.db`（SQLite）。连接：`check_same_thread=False` + `timeout=15`，
`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`，所有写操作由一把 `RLock` 串行化 ——
三条线程共用一个连接，简单但不会因为并发写而炸。

| 表 | 用途 |
| --- | --- |
| `users` / `sessions` / `panel_tokens` | 面板自身的账号、会话、API 令牌 |
| `nodes` | 被管理的 CPA 节点（地址、管理密钥、前缀、启用状态） |
| `credentials` | 凭证当前视图（含 `standby` 本地标记、`deleted` 软删标记） |
| `credential_events` | 新增 / 消失 / 状态变化事件 |
| `credential_samples` | 每次巡检的状态采样（回溯用） |
| `usage_events` | 用量明细（幂等键去重；`prune` 默认保留 180 天） |
| `usage_daily` | 按天聚合（趋势图数据源） |
| `api_keys` / `key_usage` | 下游 Key 与按 Key 用量 |
| `inspections` / `actions` | 巡检轮次与每次计划动作的结果 |
| `audit` | 审计日志（所有写操作、危险操作、熔断） |
| `settings` / `meta` | 运行时设置与元信息 |

## 几个刻意的取舍

| 决定 | 理由 |
| --- | --- |
| 不订阅上游的订阅通道 | 一旦有订阅者，上游就不再往队列里放记录 —— 面板会和用户其它工具抢数据 |
| 单页 UI 无构建步骤 | 部署只需要 Python；改一行 JS 直接生效，不需要 node/npm |
| 前端不用外链 CDN | 面板常在无外网的内网环境跑 |
| 上游契约集中在一个文件 | 上游有两代前缀、多种请求体形状（裸数组/裸布尔），集中起来才可能配对 |
| 判定与执行分离（计划 → 执行） | 让「机器判断」可被人审阅、可复现、可审计 |
