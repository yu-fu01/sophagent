FROM python:3.12-slim

RUN useradd -m -u 1000 app \
 && useradd -u 1001 -M -s /usr/sbin/nologin sandbox

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY sophagent/ sophagent/
COPY web/ web/
RUN pip install --no-cache-dir .

# /data 由 root 持有；启动时 _harden_data_dir 收紧到 0711 + 密钥 0600，
# workspace 由 workspace_for chown 给 sandbox。
RUN mkdir -p /data

# 以 root 起动：主进程持有密钥，exec 子进程经 user= 降权到无权读 /data 的 sandbox。
ENV SOPHAGENT_DATA_DIR=/data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "sophagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
