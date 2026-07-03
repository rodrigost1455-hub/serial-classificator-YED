FROM python:3.12-slim

WORKDIR /srv

# Backend service code + its own deps.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/app backend/app

# Static dashboard the backend serves at "/" (see app/main.py:dashboard_index).
COPY index.html support.js ./

ENV VA_DATA_DIR=/data
ENV VA_FRONTEND_DIR=/srv
VOLUME ["/data"]

EXPOSE 8000
WORKDIR /srv/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
