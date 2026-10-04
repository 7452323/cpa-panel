"""文档与代码一致性核查。

用途：文档里出现过的每一个 API 路径 / 上游路径 / 配置项，都要在代码里找得到。
宁可少写，不能写错 —— 这个脚本就是那句话的执行者。

跑法：python3 tests/check_docs.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        return handle.read()


def main():
    docs = {}
    for name in ("README.md", "CHANGELOG.md"):
        docs[name] = read(name)
    docs_dir = os.path.join(ROOT, "docs")
    for name in sorted(os.listdir(docs_dir)):
        if name.endswith(".md"):
            docs["docs/" + name] = read(os.path.join("docs", name))

    server = read("cpapanel/web/server.py")
    cpa = read("cpapanel/cpa.py")
    config = read("cpapanel/config.py")

    # 1) 面板端点：代码里的真实路由
    routes = re.findall(r"@route\(\"([A-Z]+)\",\s*r\"([^\"]+)\"\)", server)

    def route_exists(path):
        for _method, pattern in routes:
            # 文档里写 {id}，代码里是 (?P<id>\d+)，统一成一段通配再比
            normalized = re.sub(r"\(\?P<[^>]+>[^)]+\)", "*", pattern).rstrip("/")
            if re.fullmatch(normalized.replace("*", "[^/]+"), path.rstrip("/")):
                return True
        return False

    # 2) 上游路径：代码里的路径表
    upstream = set(re.findall(r"\"(/[A-Za-z0-9._/-]+)\"", cpa))

    # 3) 配置项：默认值里的点号路径 + 白名单里的键
    config_keys = set()
    for block in re.findall(r"\"(\w+)\":\s*\{([^{}]*)\}", config, re.S):
        section, body = block
        for key in re.findall(r"\"(\w+)\":", body):
            config_keys.add(f"{section}.{key}")

    problems = []

    for doc_name, text in docs.items():
        # 面板 API 路径
        for path in sorted(set(re.findall(r"/api/[A-Za-z0-9._/{}-]+", text))):
            cleaned = path.rstrip(".,)`").rstrip("/")
            if "{" in cleaned or "}" in cleaned:
                continue          # 带占位符的写法交给路由正则匹配
            if cleaned in ("/api",):
                continue
            # 上游源码的路径（如 services/api/apiKeys.ts）不是面板端点，别误报
            if ".ts" in cleaned or ".js" in cleaned or ".py" in cleaned:
                continue
            if not route_exists(cleaned):
                problems.append(f"{doc_name}: 面板路径 {cleaned} 在 server.py 里找不到")

        # 上游管理路径
        for path in sorted(set(re.findall(r"/v[08]/management/[A-Za-z0-9._/-]+", text))):
            tail = path.split("/management", 1)[1]
            if tail and tail not in upstream and tail.rstrip("/") not in upstream:
                problems.append(f"{doc_name}: 上游路径 {path} 不在 cpa.py 的路径表里")

        # 配置项（排除 collector.py / inspector.py 这类文件名）
        for key in sorted(set(re.findall(r"\b((?:inspector|collector|notify|admin)\.[a-z_]{2,})\b", text))):
            if key.endswith((".py", ".js", ".ts", ".md")):
                continue
            if key not in config_keys and key not in server:
                problems.append(f"{doc_name}: 配置项 {key} 在 config.py / server.py 里找不到")

    # 4) 前端：`app.js` 里写死的 `/api/...` 必须真的存在
    #    （文档写错端点只是难查，前端写错端点是直接给用户看 404）
    app_js = os.path.join(ROOT, "cpapanel/web/static/app.js")
    if os.path.exists(app_js):
        with open(app_js, "r", encoding="utf-8") as handle:
            js = handle.read()
        for path in sorted(set(re.findall(r"[\"'`](/api/[A-Za-z0-9._/-]+)", js))):
            cleaned = path.rstrip("/")
            if cleaned in ("/api",):
                continue
            if not route_exists(cleaned):
                problems.append(f"cpapanel/web/static/app.js: 调用了不存在的端点 {cleaned}")

    print(f"文档 {len(docs)} 份；代码路由 {len(routes)} 个；上游路径 {len(upstream)} 个；配置项 {len(config_keys)} 个")
    if problems:
        print(f"\n发现 {len(problems)} 处对不上：")
        for item in problems:
            print("  ✗", item)
        return 1
    print("\n全部一致 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
