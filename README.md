# SearXNG OpenAPI Tool

Minimalny serwis HTTP (FastAPI) łączący wyszukiwanie SearXNG z ekstrakcją treści stron (Trafilatura + Readability) w podejściu „ensemble”. Zaprojektowany jako narzędzie dla OpenWebUI / OpenAI Tools, ale działa też samodzielnie.

- Autor: Seweryn Sitarski, Kat (asysta kodowa)
- Wersja: 0.8.0
- Licencja: MIT

## Funkcje
- Wyszukiwanie w SearXNG z paginacją, filtrem czasu i „safesearch”.
- Batch search: powtarzalne `q` pozwala wykonać wiele zapytań równolegle w ramach jednego wywołania.
- Równoległe pobieranie i ekstrakcja treści wielu adresów URL.
- Dwustopniowa ekstrakcja: Trafilatura i Readability (wybór lepszego wyniku + bezpieczne fallbacki).
- Prosty interfejs testowy pod `/ui` oraz dokumentacja OpenAPI pod `/docs`.
- Niskie opóźnienia: HTTP/2, klient `httpx`, limity połączeń i czasu.
- Rotacja User-Agent: każde żądanie używa losowego, realistycznego nagłówka przeglądarki (desktop/mobile), co pomaga ograniczać blokady po stronie serwisów.

## Endpointy
- `GET /health`: Szybki status usługi.
- `GET /about`: Metadane narzędzia, wersja, przykłady użycia, ekspozycja zmiennych środowiska.
- `GET /search`: Wyszukiwanie z SearXNG. Parametry: powtarzalne `q` (min. 2 znaki, wspiera listy JSON), `site`, `time_range=day|week|month|year`, `page`, `limit` (limit per zapytanie), `language`, `safesearch=0|1|2|off|moderate|strict`.
- `GET /fetch`: Pobieranie i ekstrakcja jednego lub wielu URL-i przez parametry `url` (można powtórzyć wiele razy) + `max_chars`, `concurrency`.
- `POST /fetch`: Pobieranie i ekstrakcja przez body JSON: `{ url: string|array, urls: array, max_chars, concurrency }`.
- `GET /ui`: Proste UI do ręcznego testowania (poza schematem OpenAPI).

Dokumentacja interaktywna: `http://localhost:7000/docs`
OpenAPI JSON: `http://localhost:7000/openapi.json`

## Zmiennie środowiskowe (najważniejsze)
- `SEARXNG_URL` (domyślnie `http://localhost:8080`): Bazowy URL instancji SearXNG, np. `https://twoj-searxng.example.org`.
- `TIMEOUT_S` (domyślnie `3.0`): Limit czasu zapytań HTTP do upstreamów.
- `MAX_CONN` / `MAX_KEEP` (domyślnie `200` / `100`): Limity połączeń dla klienta HTTP.
- `ACCEPT_LANGUAGE` (domyślnie `pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7`): Preferencja języka.
- Uwaga dot. UA: narzędzie losuje nagłówki User-Agent per żądanie z puli realistycznych UA; brak zmiennej środowiskowej do wymuszania stałego UA.
- `MIN_OUTPUT_CHARS` (domyślnie `200`): Minimalna „gęstość” treści do preferowania ekstraktora.
- `HARD_MAX_CHARS` (domyślnie `40000`): Twardy limit znaków w zwracanym Markdown.
- `EXTRACT_TIMEOUT_S` (domyślnie `6.0`): Timeout pojedynczej ekstrakcji.
- `SEARCH_SHOW_HINTS` (`0/1`, domyślnie `0`): Czy dodać w `/search` podpowiedź do masowego `/fetch`.
- `INCLUDE_EXCERPT` (`0/1`, domyślnie `0`): Włącza globalnie zwracanie pola `excerpt` (skrót treści) w odpowiedziach `/fetch`.

Pełna lista wraz z bieżącymi wartościami jest dostępna pod `GET /about`.

## Szybki start (lokalnie – Python/uvicorn)
Wymagany Python 3.11.

1. Zainstaluj zależności systemowe (Debian/Ubuntu):
   ```bash
   sudo apt update && sudo apt install -y build-essential libxml2-dev libxslt1-dev
   ```
2. Zainstaluj biblioteki Pythona (wirtualne środowisko zalecane):
   ```bash
   pip install "fastapi>=0.110,<1.0" "uvicorn[standard]>=0.29,<1.0" \
              "httpx[http2]>=0.27,<1.0" trafilatura readability-lxml markdownify brotli
   ```
3. Ustaw `SEARXNG_URL` na działającą instancję SearXNG:
   ```bash
   export SEARXNG_URL="https://twoj-searxng.example.org"
   ```
4. Uruchom serwer:
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 7000
   ```
5. Sprawdź: `http://localhost:7000/health`, `http://localhost:7000/ui`, `http://localhost:7000/docs`.

Uwaga: Endpointy `/fetch` działają niezależnie od SearXNG, ale `/search` wymaga poprawnego `SEARXNG_URL`.

## Uruchomienie w Dockerze
W repo znajduje się gotowy `Dockerfile`.

- Budowanie lokalne:
  ```bash
  docker build -t searxng-openapi-tool:latest .
  ```
- Uruchomienie (przykład z podaniem `SEARXNG_URL`):
  ```bash
  docker run --rm -p 7000:7000 \
    -e SEARXNG_URL="https://twoj-searxng.example.org" \
    --name searxng-openapi-tool searxng-openapi-tool:latest
  ```

## Docker Compose
W repo jest `docker-compose.yaml` z przykładową konfiguracją (port 7000, healthcheck, zmienne środowiskowe).

- Start w tle z budowaniem obrazu:
  ```bash
  docker compose up -d --build
  ```
- Podmiana zmiennych (np. w pliku Compose lub przez `env_file`). Minimalnie ustaw `SEARXNG_URL`.
- Logi: `docker compose logs -f searxng-tool`
- Zatrzymanie: `docker compose down --remove-orphans`

## Wdrożenie przez systemd
Katalog `deploy/` zawiera dwa warianty jednostek systemd oraz plik środowiskowy.

1) Wariant „docker run” (pojedynczy kontener)
- Skopiuj domyślny plik środowiskowy i dostosuj:
  ```bash
  sudo install -m 0644 deploy/default/searxng-openapi-tool /etc/default/searxng-openapi-tool
  sudo editor /etc/default/searxng-openapi-tool   # ustaw SEARXNG_URL, opcjonalnie PORT/IMAGE/CONTAINER_NAME
  ```
- Zainstaluj jednostkę i uruchom:
  ```bash
  sudo install -m 0644 deploy/systemd/searxng-openapi-tool-docker.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now searxng-openapi-tool-docker
  ```
- Logi: `journalctl -u searxng-openapi-tool-docker -f`
- Aktualizacja do nowego obrazu: zmień `IMAGE` w `/etc/default/...` lub przebuduj lokalnie i zrestartuj usługę.

2) Wariant „Docker Compose” (stack)
- Skopiuj i edytuj plik środowiskowy, ustawiając `COMPOSE_DIR` na katalog repo na hoście oraz `SEARXNG_URL`:
  ```bash
  sudo install -m 0644 deploy/default/searxng-openapi-tool /etc/default/searxng-openapi-tool
  sudo editor /etc/default/searxng-openapi-tool   # ustaw COMPOSE_DIR=/ścieżka/do/repo oraz SEARXNG_URL
  ```
- Zainstaluj jednostkę i uruchom:
  ```bash
  sudo install -m 0644 deploy/systemd/searxng-openapi-tool-container.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now searxng-openapi-tool-container
  ```
- Jednostka używa:
  - `ExecStart`/`ExecReload`: `docker compose up -d --build` w katalogu `COMPOSE_DIR`
  - `ExecStop`: `docker compose down --remove-orphans`
- Logi stacka: `docker compose -f "$COMPOSE_DIR/docker-compose.yaml" logs -f`

## Przykłady użycia (curl)
- Health: `curl -sS http://localhost:7000/health`
- About: `curl -sS http://localhost:7000/about`
- Search: `curl -sS "http://localhost:7000/search?q=openai&limit=3&safesearch=moderate"`
- Fetch (pojedynczy, GET): `curl -sS "http://localhost:7000/fetch?url=https://example.com&max_chars=2000"`
- Fetch (wiele, POST):
  ```bash
  curl -sS -H "Content-Type: application/json" \
       -d '{"urls":["https://example.com","https://httpbin.org/json"],"max_chars":1500,"concurrency":4}' \
       http://localhost:7000/fetch
  ```
- Fetch (wiele, GET – powtarzane `url`):
  ```bash
  curl -sS "http://localhost:7000/fetch?url=https://example.com&url=https://httpbin.org/json&max_chars=1500&concurrency=4"
  ```

## Uwagi i dobre praktyki
- Nie wystawiaj publicznie bez podstawowych zabezpieczeń (rate limiting, reverse proxy, ACL, TLS).
- Ustaw prawidłowy `SEARXNG_URL` – bez tego `/search` zwróci błąd upstream.
- Klient HTTP używa losowego `User-Agent` i nagłówka `Accept-Language`; dostosuj `ACCEPT_LANGUAGE` do polityk Twojej organizacji.
- Skrót `excerpt` w `/fetch` jest wyłączony domyślnie; włącz `INCLUDE_EXCERPT=1` tylko gdy faktycznie potrzebujesz streszczeń (oszczędza to kontekst modeli LLM).

## Informacje
- Repozytorium zawiera: `app.py`, `Dockerfile`, `docker-compose.yaml`, `deploy/` (jednostki systemd i domyślne środowisko).
- Autor/kontakt: `seweryn.sitarski@gmail.com`
- Licencja: MIT
