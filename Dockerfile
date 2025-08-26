FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py .
RUN pip install --no-cache-dir \
    "fastapi>=0.110,<1.0" "uvicorn[standard]>=0.29,<1.0" \
    "httpx[http2]>=0.27,<1.0" trafilatura readability-lxml markdownify brotli
EXPOSE 7000
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","7000"]

