# cpa-panel

自托管的 **CLIProxyAPI（CPA）账号管理面板**。零第三方依赖（Python 3 + 内嵌单页 UI），

```bash
python3 -m cpapanel init --node http://127.0.0.1:8317 --node-key sk-你的管理密钥
python3 -m cpapanel serve --host 127.0.0.1 --port 18317
```

打开 `http://127.0.0.1:18317` 即可。

---

## 它补的是上游缺的那三样

CLIProxyAPI 把 Codex / Claude Code / Antigravity / Gemini CLI 等订阅包装成 OpenAI 兼容 API，
但它的管理 API 只提供**原始数据**：一份凭证快照、一条每秒被消费的用量队列、一些状态文案。
cpa-panel 补的是上游不提供的三件事 —— **历史、判定、护栏**：

| 上游给的 | 不加处理会发生什么 | cpa-panel 的做法 |
| --- | --- | --- |
| 用量队列（取出即删、只留约 60 秒） | 不采就永久丢；多个采集器还会互相抢记录 | 秒级落库、数据缺口检测、成本估算与告警 |
| `status_message` 自由文案 | 「429 限流」和「号废了」看起来一样 | 六态判定 + **强/弱证据分离**（只有字段级证据才允许动手） |
| 原始 auth-file 列表 | 几百个号里分不清哪个该续、哪个该删 | 巡检计划（先 dry-run 出计划，再 apply 执行）+ 备用池 |
| 没有 | 池子集体掉线时，自动脚本会把剩下的号也删光 | **就绪率熔断**：低于阈值本轮直接停手并告警 |

## 核心能力

- **用量**：请求/Token/延迟趋势、按模型 / 按凭证 / 按下游 Key 三个维度分解、费用估算、原始事件检索与导出。
- **凭证**：六态分类（健康 / 冷却 / 额度耗尽 / 需重登 / 已禁用 / 未知）并保留**判定依据**；单个与批量操作（禁用 / 启用 / 删除 / 刷新 / 备用池 / 重置冷却）。
- **巡检**：定时或手动，产出「计划 → 执行」两段式；dry-run 默认开启，计划与执行结果都进审计。
- **熔断与证据闸门**：见下文「默认安全策略」。
- **OAuth 与配额**：设备码/回调流程、主动配额查询、OAuth 模型排除与模型别名。
- **下游 Key**：整表替换、掩码展示（永不回明文）、按 Key 用量。
- **运维**：审计日志、验收/错误日志、数据清理、`/metrics`、CLI 与面板 API 双入口。

## 默认安全策略（这些默认值不是随手选的）

| 默认值 | 为什么 |
| --- | --- |
| `inspector.dry_run = true` | 第一次接上一个真实池子时，你应该先看它**想做什么**，而不是让它直接做 |
| `delete_unauthorized / delete_quota_exhausted = false` | 删号不可逆。默认只禁用/进备用池，保留随时恢复的可能 |
| `max_deletes_per_run = 20` | 单轮删除上限；手动批量删除也受同一个上限约束 |
| `circuit_breaker_enabled = true`（`min_ready_ratio = 0.5`） | 就绪率 < 50% 时判定为「上游事故」而不是「这些号都该删」，本轮只出计划 |
| `act_on_weak_evidence = false` | 只有文案线索时**只标记不动作**（详见下） |

### 为什么区分「强证据」和「弱证据」

同一个「额度耗尽」，可能来自上游的结构化字段（`quota.signals.quota_exhausted`、状态码、订阅到期时间戳），
也可能只是 `status_message` 里的一句 `429 too many requests`。后者经常是**几分钟后自己就好的限流**。

所以判定结果里带 `evidence_level`：

- `strong`（字段级）→ 允许执行不可逆动作
- `weak`（纯文案）→ 只标记 + 告警；想放开得显式改 `inspector.act_on_weak_evidence`

同理，带瞬态词（`timeout` / `connection` / `429` / `rate limit` / `502` …）的提示**永远不会**被当成
「凭证失效」或「额度耗尽」，而是归入「冷却中」，等它自愈。

## 不要做的事

- **不要和其它采集器共用一个 CPA 实例**：上游用量队列是消费型的（取出即删），多个消费者会互相抢记录，
  导致**双方的统计都静默不完整**。要么只跑一个采集器，要么所有采集器都改用上游的订阅模式。详见 [docs/upstream-api.md](docs/upstream-api.md)。
- **不要把面板直接暴露在公网**：默认只监听 `127.0.0.1`。要远程访问请放在反向代理后面并开 HTTPS。
- **不要跳过 dry-run 直接上 `--apply`**：先用 `inspect` 看计划。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/getting-started.md](docs/getting-started.md) | 安装、初始化、反向代理、Docker、常见错误 |
| [docs/architecture.md](docs/architecture.md) | 模块划分、数据流、数据库表、状态机与熔断设计 |
| [docs/api.md](docs/api.md) | 面板 HTTP API 参考 |
| [docs/upstream-api.md](docs/upstream-api.md) | 上游 CPA 契约、请求体形状的坑、能力边界 |
| [docs/operations.md](docs/operations.md) | 日常运维与故障处置 |
| [docs/development.md](docs/development.md) | 开发、测试、以及本项目的环境陷阱 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |

## 开发

```bash
make check     # 全量测试 + CLI 端到端冒烟（提交前跑这一条）
make test      # 147 个单元/集成用例
make smoke     # 37 项 CLI 冒烟（对着内置 Mock CPA 演练 init→collect→inspect→apply）
```

零第三方依赖是刻意约束：目标机器上只需要一个 `python3`，`pip install` 都不需要。

## 许可

MIT，见 [LICENSE](LICENSE)。
