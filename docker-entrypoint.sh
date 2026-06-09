#!/bin/sh
# One image, two roles. Railway sets SERVICE_ROLE=poller on the poller service;
# the web service (default) runs migrations + seeds before serving.
set -e

if [ "$SERVICE_ROLE" = "poller" ]; then
    exec python -m isbe_notifier.poller
fi

alembic upgrade head
python -m isbe_notifier.seeds
exec uvicorn isbe_notifier.web.app:app --host 0.0.0.0 --port "${PORT:-8000}"
