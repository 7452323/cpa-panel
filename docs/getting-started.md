# 快速上手

## 前置条件

- **Python 3.9+**（无第三方依赖，不需要 `pip install`）
- 一个正在运行的 CLIProxyAPI 实例，**并且配置了管理密钥**（没有密钥时上游的管理 API 整体返回 404）
- 面板和被管理的 CPA 之间网络可达（通常是同一台机器上的 `127.0.0.1:8317`）

## 1. 初始化

```bash
cd cpa-panel
python3 -m cpapanel init \
  --node http://127.0.0.1:8317 \
  --node-key sk-你的管理密钥
```

它会：

- 生成 `panel.config.json`（服务参数，含节点引导信息）
- 创建 `data/panel.db`（账号、令牌、历史、审计都在这里）
- 创建管理员 `admin` 并**打印一次初始口令**

可选参数：`--username`（默认 `admin`）、`--password`、`--node-name`（默认 `default`）、
`--node-prefix`（`auto`|`v0`|`v8`，默认自动探测）、`--force`（覆盖已存在的配置）。

## 2. 启动

```bash
python3 -m cpapanel serve --host 127.0.0.1 --port 18317
```

打开 `http://127.0.0.1:18317`，用 `admin` + 初始口令登录（**登录后立刻改口令**）。

| 参数 | 说明 |
| --- | --- |
| `--host` / `--port` | 默认读配置（`127.0.0.1:18317`）；**默认只监听本机** |
| `--no-collector` | 不启动采集线程（打算用 cron 跑 `collect` 时） |
| `--no-inspector` | 不启动巡检线程（打算用 cron 跑 `inspect --apply` 时） |

也可以直接用启动器脚本（不依赖 `python -m`）：

```bash
./bin/cpapanel serve
```

## 3. 确认它工作正常

```bash
python3 -m cpapanel collect     # 采一轮用量（失败返回非 0）
python3 -m cpapanel inspect     # 出一份巡检计划（默认 dry-run，不动上游）
curl -s localhost:18317/api/health
```

`inspect` 的输出应该能看到每个凭证的判定与打算执行的动作。**确认计划合理之后**再考虑执行 ——
参见 [operations.md](operations.md#首次接入按顺序做别跳)。

## 系统服务（systemd）

```ini
# /etc/systemd/system/cpa-panel.service
[Unit]
Description=cpa-panel
After=network-online.target

[Service]
User=cpa
WorkingDirectory=/opt/cpa-panel
ExecStart=/usr/bin/python3 -m cpapanel serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now cpa-panel
journalctl -u cpa-panel -f
```

## Docker

```bash
cp .env.example .env      # 填 CPAPANEL_NODE_URL / CPAPANEL_NODE_KEY / 口令
docker compose up -d --build
```

数据与配置通过卷持久化（见 `docker-compose.yml`）。容器里监听的地址由 `CPAPANEL_HOST`（默认 `0.0.0.0`）决定，
对外暴露的端口映射在 compose 文件里。

## 放在反向代理后面

要让面板从外网访问，**必须**加一层 HTTPS 反向代理，并且**不要**把 `18317` 直接暴露出去。

Caddy（最省事，自动签证书）：

```
panel.example.com {
    reverse_proxy 127.0.0.1:18317
}
```

Nginx：

```nginx
location / {
    proxy_pass http://127.0.0.1:18317;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

然后在配置（或环境变量）里设置：

- `CPAPANEL_PUBLIC_URL=https://panel.example.com` —— 生成 OAuth 回跳地址时用
- `CPAPANEL_TRUST_PROXY_HEADERS=true` —— **只在可信反代后面开**，开了才信任 `X-Forwarded-For`

## 配置

两种方式，环境变量覆盖同名配置项（前缀 `CPAPANEL_`）：

| 位置 | 用途 |
| --- | --- |
| `panel.config.json` | 服务参数、巡检/采集策略、通知、熔断阈值 |
| 环境变量（`.env.example` 里有全集） | 容器/CI 里更顺手，且**不会把密钥写进文件**；同名项覆盖配置文件 |
| 面板「设置」页（面板配置表单） | 改上表里的白名单字段，**写回 `panel.config.json` 并热生效**（后台线程会重启） |
| 面板「设置」页（界面偏好） | 主题、每页条数、自动巡检开关，存在数据库里 |

环境变量覆盖配置文件里的同名项，配置文件又覆盖内置默认值。
危险开关（`delete_unauthorized` 之类）也在白名单里，但**默认值就是关**，改之前请先读一遍 [operations.md](operations.md#危险操作)。

## 忘了口令

```bash
python3 -m cpapanel password --username admin --password 新口令
```

## 常见错误

| 现象 | 原因 |
| --- | --- |
| `上游未启用 Management API（/v0 与 /v8 均返回 404…）` | 上游没配管理密钥（注意是 404 不是 401）；或地址写错了 |
| `管理密钥无效（401）` | 密钥不对 |
| 页面能开但数据一直不动 | 采集线程没跑（`--no-collector`）或上游队列本来就没数据 |
| 端口占用 | 换 `--port`；注意默认端口 `18317` 可能与其它面板相同 |

更多见 [operations.md](operations.md#故障处置)。
