FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    postgresql-client \
    age \
    rclone \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

COPY requirements.lock pyproject.toml README.md ./
RUN pip install --upgrade pip \
    && pip install -r requirements.lock \
    && pip install --no-deps . \
    && pip install gunicorn==23.0.0

COPY . .
RUN chmod +x /app/ops/backup/*.sh \
    && chown -R appuser:appuser /app \
    && mkdir -p /backups \
    && chown appuser:appuser /backups

USER appuser

EXPOSE 8000

CMD ["gunicorn", "app.web.app:app", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
