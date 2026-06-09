FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project > requirements.txt \
    && uv pip install --system -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv pip install --system --no-deps .

EXPOSE 8000

# Default command is the web app; the poller service overrides with
# `python -m isbe_notifier.poller` (see docker-compose.yaml / Railway settings).
CMD ["uvicorn", "isbe_notifier.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
