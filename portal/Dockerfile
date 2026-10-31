FROM python:3.12-slim

LABEL org.opencontainers.image.title="Android Device Portal" \
      org.opencontainers.image.description="Self-hosted, sequential Android device-matrix testing on Windows" \
      org.opencontainers.image.url="https://blueflutex.gt.tc/" \
      org.opencontainers.image.source="https://github.com/vikasdocker/android-device-portal" \
      org.opencontainers.image.vendor="BlueFluteX" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="1.0.1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 180 --retries 10 -r requirements.txt

COPY app ./app
COPY web ./web

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
