# cpa-panel 镜像
#
# 说明：只依赖 python:3.12-slim，没有任何 pip install（项目零第三方依赖）。
# 上游 CPA 通常在宿主机或另一个容器里监听 127.0.0.1:8317，
# 所以这里用 host 网络在 Linux 上最省事；容器网络下请把节点填成可达地址。

FROM python:3.12-slim

LABEL org.opencontainers.image.title="cpa-panel" \
      org.opencontainers.image.description="Self-hosted account management panel for CLIProxyAPI (CPA)" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CPAPANEL_DATA_DIR=/data \
    CPAPANEL_DATABASE=/data/panel.db \
    CPAPANEL_HOST=0.0.0.0 \
    CPAPANEL_PORT=18317

WORKDIR /app
COPY cpapanel /app/cpapanel
COPY bin /app/bin
COPY docs /app/docs
COPY tests /app/tests
COPY README.md LICENSE /app/

# 以非 root 运行；/data 用于持久化数据库与日志
RUN useradd --create-home --uid 10001 cpa \
 && mkdir -p /data \
 && chown -R cpa:cpa /app /data

USER cpa
VOLUME ["/data"]
EXPOSE 18317

# 健康检查直接打面板自己的公开端点（不需要认证）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; \
url='http://127.0.0.1:'+os.environ.get('CPAPANEL_PORT','18317')+'/api/health'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

ENTRYPOINT ["python3", "-m", "cpapanel"]
CMD ["serve"]
