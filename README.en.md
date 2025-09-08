# SearXNG OpenAPI Tool

A minimal HTTP service (FastAPI) that combines SearXNG search with web content extraction (Trafilatura + Readability) using an ensemble approach. Designed as a tool for OpenWebUI / OpenAI Tools, but works standalone as well.

- Author: Seweryn Sitarski, Kat (coding assistance)
- Version: 0.4.1
- License: MIT

## Features
- SearXNG search with pagination, time filter, and safesearch.
- Parallel fetching and extraction of content from multiple URLs.
- Two-step extraction: Trafilatura and Readability (selects the better result + safe fallbacks).
- Simple test interface at `/ui` and OpenAPI docs at `/docs`.
- Low latency: HTTP/2, `httpx` client, connection and timeout limits.

## Endpoints
- `GET /health`: Quick service status.
- `GET /about`: Tool metadata, version, usage examples, environment exposure.
- `GET /search`: SearXNG search. Params: `q`, `site`, `time_range=day|week|month|year`, `page`, `limit`, `language`, `safesearch=0|1|2|off|moderate|strict`.
- `GET /fetch`: Fetch and extract one or many URLs via repeated `url` parameters + `max_chars`, `concurrency`.
- `POST /fetch`: Fetch and extract via JSON body: `{ url: string|array, urls: array, max_chars, concurrency }`.
- `GET /ui`: Simple manual testing UI (excluded from OpenAPI schema).

Interactive docs: `http://localhost:7000/docs`
OpenAPI JSON: `http://localhost:7000/openapi.json`

## Environment variables (key ones)
- `SEARXNG_URL` (default `http://localhost:8080`): Base URL of the SearXNG instance, e.g., `https://your-searxng.example.org`.
- `TIMEOUT_S` (default `3.0`): HTTP upstream request timeout.
- `MAX_CONN` / `MAX_KEEP` (default `200` / `100`): HTTP client connection limits.
- `ACCEPT_LANGUAGE` (default `pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7`): Preferred language.
- `USER_AGENT` (default `Mozilla/5.0 (compatible; searxng-openapi-tool/...)`).
- `MIN_OUTPUT_CHARS` (default `200`): Minimum content “density” to favour an extractor.
- `HARD_MAX_CHARS` (default `40000`): Hard limit of characters in returned Markdown.
- `EXTRACT_TIMEOUT_S` (default `6.0`): Single extraction timeout.
- `SEARCH_SHOW_HINTS` (`0/1`, default `0`): Whether `/search` adds a batch `/fetch` hint.

The full list with current values is available at `GET /about`.

## Quick start (local – Python/uvicorn)
Requires Python 3.11.

1) Install system dependencies (Debian/Ubuntu):
```bash
sudo apt update && sudo apt install -y build-essential libxml2-dev libxslt1-dev
```
2) Install Python packages (virtualenv recommended):
```bash
pip install "fastapi>=0.110,<1.0" "uvicorn[standard]>=0.29,<1.0" \
           "httpx[http2]>=0.27,<1.0" trafilatura readability-lxml markdownify brotli
```
3) Set `SEARXNG_URL` to a working SearXNG instance:
```bash
export SEARXNG_URL="https://your-searxng.example.org"
```
4) Run the server:
```bash
uvicorn app:app --host 0.0.0.0 --port 7000
```
5) Check: `http://localhost:7000/health`, `http://localhost:7000/ui`, `http://localhost:7000/docs`.

Note: `/fetch` works independently of SearXNG, but `/search` requires a valid `SEARXNG_URL`.

## Run with Docker
A ready-to-use `Dockerfile` is included.

- Build locally:
```bash
docker build -t searxng-openapi-tool:latest .
```
- Run (example passing `SEARXNG_URL`):
```bash
docker run --rm -p 7000:7000 \
  -e SEARXNG_URL="https://your-searxng.example.org" \
  --name searxng-openapi-tool searxng-openapi-tool:latest
```

## Docker Compose
`docker-compose.yaml` is provided (port 7000, healthcheck, environment variables).

- Start in background and build:
```bash
docker compose up -d --build
```
- Adjust variables (in Compose or via `env_file`). At minimum set `SEARXNG_URL`.
- Logs: `docker compose logs -f searxng-tool`
- Stop: `docker compose down --remove-orphans`

## Deploy with systemd
The `deploy/` directory contains two systemd unit variants and a default environment file.

1) “docker run” variant (single container)
- Copy and adjust the default env file:
```bash
sudo install -m 0644 deploy/default/searxng-openapi-tool /etc/default/searxng-openapi-tool
sudo editor /etc/default/searxng-openapi-tool   # set SEARXNG_URL, optionally PORT/IMAGE/CONTAINER_NAME
```
- Install unit and start:
```bash
sudo install -m 0644 deploy/systemd/searxng-openapi-tool-docker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now searxng-openapi-tool-docker
```
- Logs: `journalctl -u searxng-openapi-tool-docker -f`
- Upgrade to a new image: change `IMAGE` in `/etc/default/...` or rebuild locally and restart the service.

2) “Docker Compose” variant (stack)
- Copy and edit the env file, set `COMPOSE_DIR` to the repo path on host and `SEARXNG_URL`:
```bash
sudo install -m 0644 deploy/default/searxng-openapi-tool /etc/default/searxng-openapi-tool
sudo editor /etc/default/searxng-openapi-tool   # set COMPOSE_DIR=/path/to/repo and SEARXNG_URL
```
- Install unit and start:
```bash
sudo install -m 0644 deploy/systemd/searxng-openapi-tool-container.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now searxng-openapi-tool-container
```
- The unit uses:
  - `ExecStart`/`ExecReload`: `docker compose up -d --build` in `COMPOSE_DIR`
  - `ExecStop`: `docker compose down --remove-orphans`
- Stack logs: `docker compose -f "$COMPOSE_DIR/docker-compose.yaml" logs -f`

## Usage examples (curl)
- Health: `curl -sS http://localhost:7000/health`
- About: `curl -sS http://localhost:7000/about`
- Search: `curl -sS "http://localhost:7000/search?q=openai&limit=3&safesearch=moderate"`
- Fetch (single, GET): `curl -sS "http://localhost:7000/fetch?url=https://example.com&max_chars=2000"`
- Fetch (multiple, POST):
```bash
curl -sS -H "Content-Type: application/json" \
     -d '{"urls":["https://example.com","https://httpbin.org/json"],"max_chars":1500,"concurrency":4}' \
     http://localhost:7000/fetch
```
- Fetch (multiple, GET – repeated `url`):
```bash
curl -sS "http://localhost:7000/fetch?url=https://example.com&url=https://httpbin.org/json&max_chars=1500&concurrency=4"
```

## Notes and best practices
- Do not expose publicly without basic protections (rate limiting, reverse proxy, ACL, TLS).
- Set a valid `SEARXNG_URL` — otherwise `/search` will report an upstream error.
- The HTTP client sets `User-Agent` and `Accept-Language`; adjust them to your org policies.

## Info
- Repository contains: `app.py`, `Dockerfile`, `docker-compose.yaml`, `deploy/` (systemd units and defaults).
- Author/contact: `seweryn.sitarski@gmail.com`
- License: MIT

