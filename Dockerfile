FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system xerrameca \
    && useradd --system --gid xerrameca --home-dir /nonexistent --shell /usr/sbin/nologin xerrameca \
    && mkdir -p /var/lib/xerrameca \
    && chown xerrameca:xerrameca /var/lib/xerrameca

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER xerrameca
EXPOSE 8791
VOLUME ["/var/lib/xerrameca"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/health', timeout=2).read()"

CMD ["uvicorn", "xerrameca.app:app", "--host", "0.0.0.0", "--port", "8791", "--workers", "1"]
