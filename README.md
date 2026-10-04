# cpa-panel

**CLIProxyAPI（CPA）自托管账号管理面板** —— 把「号池 + 用量 + 钱」这三件运维最痛的事，收进一个页面里。

零第三方依赖（只用 Python 标准库）、单进程、内嵌 Web UI、SQLite 持久化。
装完就是一个 `http://127.0.0.1:18317` 的面板。

```
┌────────────────────────────┐        ┌──────────────────────────┐
│  cpa-panel（本项目）        │        │  CLIProxyAPI（CPA 核心）  │
│                            │        │                          │
│  采集线程 ── 每 15s pop ───┼───────▶│  /v8/management/…        │
│  巡检线程 ── 每 15min ─────┼───────▶│  observability/usage/    │
│  Web UI   ◀── 你的浏览器 ──┤        │  queue  ← 只保留 60 秒！  │
│  SQLite   ◀── 永久保存 ────┤        │  credentials / oauth / … │
└────────────────────────────┘        └──────────────────────────┘
```

---

## 为什么需要它

CLIProxyAPI 本身把「多个 AI 账号压成一个 OpenAI 兼容 API」这件事做得很好，但它的管理面是**给机器看的**：

- 用量队列是**消费型（pop）且只保留 60 秒**——你不去取，数据就永远没了；
- 凭证列表只是一个当前快照——**没有历史**，你看不出「这个号是刚才坏的还是三天前坏的」；
- 没有「哪些号该扔、池子还够不够」的运维视图。

cpa-panel 就是补上这一层：**持续采集 → 永久落库 → 可操作的状态收敛 → 自动化运维**。

---

## ⚠️ 三条必须先知道的硬事实

这三点决定了本项目的核心设计，也解释了为什么「随手写一个面板」通常会在生产里丢数据。它们全部来自上游源码（见 [`docs/upstream-api.md`](docs/upstream-api.md)）。

### 1. 用量队列是消费型的，而且只活 60 秒

```go
// internal/api/handlers/management/usage.go
count, errCount := parseUsageQueueCount(c.Query("count"))  // 缺省 1，非正整数 → 400
items := redisqueue.PopOldest(count)                       // ★ 取出即删除
```

`internal/redisqueue/queue.go` 里 `defaultRetentionSeconds = 60`。
**超过 60 秒没被取走的记录会被自动淘汰。**

→ 所以本面板：采集间隔默认 **15 秒**（远小于 60），每批取 **50 条**，每条**立刻落库**；
并且会检测「相邻两次成功采集的间隔」——一旦超过保留窗口，就判定**期间数据已丢失**，
写审计 + 发告警。**这是上游的一个静默陷阱，很多人第一次跑就丢了一整天的数据却不知道。**

### 2. 管理 API 有 v0 / v8 两套前缀，路径不一样

| 业务 | v0 | v8 |
| --- | --- | --- |
| 凭证列表 | `/auth-files` | `/credentials` |
| 用量队列 | `/usage-queue` | `/observability/usage/queue` |
| 下游 Key | `/api-keys` | `/config/access/api-keys` |
| OAuth | `/{provider}-auth-url` | `/oauth/auth-url?provider=` |

→ 本面板用一张映射表同时支持两套，并会**自动探测**上游支持哪一套（`api_prefix: auto`）。

### 3. 管理路径返回 404 ≠ 路径写错了

上游在**没有配置管理密钥**时，根本不注册管理路由 → 所有 `/vN/management/*` 返回 **404**（不是 401）。

→ 本面板把这种情况单独识别为「上游未启用 Management API」并给出人话提示，
而不是甩一个 "not found" 让你去怀疑路径。

---

## 功能

### Web UI（9 个页面，纯手写，无 CDN / 无框架 / 无构建）

| 页面 | 内容 |
| --- | --- |
| 总览 | 告警条、凭证状态卡（总/健康/冷却/禁用/备用池/异常）、近 14 天用量柱状图（手写 SVG）、Top 模型、最近巡检与动作 |
| 账号 | 筛选（节点/提供方/状态/关键词/备用池）+ 表格 + 状态徽章（含原因）+ 详情抽屉（巡检采样时间线、可用模型、原始字段）+ 导入/下载凭证 |
| 配额 | 每个凭证的 `quota.signals` 展开成指标表、冷却到期、订阅到期 |
| 用量 | 1/7/14/30 天切换、请求数/错误率/token/成本汇总卡、按天柱状图、Top 模型、Top 凭证 |
| 请求明细 | 多维筛选 + 分页 + 点开看原始 JSON |
| 下游 Key | 列表（掩码 + 用量）+ 新增/删除 + 从上游同步 |
| 配置 | 面板设置（采集/巡检/通知）、节点增删改+连通性测试、API 令牌、上游 config/YAML 查看与替换、数据维护 |
| 日志 | 面板日志（自动增量轮询）/ 上游应用日志 / 上游错误日志 |
| 设置 | 改口令、通知测试、关于与安全提示 |

### 后端能力

- **凭证状态机**：把上游原始信号（`status` / `disabled` / `unavailable` / `next_retry_after` / `quota.signals` / `id_token` 有效期）
  收敛为 6 个**可操作**状态：`healthy` `cooling` `quota_exhausted` `unauthorized` `disabled` `unknown`，并保留判定依据。
- **快照差异**：每次同步都会产出「新增 / 消失 / 状态变化」事件——把快照变成有时间轴的事件流。
- **巡检与自动化**：拉取全量凭证 → 分类 → 生成计划 → 执行（禁用 / 删除 / 移入备用池 / 从备用池补位）→ 审计。
- **安全护栏**：默认 `dry_run`；删除动作三重开关（开关 + 单轮上限 + 跳过内存凭证）；所有动作留痕。
- **用量落库**：幂等去重（优先用记录自带 request id，否则原始 JSON 的 SHA-1）+ 按天增量聚合 + 成本估算。
- **告警**：Webhook / Telegram；同事件去抖；采集失败、数据丢失、失效账号、池子低水位都会推。
- **可观测**：`/metrics` 暴露 Prometheus 指标。
- **审计**：登录、配置修改、凭证操作、上游配置替换全部留痕。

---

## 快速开始

要求：**Python 3.9+**（无需 pip install 任何东西）。

```bash
git clone https://github.com/7452323/cpa-panel.git
cd cpa-panel

# 1) 初始化：生成配置 + 建管理员 + 写入第一个 CPA 节点
python3 -m cpapanel init \
  --node http://127.0.0.1:8317 \
  --node-key sk-你的管理密钥

# 2) 启动
python3 -m cpapanel serve --host 127.0.0.1 --port 18317

# 3) 打开 http://127.0.0.1:18317 ，用上一步打印的账号登录
```

> 如果 `python3 -m cpapanel` 在你的环境里不可用（少数内嵌解释器不处理 `-m`），
> 用等价启动器：`python3 bin/cpapanel <命令>`。

**先别急着开自动化**：巡检默认 `dry_run=true`，它会告诉你「打算做什么」但不真的动手。
看几轮计划确认无误后，再到「配置 → 巡检」里关掉 dry-run。

### Docker

```bash
docker compose up -d          # 见 docker-compose.yml；数据落在 ./data
```

---

## 目录结构

```
cpa-panel/
├── cpapanel/
│   ├── __main__.py        CLI（serve / init / collect / inspect / password / token / prune）
│   ├── config.py          配置：默认值 → 文件 → 环境变量，逐层覆盖
│   ├── store.py           SQLite 存储层（表结构 + 全部 DAO + 迁移位）
│   ├── cpa.py             CLIProxyAPI 管理 API 客户端（v0/v8 双前缀 + 错误语义）
│   ├── models.py          归一化与状态机（别名表 + 原始留存 + 质量报告）
│   ├── collector.py       用量采集线程（pop → 落库 → 间隙检测）
│   ├── inspector.py       巡检与自动化（计划/执行/备用池/护栏）
│   ├── pricing.py         成本估算（内置价目 + 可覆盖）
│   ├── notify.py          Webhook / Telegram
│   ├── security.py        pbkdf2 口令 + 令牌 + 常量时间比较
│   ├── runtime.py         装配（CLI 与测试共用同一份装配逻辑）
│   └── web/
│       ├── server.py      内置 HTTP 服务（~50 个端点 + 鉴权 + CSRF + 指标）
│       └── static/        前端（index.html / style.css / app.js，零依赖）
├── tests/
│   ├── mock_cpa.py        ★ 复刻上游语义的 Mock CPA（含 pop 即删除、60s TTL、404 语义）
│   ├── test_models.py     归一化 / 状态机 / 计价
│   ├── test_store.py      快照差异 / 幂等 / 聚合 / 清理
│   ├── test_cpa_client.py 前缀探测 / 鉴权 / 错误映射 / 消费型队列
│   ├── test_e2e.py        端到端：Mock CPA ← 后端 ← HTTP API ← 客户端
│   ├── smoke_cli.py       CLI 全流程演练（init → collect → inspect → apply）
│   └── run_all.py         测试运行器（顺带处理两个环境坑，见下）
├── bin/cpapanel           不依赖 `-m` 的启动器
└── docs/
    ├── upstream-api.md    上游契约（从源码抄录，附来源 commit）
    ├── web-api.md         本面板 HTTP API 参考
    └── architecture.md    架构与数据模型
```

---

## 配置要点

配置文件默认 `panel.config.json`（0600 权限）。完整默认值见 [`cpapanel/config.py`](cpapanel/config.py)。

环境变量可覆盖（`CPAPANEL_` 前缀），容器部署时推荐用环境变量传密钥：

```bash
export CPAPANEL_ADMIN_PASSWORD='...'     # 仅 init 时用于创建管理员，不落盘明文
export CPAPANEL_NODE_URL='http://127.0.0.1:8317'
export CPAPANEL_NODE_KEY='sk-...'
export CPAPANEL_HOST=0.0.0.0 CPAPANEL_PORT=18317
```

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `collector.interval_seconds` | `15` | **必须远小于 60**，否则丢数据 |
| `collector.batch_size` | `50` | 每次最多 pop 多少条 |
| `collector.queue_retention_seconds` | `60` | 与上游保留窗口对齐，用于间隙告警判定 |
| `collector.gap_warn_ratio` | `3.0` | 间隔 > 60×3 秒即判定丢数据并告警 |
| `inspector.dry_run` | `true` | **默认只出计划不动手** |
| `inspector.delete_unauthorized` | `false` | 危险动作，默认关 |
| `inspector.max_deletes_per_run` | `20` | 单轮删除上限（防脚本失控） |
| `inspector.standby_pool` | `true` | 失效号移入本地备用池（上游=禁用） |
| `inspector.target_active` | `0` | >0 时低于目标会从备用池补位并告警 |
| `notify.events` | 见文件 | 订阅哪些事件推送 |

---

## 安全模型

诚实地说明边界，而不是给虚假的安全感：

**做了**
- 口令：`pbkdf2_sha256`（20 万轮 + 16 字节随机盐），比较用常量时间。
- 会话 / API 令牌：数据库只存 SHA-256 摘要，明文只显示一次。
- 写操作要求 `X-CPA-Panel: 1` 头（同源前端会带），挡 CSRF；Cookie 为 `HttpOnly` + `SameSite=Lax`。
- 上游管理密钥**永不出现在任何 API 响应里**（只回掩码 + `has_key`）；日志与审计同样脱敏。
- 默认只监听 `127.0.0.1`。
- 配置文件 0600。

**没做 / 需要注意**
- 配置里的密钥是**明文存储**的。标准库没有可用的 AEAD，做「自制加密」只会制造虚假安全感——
  真正的保护来自文件权限 + 响应脱敏 + 不暴露到公网。**不要把面板直接暴露在公网**；要远程访问请走反向代理 + TLS + 额外认证。
- `/metrics` 是公开端点（Prometheus 抓取方便），它只暴露计数与汇总，不含密钥。
- 巡检的删除动作**真的会删上游凭证文件**。默认关闭，开启后请先跑几轮 dry-run。

---

## CLI

```bash
python3 -m cpapanel serve    [--host 0.0.0.0] [--port 18317] [--no-collector] [--no-inspector]
python3 -m cpapanel init     [--node URL --node-key KEY] [--password ...] [--force]
python3 -m cpapanel collect  [--node-id N]        # 失败返回非 0，可放进 cron/监控
python3 -m cpapanel inspect  [--apply] [--json]   # 默认 dry-run
python3 -m cpapanel password [--username admin]
python3 -m cpapanel token    [--name ci] [--role viewer]
python3 -m cpapanel pricing  [--out pricing.json]
python3 -m cpapanel prune    [--usage-days 180] [--sample-days 30] [--audit-days 90]
python3 -m cpapanel version
```

面板自身的 HTTP API 参考见 [`docs/web-api.md`](docs/web-api.md)（~50 个端点，含 `curl` 示例）。

---

## 验证：本项目怎么证明它是真的

「功能完整、逻辑真实」不靠嘴说，靠测试。全部可复现：

```bash
python3 tests/run_all.py     # 122 个用例（含端到端）
python3 tests/smoke_cli.py   # 33 项 CLI 全流程检查
```

当前状态：**122/122 通过**、**CLI 冒烟 33/33 通过**。

关键点在于 [`tests/mock_cpa.py`](tests/mock_cpa.py) —— 它不是「好说话」的假服务，
而是**刻意复刻上游那几个容易踩坑的行为**：

- 管理密钥不对 → **401**；上游没配密钥 → **404**（而不是 401）；
- 可配置只开 v0 / 只开 v8，用于验证自动探测；
- 用量队列 `count` 缺省 1、**非正整数返回 400**、**读取即删除**、**超过 TTL 自动丢弃**；
- 凭证列表无分页时返回 `{observed_at, files}`，带分页时追加 `total/has_page`；
- PATCH/DELETE/上传都真实改变状态。

端到端测试跑的是完整链路：`Mock CPA ← CPAClient ← 面板后端 ← 面板 HTTP API ← HTTP 客户端`，
断言包括「队列被消费后为空」「重复推送被去重」「超过 60s 的记录取不回来」「dry-run 不改上游 / apply 才改」。

### 测试替我们抓到的真 bug（修复前后都留在这份历史里）

| # | 症状 | 根因 |
| --- | --- | --- |
| 1 | `/api/health` 返回 500 | 公开端点的路由匹配结果没解包 |
| 2 | 所有上游请求炸 `TypeError: 'tuple' object cannot be interpreted as an integer` | `urllib` 不接受 `(connect, read)` 元组超时 |
| 3 | 用量汇总请求数偏低 | `usage_summary` 用了 `COUNT(*)`（桶数量）而不是 `SUM(requests)` |
| 4 | 打开账号页 500 | 额度判定的 evidence 里取了不存在的 `status_message` 键 |
| 5 | 延迟显示成 2ms 而不是 2500ms | `duration: 2.5`（秒）被当整数截断 |
| 6 | `init` 后引导节点报 `UnboundLocalError` | 变量作用域写错 |
| 7 | 采集失败却返回退出码 0 | CLI 没把节点失败映射成非 0（对 cron 是致命的） |

### 两个环境坑（已在测试运行器里处理）

如果你在受限环境（内嵌/常驻解释器、无管道、粗粒度 mtime 的文件系统）跑测试，可能会遇到：

1. **`sys.modules` 跨次保留** —— 改了代码但跑的还是旧逻辑，而且 traceback 的行号与源码对不上。
   `run_all.py` / `smoke_cli.py` 因此会先清 `cpapanel*` / `tests*` 的模块缓存。
2. **陈旧的 `__pycache__`** —— 同时清掉。

---

## 已知局限（不打算含糊过去）

- **用量记录的字段没有权威 schema**。上游把队列记录当不透明 JSON 转发，不同版本/提供方字段名可能不同。
  本项目用**别名表**做尽力归一化（`prompt_tokens|input_tokens`、`duration|latency_ms`…），
  并且**无论如何都把原始 JSON 落库**（`usage_events.raw_json`），同时输出「未识别字段」的质量报告。
  预期之外的字段名**不会被静默丢弃**，只会解析不到——这是有意的取舍。
- **成本是估算**。上游不提供价格，内置价目表只是公开价近似值；请用 `pricing.json` 覆盖成你的账单口径。
- **「备用池」是面板的本地概念**。上游没有这个状态，所以「移入备用池」在上游的真实动作是**禁用**，
  面板另外打本地标记，以便低水位时按顺序补位。
- **巡检的额度判定依赖上游被动观测**（`quota.signals`），不是实时的配额查询；能否主动查取决于上游是否支持 `quota/fetch`。
- **没有多用户/权限体系**：一个管理员 + 若干只读/管理 API 令牌。
- 前端要求与 API 同源访问（会话 Cookie + CSRF）；直接用 `file://` 打开 `index.html` 无法登录。

---

## 与上游的兼容性

| 上游能力 | 本项目 |
| --- | --- |
| `/v0/management/*`（旧） | ✅ 完整支持 |
| `/v8/management/*`（新） | ✅ 完整支持，自动探测优先 v8 |
| 管理密钥（`Bearer` / `X-Management-Key`） | ✅ 两个都发 |
| 用量队列（消费型 + 60s TTL） | ✅ 15s 轮询 + 幂等落库 + 间隙告警 |
| 凭证 CRUD / 启停 / 字段 / 刷新 / 下载 / 上传 | ✅ |
| 凭证可用模型 | ✅ |
| OAuth 登录（v8 统一 + v0 按 provider） | ✅ 发起 / 轮询状态 / 取消 |
| 下游 API Key（v8 走 config 路径，整表替换语义） | ✅ 读-改-写封装 |
| 上游日志 / 错误日志 / 单请求日志 | ✅ |
| 上游 config / config.yaml | ✅ 查看 + 替换 |
| Redis-queue 订阅通道 / error 通道 | ❌ 不订阅（HTTP 端点不暴露，且订阅会**抢走**队列数据） |

> 最后一条值得强调：上游的 `Enqueue` 会优先投递给订阅者；**一旦有人订阅了 redis-queue 通道，
> 记录就不再进入队列**。所以本面板刻意不订阅——否则就会把数据从别人手里抢走。

---

## Roadmap

- [ ] 号池自动补货（对接注册机/发卡接口）
- [ ] 按 Key / 项目的预算与额度熔断
- [ ] 用量异常检测（同模型成本突增、失败率突升）
- [ ] 多用户与细粒度权限
- [ ] OIDC / 反向代理身份头认证

---

## License

MIT —— 见 [LICENSE](LICENSE)。

> 本项目与 CLIProxyAPI 上游项目没有隶属关系，只是它的一个管理面客户端。
