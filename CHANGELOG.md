# 变更记录

## 0.2.0 — 2026-10-05

这一版的重点是**安全护栏**与**上游契约的准确性**：功能与竞品对比后补上缺口，
并把「会静默造成破坏」的几处问题修掉。

### 新增

- **就绪率熔断**（`inspector.circuit_breaker_enabled` / `min_ready_ratio`，默认开、阈值 50%）
  就绪率 = (总数 − 需重登 − 额度耗尽) / 总数。低于阈值时**本轮放弃全部维护动作**，
  只出计划 + 告警 + 审计（`inspection.circuit_open`），巡检结果为 `mode = "circuit_break"`。
  目的：池子集体掉线（上游事故/风控）时，阻止自动巡检把剩下的号一起删掉。
- **证据强度分离**（`evidence_level` = `strong` / `weak`）
  字段级证据（`status`、结构化 `quota.signals`、订阅到期时间戳）才算强证据；
  只有 `status_message` 文案命中的算弱证据。**不可逆动作只对强证据执行**，
  弱证据只写「标记」动作 + 告警（`inspector.act_on_weak_evidence` 默认 `false`）。
- **瞬态错误保护**：带 `timeout` / `connection` / `429` / `rate limit` / `502` 等词的提示
  不再被判为「失效」或「额度耗尽」，而是归入「冷却中」等待自愈。
- **冷却重置**：`POST /api/credentials/{id}/action` 新增 `reset_cooldown`；
  CLI 新增 `cpapanel cool <name>`（自动解析上游要求的 `auth_index`）。
- **批量操作**：`POST /api/credentials/batch`（禁用/启用/删除/刷新/备用池/转出/重置冷却），
  逐项返回结果；批量删除同样受 `inspector.max_deletes_per_run` 限制。
- **清空全部凭证**：`POST /api/credentials/delete-all`，必须带 `confirm="DELETE-ALL"`。
- **OAuth 模型排除 / 模型别名**：`GET/POST /api/upstream/oauth-excluded-models`、
  `…/oauth-model-alias`；面板做「读-改-写」，避免整表替换时静默清空其它渠道。
- **请求日志开关**：`GET/POST /api/upstream/request-log`。

### 修复

- **下游 Key 下发的请求体形状**：改为与官方 WebUI 一致的**裸 JSON 数组**（保留包装体回退）。
  发错形状在部分上游版本上会被当成「空列表」→ **清空全部下游 Key**。
- **凭证删除**：改为官方形状 `DELETE /credentials` + `{"names":[...]}`（保留 query 回退）。
- **Mock 保真度**：`_read_json` 会把非 dict 的 body 静默变成 `{}`，**掩盖了上面的形状问题**；
  拆分为 `_read_json` 与 `_read_json_any`，并让 Mock 记录 raw body 以便断言线上格式。
- `_execute` 现在能正确处理「标记」型动作（只记本地状态，不碰上游）。
- 修掉 `refresh_credential` 里一处被误删的变量赋值。

### 测试

- 147 个单元/集成用例（此前 122）+ 37 项 CLI 冒烟（此前 33），全绿。
- 新增覆盖：熔断、弱证据只标记、瞬态错误归类、批量动作逐项结果、
  清空全部的确认词、冷却重置（含 v0 下明确报 501）、Key 下发的线上格式、
  OAuth 模型排除/别名（含「改一个渠道不清空其它渠道」）、请求日志开关的裸布尔 body。

### 文档

- 全量重写并重新组织：`README.md`（精简）+
  `docs/getting-started.md`、`docs/architecture.md`、`docs/api.md`（取代 `web-api.md`）、
  `docs/upstream-api.md`、`docs/operations.md`、`docs/development.md`、本文件。
- 明确记录了两个高频踩坑：**未配置管理密钥时上游返回 404 而不是 401**、
  **多个采集器共用一个 CPA 会互相抢记录导致数据静默丢失**。

## 0.1.0 — 2026-10-04

首个版本：零依赖 Python 面板 + 内嵌单页 UI。

- 用量采集（消费型队列、幂等去重、按天聚合、缺口检测）与费用估算
- 凭证六态判定、快照与采样历史
- 巡检（dry-run / apply）与备用池
- 双前缀（v0/v8）上游适配与自动探测
- 面板 API 令牌、审计日志、通知（Webhook / Telegram）
- CLI：`init` / `serve` / `collect` / `inspect` / `password` / `token` / `pricing` / `prune` / `version`
- 122 个单元/集成用例 + 33 项 CLI 冒烟；GitHub Actions 在 3.9 与 3.13 上跑测试
