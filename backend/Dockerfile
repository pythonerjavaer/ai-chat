FROM node:22-alpine AS frontend-build

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/index.html frontend/vite.config.js ./
COPY frontend/public ./public
COPY frontend/src ./src
RUN npm run build


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt \
    && useradd --create-home --uid 10001 frostfire \
    && mkdir -p /app/backend/data \
    && chown -R frostfire:frostfire /app

COPY --chown=frostfire:frostfire backend /app/backend
COPY --from=frontend-build --chown=frostfire:frostfire /build/frontend/dist /app/frontend/dist

USER frostfire
EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
