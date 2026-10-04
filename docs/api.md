# 面板 HTTP API

## 约定

- **Base URL**：面板自己的地址，默认 `http://127.0.0.1:18317`
- **鉴权**：二选一
  - 浏览器：登录后的会话 Cookie
  - 脚本/CI：`Authorization: Bearer <面板令牌>`（用 `python3 -m cpapanel token --name x` 生成，或面板的「令牌」页）
- **CSRF 头**：`POST/PUT/PATCH/DELETE` 需要 `X-CPA-Panel: 1`
  —— **仅当用会话 Cookie 鉴权时要求**（令牌天然不受 CSRF 影响，所以用令牌的脚本不用带这个头）
- **权限**：所有写操作要求 `admin` 角色
- **认证方式**：所有端点默认要求登录，**只有这四个公开**：
  `/api/health`、`/api/login`、`/api/session`、`/metrics`
- **`node_id`**：多节点时用 `?node_id=` 或 body 里的 `node_id` 指定操作哪个 CPA；
  不传则用第一个启用的节点（没有节点时报 400）
- **错误**：

| 状态码 | 含义 |
| --- | --- |
| 400 | 参数错误（例如危险操作缺确认词、`ids` 为空） |
| 401 | 未登录（会话过期，或面板令牌不存在） |
| 403 | 需要管理员权限 / 缺少 CSRF 头 |
| 404 | 未知接口 |
| 413 | 请求体过大 |
| 502 | **上游（CPA）返回了错误**：包括上游未开管理 API（404）、管理密钥无效（401）等 |
| 500 | 面板内部异常（会记日志） |

注意：上游的错误**不**直接透传状态码，统一是 **502**，body 里带上游本来返回的内容与状态码。
所以脚本判断「密钥不对」不能只看 401，要看 body 里的上游信息。

---

## 认证与会话

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/login` | 登录，下发会话 Cookie |
| POST | `/api/logout` | 退出 |
| GET | `/api/session` | 当前会话（未登录时也返回 200，供前端判断是否显示登录页） |
| POST | `/api/password` | 修改自己的口令 |
| GET | `/api/tokens` | 列出面板 API 令牌（只返回掩码） |
| POST | `/api/tokens` | 创建令牌（明文只返回一次） |
| DELETE | `/api/tokens/{token_id}` | 删除令牌 |

## 节点（被管理的 CPA）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/nodes` | 列出节点 |
| POST | `/api/nodes` | 新增节点 |
| PATCH | `/api/nodes/{node_id}` | 改地址/密钥/前缀/启用状态 |
| DELETE | `/api/nodes/{node_id}` | 删除节点 |
| POST | `/api/nodes/{node_id}/test` | 连通性测试（探测 v0/v8、校验密钥） |

## 凭证

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/credentials` | 列表；筛选参数 `q`（名字）、`state`、`standby`、`node_id` |
| POST | `/api/credentials/sync` | 从上游拉一次快照并落库 |
| GET | `/api/credentials/{cred_id}` | 详情（含判定依据 `evidence`、状态采样历史） |
| PATCH | `/api/credentials/{cred_id}` | 改**本地**标记（如 `standby`） |
| POST | `/api/credentials/{cred_id}/action` | 单个动作：`refresh` / `disable` / `enable` / `delete` / `standby` / `promote` / `reset_cooldown` |
| POST | `/api/credentials/batch` | 批量动作，body `{action, ids:[...]}`，**逐项返回结果** |
| POST | `/api/credentials/delete-all` | ⚠️ 清空上游全部凭证，body 必须有 `confirm: "DELETE-ALL"` |
| GET | `/api/credentials/{cred_id}/models` | 该凭证支持的模型 |
| GET | `/api/credentials/{cred_id}/download` | 下载凭证文件 |
| POST | `/api/credentials/import` | 上传凭证（multipart，`.json`） |
| GET | `/api/credentials/events` | 新增/消失/状态变化事件流 |

## OAuth

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/oauth/start` | 发起 OAuth（返回授权 URL 与 state） |
| GET | `/api/oauth/status` | 轮询授权状态 |
| DELETE | `/api/oauth/session` | 取消当前授权会话 |

## 配额

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/quotas` | 已缓存的配额信息 |
| POST | `/api/quotas/fetch` | 主动向上游查询配额 |

## 用量

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/usage/summary` | 汇总（请求数/token/成本，支持 `days`） |
| GET | `/api/usage/series` | 时间序列（趋势图） |
| GET | `/api/usage/models` | 按模型分解 |
| GET | `/api/usage/credentials` | 按凭证分解 |
| GET | `/api/usage/keys` | 按下游 Key 分解 |
| GET | `/api/usage/events` | 明细检索（模型/凭证/状态/时间过滤） |
| GET | `/api/usage/events/{event_id}` | 单条明细 |
| POST | `/api/usage/collect` | 立即采集一次（返回 `{ok, result:{nodes:[…]}}`） |

## 下游 Key

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/keys` | 列出（**只返回掩码**，永不回明文） |
| POST | `/api/keys/sync` | 从上游拉取 Key 列表落库 |
| POST | `/api/keys` | 新增一个 Key |
| DELETE | `/api/keys` | 删除一个 Key |

## 面板配置

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/config` | 当前配置（脱敏） |
| PUT | `/api/config` | 覆盖配置（白名单字段） |
| GET | `/api/settings` | 运行时设置 |
| PUT | `/api/settings` | 改运行时设置（巡检策略、通知、熔断阈值…） |

## 上游配置

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/upstream/config` | 上游 JSON 配置 |
| GET | `/api/upstream/config-yaml` | 上游 config.yaml 原文 |
| PUT | `/api/upstream/config-yaml` | ⚠️ **整体替换**上游 config.yaml |
| GET | `/api/upstream/oauth-excluded-models` | 各渠道「不接的模型」 |
| POST | `/api/upstream/oauth-excluded-models` | 设置某渠道：body `{provider, models:[…]}`，空数组=删除该渠道 |
| GET | `/api/upstream/oauth-model-alias` | 模型别名 |
| POST | `/api/upstream/oauth-model-alias` | 设置某渠道：body `{channel, aliases:[{name, alias}]}` |
| GET | `/api/upstream/request-log` | 请求日志开关当前值（读不到返回 `null`，不猜） |
| POST | `/api/upstream/request-log` | 开关请求日志：body `{enabled: true/false}` |

## 日志

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/logs` | 上游请求日志 |
| DELETE | `/api/logs` | 清空上游日志 |
| GET | `/api/logs/panel` | 面板自身日志 |
| GET | `/api/logs/errors` | 上游错误日志列表 |
| GET | `/api/logs/errors/{name}` | 下载某个错误日志 |

## 巡检与审计

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/inspections` | 巡检历史（每轮的结果统计） |
| POST | `/api/inspections/run` | 跑一次巡检：body `{node_id?, dry_run?}` |
| GET | `/api/actions` | 维护动作记录（计划与实际结果） |
| GET | `/api/audit` | 审计日志（支持 `action`、`limit`） |

## 系统

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/overview` | 概览页数据（池子状态、告警、用量摘要） |
| POST | `/api/notify/test` | 发一条测试通知 |
| POST | `/api/maintenance/prune` | 清理过期数据 |
| GET | `/api/health` | 健康检查（**公开**） |
| GET | `/metrics` | Prometheus 文本格式指标（**公开**） |

---

## 危险端点

| 端点 | 防护 |
| --- | --- |
| `POST /api/credentials/delete-all` | 必须 `confirm: "DELETE-ALL"`；不可逆；**巡检永远不会调用它** |
| `POST /api/credentials/batch`（`action=delete`） | 受 `inspector.max_deletes_per_run`（默认 20）限制 —— 手动批量也**不能**绕过巡检的删除上限 |
| `PUT /api/upstream/config-yaml` | 全量替换：写坏了上游起不来。先备份 |
| `DELETE /api/credentials/{id}` | 单个删除，不可逆（建议优先用「禁用 / 备用池」） |

## 示例

跑一次巡检（dry-run，出计划）：

```bash
curl -s -X POST http://127.0.0.1:18317/api/inspections/run \
  -H "Authorization: Bearer <面板令牌>" \
  -H "Content-Type: application/json" \
  -d '{"node_id": 1, "dry_run": true}'
```

返回（节选）：

```json
{
  "ok": true,
  "result": {
    "nodes": [{
      "node_id": 1, "node_name": "default", "ok": true,
      "mode": "dry_run",
      "circuit": {"enabled": true, "open": false, "ready": 5, "total": 6,
                  "ready_ratio": 0.8333, "threshold": 0.5, "reason": ""},
      "counts": {"healthy": 2, "cooling": 1, "quota_exhausted": 1,
                 "unauthorized": 1, "disabled": 1, "unknown": 0},
      "planned": 2, "executed": 0,
      "actions": [
        {"action": "standby", "name": "codex-2.json", "result": "planned"},
        {"action": "disable", "name": "gemini-1.json", "result": "planned"}
      ]
    }]
  }
}
```

批量禁用两个凭证：

```bash
curl -s -X POST http://127.0.0.1:18317/api/credentials/batch \
  -H "Authorization: Bearer <面板令牌>" -H "X-CPA-Panel: 1" \
  -H "Content-Type: application/json" \
  -d '{"action": "disable", "ids": [3, 4]}'
```

```json
{"ok": true, "action": "disable", "requested": 2, "succeeded": 2,
 "results": [{"id": 3, "name": "codex-2.json", "ok": true},
             {"id": 4, "name": "gemini-1.json", "ok": true}]}
```

> 注意：**一个失败不会把整批标成失败**（`ok` 为 `false` 但 `succeeded` 是真实成功数），
> 每一项都有自己的结果与失败原因。
