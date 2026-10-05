# 开发

## 目录结构

```
cpapanel/
  cpa.py              ← 上游契约（路径表 / 探测 / 请求体形状 / 错误映射）
  models.py           ← 归一化 + 凭证状态判定（六态 + 证据强度）
  store.py            ← SQLite 数据访问
  collector.py        ← 采集线程
  inspector.py        ← 巡检线程（计划 → 熔断 → 执行）
  notify.py           ← 告警通道
  pricing.py          ← 价格表与费用估算
  security.py         ← 口令与令牌
  runtime.py          ← 组装与生命周期
  __main__.py         ← CLI
  web/server.py       ← HTTP 端点 + 鉴权
  web/static/         ← 内嵌单页 UI（无构建步骤）
tests/
  run_all.py          ← 统一入口（清缓存 + 跑全部用例）
  smoke_cli.py        ← CLI 端到端冒烟（进程内起 Mock CPA）
  check_docs.py       ← 文档与代码一致性核查
  mock_cpa.py         ← 上游 Mock（把上游的怪脾气都复刻进来）
  test_*.py           ← 单元与集成用例
```

## 跑测试

```bash
make check      # = make test + make smoke + make docs-check，提交前跑这一条
make test       # 151 个单元/集成用例
make smoke      # 37 项 CLI 冒烟
make docs-check # 文档与代码一致性（端点 / 上游路径 / 配置项）
```

`docs-check` 会扫 `README.md` / `CHANGELOG.md` / `docs/*.md` 里出现的
每一条 `/api/...`、`/vN/management/...` 与 `inspector.*` 这类配置项，
逐个到 `server.py` / `cpa.py` / `config.py` 里核对。
写文档时最容易犯的错不是文采，而是**写一个不存在的端点**——这个脚本专治它。

CI（`.github/workflows/tests.yml`）在 Python **3.9 和 3.13** 上各跑一遍，
失败时会把 `tests/_last_run.log` 传成构建产物。

## ⚠️ 本项目的环境陷阱：Python 是常驻解释器

本机（Scripting App 的 Shell）里 `python3` 是一个**常驻单进程解释器**：
两次 `python3 -c` 会得到**同一个 PID**，`sys.modules` 跨次保留。

后果：**你改了代码，跑起来还是旧逻辑** —— 而且看起来像「业务 bug」，非常浪费时间。

所以 `tests/run_all.py` 和 `tests/smoke_cli.py` 在导入 `cpapanel` **之前**都会：

1. 清掉 `cpapanel*` / `tests*` 的 `sys.modules` 缓存；
2. 删掉所有 `__pycache__`。

输出里的「清理 N 个模块缓存」就是这件事的证据。**任何新的测试入口都要照做**，
否则会出现成片假失败（历史上曾经从「58 通过」变成「122 通过」，全是这个原因）。

另外：**后台 python 任务会占住这个解释器**，新的 python 调用会排队直到超时 ——
调试时如果命令莫名超时，先看看是不是有别的 python 在后台跑。

## 加一个上游端点：三步

1. **`cpapanel/cpa.py`** — 往 `PATHS` 里加一条（v0/v8 两个前缀；不支持的那个写 `None`），
   再加一个薄方法。**请求体形状必须照抄上游真实行为**（见下）。
2. **`tests/mock_cpa.py`** — 加路由与 handler，**并且要真的改变 mock 的状态**，
   否则测试只能验证「调用没抛异常」。
3. **`tests/`** — 加断言。凡是「body 形状会影响结果」的端点，
   都要断言**线上格式**（读 `mock.calls()` 里的 body），不能只断言「返回 200」。

## Mock 的设计原则

`tests/mock_cpa.py` 不是「返回假数据的桩」，它的价值在于**复刻上游的怪脾气**：

| 复刻的行为 | 为什么重要 |
| --- | --- |
| 未配置管理密钥时 `/vN/management/*` 返回 **404**（不是 401） | 探测逻辑必须能从 404 里区分「前缀不对」和「没开管理 API」 |
| `count` 不是正整数时返回 **400** | 客户端不许把 0 发出去 |
| 用量队列是**取值即删**，且超过 60 秒的记录自动消失 | 「采一次就好了」的错误实现会在这里暴露 |
| `PUT /config/access/api-keys` 同时接受**裸数组**与包装体 | 只接受包装体会掩盖客户端的格式错误（会被当成「空列表」→ 清空全部 Key） |
| `DELETE /credentials` 同时支持 body `{"names":[...]}` 与 query `?name=` | 让回退逻辑可测 |
| 调用记录里保存 **raw body** | 才能断言线上格式，而不是只断言结果 |

**反面教材（真实踩过）**：Mock 早期用一个「宽容的 JSON 解析器」把非 dict 的 body 静默变成 `{}`，
结果客户端发错形状也测不出来 —— 在真实上游上那是「把下游 Key 全部清空」。
现在解析器分成 `_read_json`（只当对象）和 `_read_json_any`（任意 JSON），
「body 可能是数组/裸布尔」的端点必须用后者。

## 测试写法的几条纪律

- **每个测试自带初始化**，不要依赖别的测试类「顺手」把状态铺好。
  依赖执行顺序的测试在改个类名之后就会莫名其妙地红。
- **改动共享夹具时要恢复**（`addCleanup` 快照回写）。集成测试的 Mock 是模块级共享的。
- **别硬编码夹具里的具体值**（比如 `idx-3`），从对象上取。
  曾经因为断言写死 `idx-3` 而实际是 `idx-5`，白查一轮。
- **安全逻辑必须有测试**：熔断、证据闸门、危险操作的确认词 ——
  这些是「不做某事」的逻辑，没有测试就无法知道它们还在不在。

## 代码约定

- 注释与文档用中文，**解释「为什么」，不复述「是什么」**。
  例如「上游只接受 auth_index，所以本地找不到时要从快照里找一次」——
  这类信息在代码里留存下来，比写在 commit message 里有价值。
- 危险动作必须：显式确认 + 审计 + 可关闭的配置项。
- 不要引入第三方依赖。需要 JSON/YAML/HTTP 就用标准库。
- 前端不加构建步骤、不引外链 —— 面板要能在没有外网的内网里跑。
