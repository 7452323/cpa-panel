# cpa-panel HTTP API

面板自身的 API。除了标注「公开」的端点，全部需要认证。

**认证方式（二选一）**

```bash
# 1) 会话 Cookie（Web UI 用；登录后由浏览器自动携带）
curl -c jar -X POST http://127.0.0.1:18317/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的口令"}'

# 2) API 令牌（脚本 / CI 用；用 `cpapanel token` 生成）
curl -H "Authorization: Bearer <panel-token>" http://127.0.0.1:18317/api/overview
```

**写操作必须带 `X-CPA-Panel: 1` 头**。这不是可有可无的——用会话认证时缺这个头会返回 403（CSRF 防护）。
（用 Bearer 令牌认证时不需要，因为令牌不依赖浏览器自动携带。）

**错误格式**：任何非 2xx 都是 JSON `{"error": "…"}`，另可能带 `hint` / `status` / `code`。

---

## 会话与凭据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/session` | **公开**。返回是否已登录、版本 |
| POST | `/api/login` | **公开**。`{username,password}` → 设置 Cookie |
| POST | `/api/logout` | 清 Cookie |
| POST | `/api/password` | `{old_password,new_password}`（新口令 ≥ 8 位） |
| GET | `/api/tokens` | 列出面板 API 令牌（只含元信息） |
| POST | `/api/tokens` | `{name,role}` → 明文令牌**只返回一次** |
| DELETE | `/api/tokens/{id}` | 吊销 |

## 节点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/nodes` | 列出节点（`management_key` 已掩码，另有 `has_key`） |
| POST | `/api/nodes` | `{name,base_url,management_key,api_prefix}`，`api_prefix ∈ auto\|v0\|v8` |
| PATCH | `/api/nodes/{id}` | 改名称/地址/密钥/前缀/启用；密钥留空表示不改 |
| DELETE | `/api/nodes/{id}` | 删除节点（用量历史保留） |
| POST | `/api/nodes/{id}/test` | 连通性自检 → `{ok,prefix,version,usage_statistics_enabled,hint?}` |

> `POST /api/nodes/{id}/test` 是排障第一站：它会把「上游没开管理 API（404）」和
> 「管理密钥错（401）」区分开，并给出对应提示。

## 凭证（账号）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/credentials` | 参数 `node_id,provider,state,q,standby,include_removed`。返回列表 + 状态计数 + 提供方分布 |
| POST | `/api/credentials/sync` | `{node_id}` 从上游拉全量快照并计算差异 → `{added,changed,removed,unchanged,changes[]}` |
| GET | `/api/credentials/{id}` | 详情（含巡检采样历史 `samples[]`） |
| PATCH | `/api/credentials/{id}` | `{disabled?,priority?,note?,weight?,standby?}`；会**同步推给上游** |
| POST | `/api/credentials/{id}/action` | `{action}`：`refresh\|disable\|enable\|delete\|standby\|promote` |
| GET | `/api/credentials/{id}/models` | 该凭证可用的模型列表 |
| GET | `/api/credentials/{id}/download` | 下载原始凭证 JSON（attachment） |
| POST | `/api/credentials/import` | multipart，字段名 `file`（可多选）+ 可选 `node_id` |
| GET | `/api/credentials/events` | 凭证变更事件流（新增/消失/状态变化） |

`state` 取值：`healthy` `cooling` `quota_exhausted` `unauthorized` `disabled` `unknown`。

## OAuth

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/oauth/start` | `{node_id?,provider}` → `{url,state}`；provider ∈ `claude,codex,antigravity,kimi,kimi-ai,xai,devin,meta` |
| GET | `/api/oauth/status?state=` | 轮询直到 `status` 为 `ok`/`success`（其余如 `wait`/`error`） |
| DELETE | `/api/oauth/session?state=` | 取消 |

## 用量

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/usage/summary?days=&node_id=` | 汇总 + 今日 + 采集器状态 |
| GET | `/api/usage/series?days=` | 按天序列（图表用） |
| GET | `/api/usage/models?days=&limit=` | 按模型聚合 |
| GET | `/api/usage/credentials?days=&limit=` | 按凭证聚合（已关联凭证名） |
| GET | `/api/usage/keys?days=` | 按下游 Key 聚合 |
| GET | `/api/usage/events` | 明细，参数 `node_id,model,credential,api_key,errors_only,hours,limit,offset`。**只回 Key 掩码，不含原始 JSON** |
| GET | `/api/usage/events/{id}` | 单条明细 + 原始 JSON（`raw`） |
| POST | `/api/usage/collect` | 手动采集一次；返回恒为 `{ok,result:{nodes:[…]}}` |

> **注意**：采集是「消费型」的——手动调一次 `/api/usage/collect` 会真的把上游队列里的记录取走。
> 正常情况下你不需要调它，后台线程每 15 秒已经在做。

## 下游 API Key

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/keys?node_id=` | 列表（掩码 + 用量），**不回明文** |
| POST | `/api/keys/sync` | `{node_id?}` 从上游同步 |
| POST | `/api/keys` | `{node_id?,key}` 新增（内部做读-改-写，因为上游是整表替换语义） |
| DELETE | `/api/keys` | `{node_id?,key}` 删除（DELETE 也带 JSON body） |

## 配额

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/quotas?node_id=` | 各凭证的 `quota.signals`、冷却到期、订阅到期 |
| POST | `/api/quotas/fetch` | `{node_id?,name?,auth_index?}` 主动查配额（需上游支持） |

## 巡检

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/inspections?node_id=&limit=` | 历史巡检记录（含计数与池子水位） |
| POST | `/api/inspections/run` | `{node_id?,dry_run?}`；`dry_run` 缺省取面板配置（默认 true） |
| GET | `/api/actions?node_id=&limit=` | 维护动作明细（含 `planned`/`ok`/`failed`） |

## 配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/config` | 面板配置（**已脱敏**：口令哈希与通知密钥不回明文） |
| PUT | `/api/config` | 只接受白名单键（见下），其余忽略并在 `applied` 中如实反映 |
| GET | `/api/upstream/config` | 上游原始 config JSON |
| GET | `/api/upstream/config-yaml` | 上游 config.yaml 原文 |
| PUT | `/api/upstream/config-yaml` | `{yaml,node_id?}` **整份替换**上游配置（与上游 PUT 语义一致） |

可写白名单：

```
log_level, public_url, trust_proxy_headers, pricing_file,
collector.enabled, collector.interval_seconds, collector.batch_size,
collector.queue_retention_seconds, collector.gap_warn_ratio,
inspector.enabled, inspector.interval_seconds, inspector.dry_run,
inspector.disable_unauthorized, inspector.disable_quota_exhausted,
inspector.delete_unauthorized, inspector.delete_quota_exhausted,
inspector.max_deletes_per_run, inspector.standby_pool, inspector.target_active,
inspector.promote_standby_when_low,
notify.webhook_url, notify.telegram_bot_token, notify.telegram_chat_id,
notify.min_interval_seconds
```

> 改完 `collector.*` / `inspector.*` 会**热重启**对应的后台线程。
> 改 `notify.webhook_url` / `notify.telegram_bot_token` 时注意：GET 返回的是掩码值，
> 别把掩码写回去——留空表示「保持不变」。

## 日志

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/logs?node_id=&limit=` | 上游应用日志（按行返回） |
| DELETE | `/api/logs?node_id=` | 清空上游日志 |
| GET | `/api/logs/panel?limit=&after_seq=&level=` | 面板自身日志；用 `after_seq` 增量轮询 |
| GET | `/api/logs/errors?node_id=` | 上游错误日志列表 |
| GET | `/api/logs/errors/{name}?node_id=` | 下载单个错误日志（`text/plain`） |

## 系统

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | **公开**。`{ok,app,version,uptime_seconds,database}` |
| GET | `/metrics` | **公开**。Prometheus 文本格式 |
| GET | `/api/overview` | 总览页聚合数据（含 `alerts[]`） |
| GET | `/api/audit?limit=&action=` | 审计日志 |
| GET | `/api/settings` / PUT | 面板级偏好（`inspector.auto`、`ui.*`） |
| POST | `/api/notify/test` | 发一条测试通知 |
| POST | `/api/maintenance/prune` | `{usage_days?,sample_days?,audit_days?}` 清理过期数据 |

---

## 示例

```bash
BASE=http://127.0.0.1:18317
TOKEN=<panel-token>

# 看总览（含告警）
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/overview | python3 -m json.tool

# 手动采集一次并只看结果
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'X-CPA-Panel: 1' \
  -H 'Content-Type: application/json' -d '{}' $BASE/api/usage/collect

# 查最近 24 小时的错误请求
curl -s -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/usage/events?errors_only=true&hours=24&limit=20"

# 跑一次 dry-run 巡检（不改上游）
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'X-CPA-Panel: 1' \
  -H 'Content-Type: application/json' -d '{"dry_run":true}' $BASE/api/inspections/run

# Prometheus 抓取
curl -s $BASE/metrics
```
