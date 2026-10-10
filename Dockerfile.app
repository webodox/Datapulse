# DataPulse in one container: the dashboard and the API on one port, data on a volume at /data.
# For cloud hosts (Railway, Fly.io, Render...). Local development can keep using docker-compose.yml.
FROM node:24-alpine AS web
WORKDIR /web
COPY web/package.json ./
RUN npm install
COPY web ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATAPULSE_HOME=/data DATAPULSE_WEB_DIR=/app/web
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY server ./server
COPY examples ./examples
RUN pip install --no-cache-dir '.[web,sources]'
COPY --from=web /web/dist ./web
EXPOSE 8000
# Hosts set $PORT; they also terminate HTTPS in front of the app, hence --proxy-headers.
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
