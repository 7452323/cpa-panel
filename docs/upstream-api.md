# CLIProxyAPI（CPA）Management API —— 本项目依赖的真实契约

> 本文件不是猜测，是从上游源码抄录的契约，用于校验 `cpapanel/cpa.py` 的实现。
> 来源：`router-for-me/CLIProxyAPI` @ `8ef43e4df3b216a42493105d31c2873b69191473`（v8 分支）
> 参考文件：`internal/api/server_management.go`、`internal/api/server_management_v8.go`、
> `docs/management-api-v8.md`、`internal/api/handlers/management/auth_files.go`、
> `internal/api/handlers/management/usage.go`、`internal/redisqueue/queue.go`

## 1. 鉴权

```
Authorization: Bearer <management-key>
X-Management-Key: <management-key>          # 二者任一
```

- 管理路由只有在「配置了管理密钥」后才注册（`managementRoutesEnabled`）。
- 未配置密钥时，所有 `/v0/management/*`、`/v8/management/*` 返回 **404**（不是 401）。
  因此面板必须把「404 on management path」识别为「上游未启用管理 API / 密钥不对」，而不是「路径不存在」。
- Home 模式下管理端点整体关闭。
- **OAuth 回调**（`/oauth/callback`）不要求管理密钥，它校验 pending state。

## 2. 两套前缀

上游同时提供 v0（旧，保留兼容）与 v8（新，推荐 ≥ v8 核心）。同一业务在两套前缀下路径不同：

| 业务 | v0 | v8 |
| --- | --- | --- |
| 配置(JSON) | `GET/PUT /v0/management/config` | `GET/PUT/PATCH /v8/management/config` |
| 配置(YAML) | `GET/PUT /v0/management/config.yaml` | `GET/PUT /v8/management/config.yaml` |
| 凭证列表 | `GET /v0/management/auth-files` | `GET /v8/management/credentials` |
| 凭证上传 | `POST /v0/management/auth-files` | `POST /v8/management/credentials` |
| 凭证删除 | `DELETE /v0/management/auth-files?name=` | `DELETE /v8/management/credentials?name=` |
| 凭证下载 | `GET /v0/management/auth-files/download?name=` | `GET /v8/management/credentials/download?name=` |
| 启停凭证 | `PATCH /v0/management/auth-files/status` | `PATCH /v8/management/credentials/status` |
| 改凭证字段 | `PATCH /v0/management/auth-files/fields` | `PATCH /v8/management/credentials/fields` |
| 刷新凭证 | `POST /v0/management/auth-files/refresh` | `POST /v8/management/credentials/refresh` |
| 凭证可用模型 | `GET /v0/management/auth-files/models?name=` | `GET /v8/management/credentials/models?name=` |
| 用量队列 | `GET /v0/management/usage-queue` | `GET /v8/management/observability/usage/queue` |
| Key 用量 | `GET /v0/management/api-key-usage` | `GET /v8/management/observability/usage/api-keys` |
| 应用日志 | `GET/DELETE /v0/management/logs` | `GET/DELETE /v8/management/observability/logs` |
| 错误日志列表 | `GET /v0/management/request-error-logs` | `GET /v8/management/observability/logs/errors` |
| 错误日志下载 | `GET /v0/management/request-error-logs/:name` | `GET /v8/management/observability/logs/errors/:name` |
| 单条请求日志 | `GET /v0/management/request-log-by-id/:id` | `GET /v8/management/observability/logs/requests/:id` |
| 下游 Key 列表 | `GET/PUT/PATCH/DELETE /v0/management/api-keys` | `GET/PUT/PATCH/DELETE /v8/management/config/access/api-keys` |
| OAuth 登录 | `GET /v0/management/<provider>-auth-url` | `GET /v8/management/oauth/auth-url?provider=<provider>` |
| OAuth 状态 | `GET /v0/management/get-auth-status` | `GET /v8/management/oauth/status` |
| OAuth 取消 | `DELETE /v0/management/oauth-session` | `DELETE /v8/management/oauth/session` |
| 用量统计开关 | `GET/PUT /v0/management/usage-statistics-enabled` | 走 `config` |
| 配额(provider 级) | `GET /v0/management/quota/providers`、`POST /quota/fetch`、`POST /quota/reset` | 同上（v0 保留） |
| 插件 | `/v0/management/plugins` | `/v8/management/plugins` |

v8 的 `/<provider>-auth-url` 等别名不再单独注册，OAuth 统一走 `/oauth/*`。
v8 的 `/config/<section>/<field>` 支持按路径读写字段：

```
PATCH /v8/management/config        {"routing":{"retry":{"request-retry":0}}}
GET   /v8/management/config/access/api-keys
PUT   /v8/management/config/access/api-keys      ["client-key-1","client-key-2"]
DELETE /v8/management/config/<path>
```

> 注意：JSON 写入直接给值，**没有** `{"value": ...}` 外壳；PUT 是替换，PATCH 是合并（对象合并、列表与标量替换）。

## 3. 凭证列表返回结构

`GET .../auth-files`（无分页参数时）

```json
{
  "observed_at": "2026-10-05T00:00:00Z",
  "files": [ { "...entry..." } ]
}
```

带 `?page=&page_size=` 时额外附带 `total`、`page`、`page_size`、`has_more`。
查询过滤：`?name=<精确名>`、`?auth_index=<索引>`。

单个 entry（`buildAuthFileEntryLocked`）的字段：

| 字段 | 含义 |
| --- | --- |
| `id` / `auth_index` / `name` | 标识（`name` 通常为 `xxx.json`；`auth_index` 是稳定的凭证索引） |
| `type` / `provider` | 提供方（`codex` / `claude` / `gemini` / `antigravity` / `xai` / `kimi` / `qwen` / `iflow` / `vertex` / `devin` / `meta` …） |
| `label`、`note`、`priority`、`weight`、`websockets` | 运维用字段（可 PATCH `fields` 修改） |
| `status` | `active` / `error` / `disabled`（还有上游自定义态） |
| `status_message` | 人类可读原因，常见 `token expired`、`removed via management api` |
| `disabled` | 是否被禁用 |
| `unavailable` | 调度器冷却判定结果（综合凭证级与模型级冷却） |
| `success` / `failed` | 历史成功/失败计数 |
| `recent_requests` | 最近请求快照 |
| `quota` | `{observed_at, signals:{...}}`（被动观测，不含冷却） |
| `model_quotas` | `{model: {observed_at, signals}}` |
| `supports_quota` / `quota_provider` / `quota_probe` | 该凭证是否支持主动配额查询 |
| `email` / `project_id` / `account` / `account_type` | 账号信息 |
| `created_at` / `modtime` / `updated_at` / `last_refresh` | 时间戳 |
| `next_retry_after` | 冷却结束时间（有值且在未来 = 正在冷却） |
| `path` / `source` / `size` | 物理文件信息（`source=file|memory`）；`runtime_only=true` 表示仅内存凭证 |
| `id_token` | 仅 Codex：`{chatgpt_account_id, plan_type, chatgpt_subscription_active_start/until}` |
| `request_retry` | 该凭证的请求重试覆盖值 |
| `cooldowns` | 冷却快照（Home 模式为 `null`） |

**状态语义**：`disabled=true` 优先；`unavailable=true` + `next_retry_after` 在未来表示冷却中；
`status=error` + `status_message=token expired` 或 `id_token` 过期表示**需要重新登录**（真实失效）。

## 4. 凭证写操作

```
PATCH .../auth-files/status   {"name":"a.json","auth_index":"idx-1","disabled":true}
PATCH .../auth-files/fields   {"name":"a.json","priority":10,"note":"backup"}
POST  .../auth-files/refresh  {"all":true}            # 或 {"name":"a.json"}
POST  .../auth-files          multipart/form-data, 文件必须是 .json（字段名 file）
DELETE.../auth-files?name=a.json
GET   .../auth-files/download?name=a.json            # 返回文件字节
GET   .../auth-files/models?name=a.json              # {"models":[{id,display_name,type,owned_by}]}
```

上传校验：必须是 `.json`（`errAuthFileMustBeJSON`），文件名不含 `/`、`\`（`isUnsafeAuthFileName`）。

## 5. 用量队列（本项目最关键的上游特性）

```
GET /v8/management/observability/usage/queue?count=50
→ 200 [ <record>, <record>, ... ]        # 记录本身是 JSON 对象
```

```go
func (h *Handler) GetUsageQueue(c *gin.Context) {
    count, errCount := parseUsageQueueCount(c.Query("count"))   // 缺省 1，必须为正整数
    items := redisqueue.PopOldest(count)                        // ★ 取出即移除
    ...
}
```

**队列语义（`internal/redisqueue/queue.go`）**

- `PopOldest` 是**消费型读取**：记录被取出后从队列消失，面板必须落库，否则永久丢失。
- 保留窗口 `defaultRetentionSeconds = 60`（上限 `maxRetentionSeconds = 3600`），
  即**超过 60 秒未被取走的记录会被自动淘汰**。
- 队列只有在「配置了管理密钥」时才启用；写入还受配置项 `usage-statistics-enabled` 控制
  （`GET/PUT /v0/management/usage-statistics-enabled`）。
- 队列为空时返回 `[]`；`count` 非正整数返回 400。
- 另外存在 **error 通道**（`redisqueue.EnqueueError`），只能通过 WebSocket/订阅消费，
  HTTP 端点只暴露 usage 通道 → 面板的错误信息应从 `request-error-logs` 与请求日志补齐。

**工程结论（面板的实现约束）**

1. 轮询间隔必须 **远小于 60 秒**（默认 15s，配置 `collector.interval_seconds`）。
2. 必须做幂等去重（优先用记录自带的 request id，其次对原始 JSON 取 SHA-1）。
3. 必须记录「采集间隙」，一旦相邻成功采集的间隔超过保留窗口，要在审计里标注**可能丢数据**。
4. `count` 默认 1，面板应显式传更大的批量（默认 50）以减少往返。

## 6. unknown-by-design：用量记录的字段

上游把记录当 **不透明 JSON 字节** 在队列里搬运（`usageQueueRecord []byte`），
HTTP 层直接原样返回，因此**字段名没有权威 schema**：不同版本/提供方可能不同。

本项目的处理方式（`cpapanel/models.py`）：

- 用**别名表**做宽容归一化（`request_id|id|trace_id`、`prompt_tokens|input_tokens`、`ts|timestamp|time` …）。
- 无论归一化是否成功，**原始 JSON 一律落库**（`usage_events.raw_json`），保证信息不丢失。
- 归一化只是「尽力而为」，缺失字段留空而不是报错。

> 这是本项目与「假装知道上游字段」的实现最大的区别：我们承认不确定，并把不确定性显式建模。
