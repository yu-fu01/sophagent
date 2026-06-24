FROM python:3.12-slim

RUN useradd -m -u 1000 app
WORKDIR /app

COPY pyproject.toml ./
COPY sophagent/ sophagent/
COPY web/ web/
RUN pip install --no-cache-dir .

# pre-create /data owned by app so the named volume inherits the ownership
RUN mkdir -p /data && chown app:app /data

USER app
ENV SOPHAGENT_DATA_DIR=/data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "sophagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
