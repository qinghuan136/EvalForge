FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml requirements.lock ./
COPY third_party ./third_party
COPY evalforge ./evalforge
RUN pip install --no-cache-dir --constraint requirements.lock .

USER 10001:10001
CMD ["python", "-m", "uvicorn", "evalforge.server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets-sansio", "--ws-max-size", "1048576", "--ws-ping-interval", "20", "--ws-ping-timeout", "20"]
