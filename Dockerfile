FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BARKEEP_SQL_DIR=/app/sql

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY sql ./sql
COPY scripts ./scripts

RUN pip install --no-cache-dir '.[loader]'

RUN useradd --system --uid 10001 --create-home barkeep
USER barkeep

CMD ["python", "-m", "barkeep.ingest"]
