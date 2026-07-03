FROM python:3.12-slim

WORKDIR /srv

# Backend service code + its own deps.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/app backend/app

# Static dashboard the backend serves at "/" (see app/main.py:dashboard_index).
COPY index.html support.js ./

# Static reference dataset (Ford_Real ground truth) — baked into the image since it's
# not runtime-mutated. Lands at /srv/CONSOLIDADO_CON_FORD.csv, which is where
# config.CONSOLIDADO_FORD_PATH looks by default (repo-root-relative == /srv here).
COPY CONSOLIDADO_CON_FORD.csv ./

ENV VA_DATA_DIR=/data
ENV VA_FRONTEND_DIR=/srv
# No VOLUME instruction — Railway rejects it (dockerfile invalid: docker VOLUME is not
# supported, use Railway Volumes). /data still works as a plain directory; attach a
# Railway Volume at /data in the dashboard if you want it to persist across deploys.

EXPOSE 8000
WORKDIR /srv/backend
# $PORT is injected by PaaS platforms (Railway, etc); falls back to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
