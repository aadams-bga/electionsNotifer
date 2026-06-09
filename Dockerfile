FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project > requirements.txt \
    && uv pip install --system -r requirements.txt

COPY alembic.ini docker-entrypoint.sh ./
COPY migrations ./migrations
COPY src ./src
RUN uv pip install --system --no-deps . && chmod +x docker-entrypoint.sh

EXPOSE 8000

# SERVICE_ROLE=poller runs the poller; default runs migrations + web app.
CMD ["./docker-entrypoint.sh"]
