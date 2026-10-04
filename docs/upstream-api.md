# 上游契约（CLIProxyAPI）

本项目对上游的所有调用都集中在 `cpapanel/cpa.py`。这份文档记录**实测出来的**契约与坑。

## 两代管理 API

| | 旧 | 新 |
| --- | --- | --- |
| 前缀 | `/v0/management` | `/v8/management` |
| 凭证 | `/auth-files` | `/credentials` |
| 用量队列 | `/usage-queue` | `/observability/usage/queue` |
| 下游 Key | `/api-keys` | `/config/access/api-keys` |
| 日志 | `/logs` | `/observability/logs` |

节点的 `api_prefix` 可以是 `auto` / `v0` / `v8`。`auto` 会先试 v8 再退 v0
（探测请求是 `GET /vN/management/config`）。

### 两个必须先知道的坑

1. **上游没有配置管理密钥时，整个管理 API 返回 404，不是 401。**
   所以「404」有两种含义：前缀不对，或者根本没开管理 API。面板的报错会把这两种情况区分开。
2. **密钥错误才是 401。** 探测时如果拿到 401，说明**前缀是对的**，问题在密钥 ——
   面板据此直接给出「管理密钥无效」而不是继续尝试另一个前缀。

## 鉴权

```
Authorization: Bearer <管理密钥>
X-Management-Key: <管理密钥>        # 二选一
```

## 路径对照表

（与 `cpapanel/cpa.py` 的 `PATHS` 一致；`—` 表示该前缀下不存在）

| 用途 | v0 | v8 |
| --- | --- | --- |
| 配置（JSON） | `/config` | `/config` |
| 配置（YAML） | `/config.yaml` | `/config.yaml` |
| 凭证列表 / 上传 | `/auth-files` | `/credentials` |
| 凭证删除 | `/auth-files` | `/credentials` |
| 凭证支持模型 | `/auth-files/models` | `/credentials/models` |
| 凭证下载 | `/auth-files/download` | `/credentials/download` |
| 凭证启停 | `/auth-files/status` | `/credentials/status` |
| 凭证字段 | `/auth-files/fields` | `/credentials/fields` |
| 凭证刷新 | `/auth-files/refresh` | `/credentials/refresh` |
| 用量队列 | `/usage-queue` | `/observability/usage/queue` |
| 按 Key 用量 | `/api-key-usage` | `/observability/usage/api-keys` |
| 日志 | `/logs` | `/observability/logs` |
| 错误日志 | `/request-error-logs` | `/observability/logs/errors` |
| 单请求日志 | `/request-log-by-id/<id>` | `/observability/logs/requests/<id>` |
| 下游 Key | `/api-keys` | `/config/access/api-keys` |
| OAuth 授权 URL | `/get-auth-status`… 见下 | `/oauth/auth-url` |
| OAuth 状态 | `/get-auth-status` | `/oauth/status` |
| OAuth 会话 | `/oauth-session` | `/oauth/session` |
| 版本 | `/latest-version` | `/server/latest-version` |
| 插件 | `/plugins` | `/plugins` |
| 用量统计开关 | `/usage-statistics-enabled` | — |
| 配额（providers/fetch/reset） | `/quota/*` | — |
| **冷却重置** | — | `/routing/cooldown/reset` |
| **OAuth 排除模型** | — | `/config/oauth/excluded-models` |
| **OAuth 模型别名** | — | `/config/oauth/model-alias` |
| **请求日志开关** | — | `/config/observability/logs/request-log` |

> v0 下发起 OAuth 用的是按 provider 分开的历史路径（`/anthropic-auth-url`、`/codex-auth-url`、
> `/antigravity-auth-url`、`/kimi-auth-url`…），代码里在 `V0_PROVIDER_AUTH_URLS`。

## 请求体形状（这里最容易写错，且往往不报错）

| 操作 | 正确的 body | 写错会怎样 |
| --- | --- | --- |
| `PUT /config/access/api-keys` | **裸 JSON 数组** `["sk-a","sk-b"]` | 发成 `{"api-keys":[...]}` 时，部分上游版本会当成「没有 api-keys 字段」→ **把下游 Key 全部清空** |
| `PUT /config/observability/logs/request-log` | **裸布尔** `true` / `false` | 发成 `{"enabled":true}` 可能被当成无效值 |
| `DELETE /credentials`（单个也走批量） | `{"names": ["a.json"]}` | 用 query 参数的上游版本存在，代码里保留回退 |
| `DELETE /credentials?all=true` | 清空全部（**不可逆**） | — |
| `POST /credentials/refresh` | 单个 `{"name": "a.json"}`；全部 `{"all": true}` | 全部刷新官方给的超时是 300 秒 |
| `POST /routing/cooldown/reset` | `{"auth_index": "idx-1"}` | **只认 auth_index，不认 name**；传 name 等于什么都没做 |
| `PATCH /credentials/status` | `{"name": "...", "disabled": true}`（可带 `auth_index`） | 建议不带 `auth_index` 时**不要发 `null`** |
| `PATCH /credentials/fields` | `{"name": "...", ...字段}` | 可改 `priority` / `note` / `weight` / `model_aliases` / `proxy_url` / `headers` 等 |
| `GET /credentials` | 可带 `?name=` / `?auth_index=` / `?page=` / `?page_size=` | 分页时返回 `total` / `has_more` |

这两条「裸数组 / 裸布尔」是从官方 WebUI 的源码里核对出来的
（`services/api/apiKeys.ts`、`plugins.ts`），不是猜的。

## ⚠️ 用量队列是消费型的

- 上游 `GetUsageQueue` 内部是 `PopOldest(count)` —— **取出即删除**。
- 队列只保留约 **60 秒**，过期的记录**静默丢弃**（不报错）。
- `count` 缺省为 1；**非正整数返回 400**。
- ★ 上游优先把记录投递给**订阅者**：一旦有人订阅了队列通道，记录就不再进队列。

因此本项目：

1. 采集频率默认 **15 秒**（必须远小于 60 秒窗口），取到就立刻落库（幂等键去重，重复采集不会重复计数）；
2. **刻意不订阅**上游的队列通道 —— 否则会把用户的其它工具饿死；
3. 检测采集缺口并告警。

### 多个采集器共用一个 CPA = 数据静默丢失

这是官方文档里也明确警告过的：**多个采集器同时消费同一个 CPA 的队列，会互相抢记录**，
双方拿到的都是**不完整**的数据，而且**不会有任何报错**。

要么只跑一个采集器，要么让所有采集器都改用**订阅模式**。

## 能力边界（会直接影响面板功能）

| 能力 | 只有 v8 有 | 说明 |
| --- | --- | --- |
| 冷却重置 | ✅ | v0 连接下面板会明确返回「需要 v8」，而不是发一个必然 404 的请求 |
| 请求日志开关 | ✅ | |
| OAuth 排除模型 / 模型别名 | ✅ | |
| 主动配额查询 | ❌ | 走 v0 的 `/quota/*` |
| 按 Key 用量 | 两代都有 | v8 是 `/observability/usage/api-keys` |

面板在缺少能力时返回 **501/明确错误**，不会静默失败 —— 因为「以为成功了但没生效」
比「报错」危险得多。

## 上游没有的能力（所以面板自己实现）

- 历史与聚合（上游只给当前队列 + 当前快照）
- 状态判定（上游只给 `status_message` 文案）
- 备用池、熔断、删除上限、审计
- 副作用前的确认（上游的删除就是删除）
