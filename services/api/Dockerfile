FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system gearia && useradd --system --gid gearia --home-dir /app gearia

COPY --chown=gearia:gearia app ./app
COPY alembic.ini .
COPY migrations ./migrations
RUN chown -R gearia:gearia /app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

USER gearia

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

FROM runtime AS test

USER root
COPY --chown=gearia:gearia tests ./tests
RUN chown -R gearia:gearia /app
USER gearia
