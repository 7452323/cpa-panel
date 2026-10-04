# cpa-panel 常用命令
# 注意：Python 命令统一用 python3，项目零第三方依赖，所以没有 install 目标。

PY      ?= python3
CONFIG  ?= panel.config.json
HOST    ?= 127.0.0.1
PORT    ?= 18317

.PHONY: help init serve collect inspect apply token test smoke check prune docker clean

help:            ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

init:            ## 初始化配置与管理员（需 NODE / NODE_KEY，见 README）
	$(PY) -m cpapanel --config $(CONFIG) init --node $(NODE) --node-key $(NODE_KEY)

serve:           ## 启动面板（Web + 采集 + 巡检）
	$(PY) -m cpapanel --config $(CONFIG) serve --host $(HOST) --port $(PORT)

serve-local:     ## 只在本机跑，且不启后台线程（纯看 UI 时用）
	$(PY) -m cpapanel --config $(CONFIG) serve --no-collector --no-inspector

collect:         ## 手动采集一次（失败返回非 0，可挂 cron）
	$(PY) -m cpapanel --config $(CONFIG) collect

inspect:         ## 巡检一次（dry-run，只出计划）
	$(PY) -m cpapanel --config $(CONFIG) inspect

apply:           ## 巡检并真的执行维护动作（危险，先跑 inspect 确认）
	$(PY) -m cpapanel --config $(CONFIG) inspect --apply

token:           ## 创建一个面板 API 令牌（给脚本/CI）
	$(PY) -m cpapanel --config $(CONFIG) token --name $(NAME)

test:            ## 跑全部单元 + 集成测试
	$(PY) tests/run_all.py

smoke:           ## CLI 端到端冒烟（对着 Mock CPA 演练 init→collect→inspect→apply）
	$(PY) tests/smoke_cli.py

check: test smoke ## 提交前跑这一条就够了

prune:           ## 清理过期数据（用量明细保留 180 天）
	$(PY) -m cpapanel --config $(CONFIG) prune

docker:          ## 构建并启动容器
	docker compose up -d --build

clean:           ## 清掉字节码与测试产物（不动数据与配置）
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -f tests/_last_run.log
