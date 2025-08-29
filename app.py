# app.py
"""
SearXNG OpenAPI Tool

- Autor: Seweryn Sitarski, Kat (asysta kodowa)
- Kontakt: seweryn.sitarski@gmail.com
- Wersja: 0.4.1
- Licencja: MIT
- URL projektu: https://example.local/searxng-openapi-tool

Opis:
Minimalny tool HTTP dla OpenWebUI / OpenAI Tools, łączący wyszukiwanie SearXNG
oraz ekstrakcję treści (Trafilatura + Readability) w układzie ensemble.

Dobre praktyki:
- __about__ z metadanymi, /about endpoint, X-* nagłówki sygnujące wersję i źródło
- twardy CAP znaków (HARD_MAX_CHARS) + miękki cap parametru
- czytelne modele Pydantic + wersjonowanie API
- stabilne warunki (is None) zamiast truthiness na obiektach Trafilatury
- spójny User-Agent i Accept-Language
- jawne etykiety autora, licencji i wersji
"""
from fastapi import FastAPI, Query, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
try:
    # Pydantic v2
    from pydantic import ConfigDict
except Exception:  # pragma: no cover
    ConfigDict = dict  # type: ignore
from typing import List, Optional, Tuple, Dict
from typing import Union
import asyncio
import httpx
import json
import ast
import os
import re

import trafilatura
from trafilatura.settings import use_config

# Readability (python-readability / readability-lxml)
from readability.readability import Document
from markdownify import markdownify as md

__title__ = "searxng-openapi-tool"
__version__ = "0.4.1"
__author__ = "Seweryn Sitarski, Kat"
__license__ = "MIT"
__contact__ = "seweryn.sitarski@gmail.com"
__url__ = "https://example.local/searxng-openapi-tool"

app = FastAPI(
    title=__title__,
    version=__version__,
    contact={"name": __author__, "email": __contact__}
)

# --- Konfiguracja środowiska ---
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")
TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "3.0"))
MAX_CONN = int(os.environ.get("MAX_CONN", "200"))
MAX_KEEP = int(os.environ.get("MAX_KEEP", "100"))
USER_AGENT = os.environ.get(
    "USER_AGENT",
    f"Mozilla/5.0 (compatible; {__title__}/{__version__})"
)
ACCEPT_LANG = os.environ.get("ACCEPT_LANGUAGE", "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7")
MIN_OUTPUT_CHARS = int(os.environ.get("MIN_OUTPUT_CHARS", "200"))
HARD_MAX_CHARS = int(os.environ.get("HARD_MAX_CHARS", "40000"))
EXTRACT_TIMEOUT_S = float(os.environ.get("EXTRACT_TIMEOUT_S", "6.0"))

client: Optional[httpx.AsyncClient] = None

# --- Modele ---
class WebItem(BaseModel):
    title: str
    url: str
    snippet: str

class WebResult(BaseModel):
    query: str
    items: List[WebItem]
    next_page: Optional[int] = None
    batch_fetch_hint_get: Optional[str] = None

class FetchResult(BaseModel):
    url: str
    title: Optional[str]
    content_markdown: str
    excerpt: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    content_type: Optional[str] = None
    source: str

class About(BaseModel):
    name: str
    version: str
    author: str
    license: str
    homepage: str
    contact: str
    endpoints: List[str]
    env: Dict[str, str]
    openapi_url: str
    docs_url: str
    redoc_url: Optional[str]
    examples: Dict[str, str]

class FetchRequest(BaseModel):
    # Hybrydowy request: jedno pole 'url' (string lub lista) i/lub 'urls' (lista)
    model_config = ConfigDict(populate_by_name=True)
    url: Optional[Union[str, List[str]]] = Field(default=None, alias="url")
    urls: Optional[List[str]] = None
    max_chars: int = 8000
    concurrency: Optional[int] = None

class FetchResponse(BaseModel):
    results: List[FetchResult]
    errors: Dict[str, str]

# --- Trafilatura config ---
TRA_CFG = use_config()
TRA_CFG.set("DEFAULT", "MIN_OUTPUT_SIZE", "200")
TRA_CFG.set("DEFAULT", "EXTRACTION_TIMEOUT", "6")
TRA_CFG.set("DEFAULT", "EXTRACTION_TECHNIQUE", "fast")
TRA_CFG.set("DEFAULT", "include_tables", "no")

# --- Lifecycle ---
@app.on_event("startup")
async def _startup():
    global client
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT_S),
        limits=httpx.Limits(max_connections=MAX_CONN, max_keepalive_connections=MAX_KEEP),
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Language": ACCEPT_LANG,
            "Accept-Encoding": "gzip, deflate, br",
        },
        follow_redirects=True,
        http2=True,
    )

@app.on_event("shutdown")
async def _shutdown():
    global client
    if client:
        await client.aclose()
        client = None

# --- Helpery ---
_nonspace_re = re.compile(r"\S")

def searx_search_url(base: str) -> str:
    return base.rstrip("/") + "/search"

def score_markdown(text: str) -> int:
    return len(_nonspace_re.findall(text or ""))

def safe_author(author_field):
    if not author_field:
        return None
    if isinstance(author_field, str):
        return author_field.strip()
    if isinstance(author_field, (list, tuple, set)):
        return ", ".join([str(x).strip() for x in author_field if x])
    return str(author_field)

# --- Ekstraktory ---
def trafilatura_extract(html_bytes: bytes, url: str) -> Tuple[str, Dict[str, Optional[str]]]:
    downloaded = None
    try:
        downloaded = trafilatura.load_html(html_bytes)
    except Exception:
        try:
            downloaded = trafilatura.load_html(html_bytes.decode("utf-8", errors="ignore"))
        except Exception:
            downloaded = None
    if downloaded is None:
        return "", {"title": None, "author": None, "date": None}

    title = author = date = None
    try:
        meta = trafilatura.extract_metadata(downloaded)
        title = meta.title if meta and getattr(meta, "title", None) else None
        author = safe_author(getattr(meta, "author", None))
        date = meta.date if meta and getattr(meta, "date", None) else None
    except Exception:
        pass

    content = None
    try:
        content = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=True,
            output="markdown",
            config=TRA_CFG,
            url=url,
        )
    except Exception:
        content = None

    if not content:
        try:
            content = trafilatura.extract(downloaded, output="markdown", config=TRA_CFG, url=url)
        except Exception:
            pass

    return (content or "").strip(), {"title": title, "author": author, "date": date}


def readability_extract(html_bytes: bytes) -> Tuple[str, Dict[str, Optional[str]]]:
    try:
        html = html_bytes.decode("utf-8", errors="ignore")
    except Exception:
        html = ""

    title = None
    md_text = ""
    try:
        doc = Document(html)
        title = doc.short_title()
        summary_html = doc.summary(html_partial=True)
        md_text = md(summary_html or "", strip=["script", "style"]) or ""
    except Exception:
        md_text = ""

    return md_text.strip(), {"title": title, "author": None, "date": None}

# --- Endpoints ---
@app.get("/about", response_model=About)
async def about():
    return About(
        name=__title__,
        version=__version__,
        author=__author__,
        license=__license__,
        homepage=__url__,
        contact=__contact__,
        endpoints=["/health", "/search", "/fetch", "/about", "/ui"],
        env={
            "SEARXNG_URL": SEARXNG_URL,
            "TIMEOUT_S": str(TIMEOUT_S),
            "MAX_CONN": str(MAX_CONN),
            "MAX_KEEP": str(MAX_KEEP),
            "HARD_MAX_CHARS": str(HARD_MAX_CHARS),
            "MIN_OUTPUT_CHARS": str(MIN_OUTPUT_CHARS),
            "EXTRACT_TIMEOUT_S": str(EXTRACT_TIMEOUT_S),
            "ACCEPT_LANGUAGE": ACCEPT_LANG,
            "USER_AGENT": USER_AGENT,
        },
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        examples={
            "health": 'curl -sS http://localhost:7000/health',
            "about": 'curl -sS http://localhost:7000/about',
            "search": 'curl -sS "http://localhost:7000/search?q=openai&limit=3&safesearch=moderate"',
            "fetch_single_get": 'curl -sS "http://localhost:7000/fetch?url=https://example.com&max_chars=2000"',
            "fetch_multi_post": 'curl -sS -H "Content-Type: application/json" -d \'{"urls":["https://example.com","https://httpbin.org/json"],"max_chars":1500,"concurrency":4}\' http://localhost:7000/fetch',
            "fetch_multi_get": 'curl -sS "http://localhost:7000/fetch?url=https://example.com&url=https://httpbin.org/json&max_chars=1500&concurrency=4"',
        },
    )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/search", response_model=WebResult)
async def search(
    q: str = Query(..., min_length=2, description="Zapytanie wyszukiwania, min 2 znaki"),
    site: Optional[str] = Query(None, description="Ogranicz do domeny, np. example.com (wartości 'null'/'none'/'undefined' traktowane jak puste)"),
    time_range: Optional[str] = Query(
        None,
        description="Filtr czasu: day|week|month|year",
    ),
    page: int = Query(1, ge=1, description="Numer strony (>=1)"),
    limit: int = Query(5, ge=1, le=20, description="Maks. liczba wyników (1-20)"),
    language: Optional[str] = Query(None, description="Preferowany język, np. pl, en, de"),
    safesearch: Optional[str] = Query(
        None,
        description="Poziom filtracji: 0|1|2 lub off|moderate|strict",
        example="moderate",
    ),
    response: Response = None,
):
    if client is None:
        raise HTTPException(503, "client not ready")

    # Normalize 'site': treat "null"/"none"/"undefined"/"-"/"" as empty
    site_norm = (site or "").strip()
    if site_norm.lower() in {"null", "none", "undefined", "-"}:
        site_norm = ""
    query = f"site:{site_norm} {q}" if site_norm else q
    params = {"q": query, "format": "json", "pageno": str(page)}
    allowed_ranges = {"day", "week", "month", "year"}
    tr = (time_range or "").strip()
    # Toleruj wartości często generowane przez narzędzia/LLM: "null", "none", "undefined", "-"
    if tr.lower() in {"null", "none", "undefined", "-"}:
        tr = ""
    if tr:
        if tr not in allowed_ranges:
            raise HTTPException(422, f"invalid time_range; allowed: {', '.join(sorted(allowed_ranges))}")
        params["time_range"] = tr
    if language:
        params["language"] = language
    # Parse safesearch accepting numbers and common aliases
    if safesearch is not None:
        ss_map = {
            "0": 0, "off": 0, "none": 0,
            "1": 1, "moderate": 1, "med": 1,
            "2": 2, "strict": 2, "safe": 2,
        }
        val = ss_map.get(str(safesearch).strip().lower())
        if val is None:
            raise HTTPException(422, "invalid safesearch; allowed: 0|1|2 or off|moderate|strict")
        params["safesearch"] = str(val)

    url = searx_search_url(SEARXNG_URL)
    try:
        r = await client.get(url, params=params)
    except httpx.RequestError as e:
        raise HTTPException(502, f"search upstream error: {e}") from e

    if r.status_code != 200:
        raise HTTPException(r.status_code, f"searxng error: {r.text[:500]}")

    data = r.json()
    results = data.get("results", []) or []

    items: List[WebItem] = []
    for hit in results[:limit]:
        title = (hit.get("title") or "")[:200]
        url_ = hit.get("url") or ""
        snippet = (hit.get("content") or hit.get("snippet") or "")[:500]
        if url_:
            items.append(WebItem(title=title, url=url_, snippet=snippet))

    next_page = page + 1 if len(results) > limit else None

    if response is not None:
        response.headers["X-Tool-Name"] = __title__
        response.headers["X-Tool-Version"] = __version__
        response.headers["X-Tool-Source"] = "search"

    # Build a convenience hint to batch-fetch the URLs returned here
    try:
        from urllib.parse import quote_plus
        urls = [it.url for it in items if it.url]
        hint = None
        if urls:
            qs = "&".join(f"url={quote_plus(u)}" for u in urls)
            conc = min(8, len(urls))
            hint = f"/fetch?{qs}&concurrency={conc}"
    except Exception:
        hint = None

    return WebResult(query=q, items=items, next_page=next_page, batch_fetch_hint_get=hint)

async def _fetch_one(url: str, max_chars: int) -> FetchResult:
    truncated = False
    if max_chars > HARD_MAX_CHARS:
        max_chars = HARD_MAX_CHARS
        truncated = True

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": ACCEPT_LANG,
        "Accept-Encoding": "gzip, deflate, br",
    }
    try:
        resp = await client.get(url, headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(502, f"fetch upstream error: {e!r}") from e

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    is_html = ctype.startswith("text/html") or ctype == "application/xhtml+xml"

    if not is_html:
        text = resp.text[:max_chars]
        return FetchResult(
            url=url,
            title=None,
            content_markdown=text,
            excerpt=text[:400] if text else None,
            author=None,
            date=None,
            content_type=ctype,
            source="raw",
        )

    html_bytes = resp.content

    async def _run_trafilatura():
        try:
            return await asyncio.wait_for(asyncio.to_thread(trafilatura_extract, html_bytes, url), EXTRACT_TIMEOUT_S)
        except Exception:
            return "", {"title": None, "author": None, "date": None}

    async def _run_readability():
        try:
            return await asyncio.wait_for(asyncio.to_thread(readability_extract, html_bytes), EXTRACT_TIMEOUT_S)
        except Exception:
            return "", {"title": None, "author": None, "date": None}

    traf_md, traf_meta = await _run_trafilatura()
    read_md, read_meta = await _run_readability()

    sc_traf = score_markdown(traf_md)
    sc_read = score_markdown(read_md)

    if sc_traf >= max(sc_read, MIN_OUTPUT_CHARS):
        chosen_md, chosen_meta, chosen_source = traf_md, traf_meta, "trafilatura"
    elif sc_read >= max(sc_traf, MIN_OUTPUT_CHARS):
        chosen_md, chosen_meta, chosen_source = read_md, read_meta, "readability"
    else:
        if sc_traf >= sc_read:
            chosen_md, chosen_meta, chosen_source = traf_md, traf_meta, "trafilatura"
        else:
            chosen_md, chosen_meta, chosen_source = read_md, read_meta, "readability"

    if not chosen_md:
        # Robust fallbacks: try full-article readability, then markdownify whole HTML
        html_fallback = html_bytes.decode("utf-8", errors="ignore")
        try:
            doc2 = Document(html_fallback)
            full_html = doc2.summary()  # full article HTML
            chosen_md = md(full_html or "", strip=["script", "style"]) or ""
            chosen_source = "readability-fallback"
        except Exception:
            chosen_md = ""
        if not chosen_md:
            try:
                chosen_md = md(html_fallback or "", strip=["script", "style"]) or ""
                chosen_source = "html-fallback"
            except Exception:
                chosen_md = ""
        if not chosen_md:
            raise HTTPException(502, "content extraction failed")

    content = chosen_md.strip()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n… [truncated]"
        truncated = True

    title = traf_meta.get("title") or read_meta.get("title")
    author = safe_author(traf_meta.get("author") or read_meta.get("author"))
    date = traf_meta.get("date")

    return FetchResult(
        url=url,
        title=title,
        content_markdown=content,
        excerpt=content[:400],
        author=author,
        date=date,
        content_type=ctype,
        source=chosen_source,
    )


@app.get(
    
    "/fetch",
    response_model=FetchResponse,
    summary="Pobierz treść z jednego lub wielu URLi (GET)",
    description=(
        "Powtarzaj parametr url wiele razy lub użyj jednej wartości będącej listą JSON. "
        "Dla >1 URL działa równolegle. Concurrency: 1-20 (domyślnie min(8, n))."
    ),
)
async def fetch_get(
    url: List[str] = Query(..., description="Powtarzalny parametr ?url=... dla wielu adresów. Akceptuje też jedną wartość będącą JSON listą."),
    max_chars: int = Query(8000, ge=500, le=1000000, description="Limit znaków Markdown"),
    concurrency: Optional[int] = Query(None, ge=1, le=20, description="Współbieżność 1-20; domyślnie min(8,len(url))"),
    response: Response = None,
):
    if client is None:
        raise HTTPException(503, "client not ready")

    # Znormalizuj url: dopuszczamy formaty: powtarzany parametr, lista JSON w jednym parametrze, oraz CSV
    normalized: List[str] = []
    for u in url:
        s = (u or '').strip()
        if not s:
            continue
        if (s.startswith('[') and s.endswith(']')):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    normalized.extend([str(x).strip() for x in arr if x])
                    continue
            except Exception:
                try:
                    arr = ast.literal_eval(s)
                    if isinstance(arr, list):
                        normalized.extend([str(x).strip() for x in arr if x])
                        continue
                except Exception:
                    pass
        normalized.append(s.strip().strip('"\''))

    if not normalized:
        raise HTTPException(422, "no valid URLs provided in 'url' parameter")

    auto_c = min(8, max(1, len(normalized)))
    chosen_c = concurrency if (concurrency and concurrency > 0) else auto_c
    chosen_c = max(1, min(chosen_c, 20))
    sem = asyncio.Semaphore(chosen_c)
    results: List[FetchResult] = []
    errors: Dict[str, str] = {}

    async def _one(u: str):
        nonlocal results, errors
        async with sem:
            try:
                res = await _fetch_one(u, max_chars)
                results.append(res)
            except HTTPException as he:
                errors[u] = f"{he.status_code}: {he.detail}"
            except Exception as e:
                errors[u] = str(e)

    await asyncio.gather(*[_one(u) for u in normalized])

    if response is not None:
        response.headers["X-Tool-Name"] = __title__
        response.headers["X-Tool-Version"] = __version__
        response.headers["X-Tool-Source"] = "fetch"
        response.headers["X-Concurrency"] = str(chosen_c)

    return FetchResponse(results=results, errors=errors)


@app.post(
    
    "/fetch",
    response_model=FetchResponse,
    summary="Pobierz treść z jednego lub wielu URLi (POST)",
    description=(
        "Body JSON: {url: string|array, urls: array}. Dla >1 URL działa równolegle. "
        "Concurrency 1-20; domyślnie min(8, n)."
    ),
)
async def fetch_post(request: FetchRequest, response: Response = None):
    if client is None:
        raise HTTPException(503, "client not ready")

    req_urls: List[str] = []
    if request.urls:
        req_urls.extend([u for u in request.urls if u])
    if request.url:
        if isinstance(request.url, list):
            req_urls.extend([u for u in request.url if u])
        elif isinstance(request.url, str):
            s = request.url.strip()
            if s.startswith('[') and s.endswith(']'):
                try:
                    arr = json.loads(s)
                    if isinstance(arr, list):
                        req_urls.extend([str(x).strip() for x in arr if x])
                except Exception:
                    try:
                        arr = ast.literal_eval(s)
                        if isinstance(arr, list):
                            req_urls.extend([str(x).strip() for x in arr if x])
                        else:
                            req_urls.append(s)
                    except Exception:
                        req_urls.append(s)
            elif s:
                req_urls.append(s)
    if not req_urls:
        raise HTTPException(422, "no URLs provided: use 'url' or 'urls'")

    auto_c = min(8, max(1, len(req_urls)))
    chosen_c = request.concurrency if (request.concurrency and request.concurrency > 0) else auto_c
    chosen_c = max(1, min(chosen_c, 20))
    sem = asyncio.Semaphore(chosen_c)
    results: List[FetchResult] = []
    errors: Dict[str, str] = {}

    async def _one(u: str):
        nonlocal results, errors
        async with sem:
            try:
                res = await _fetch_one(u, request.max_chars)
                results.append(res)
            except HTTPException as he:
                errors[u] = f"{he.status_code}: {he.detail}"
            except Exception as e:
                errors[u] = str(e)

    await asyncio.gather(*[_one(u) for u in req_urls])

    if response is not None:
        response.headers["X-Tool-Name"] = __title__
        response.headers["X-Tool-Version"] = __version__
        response.headers["X-Tool-Source"] = "fetch"
        response.headers["X-Concurrency"] = str(chosen_c)

    return FetchResponse(results=results, errors=errors)


@app.get("/ui", response_class=HTMLResponse)
async def ui_page():
    return """
<!doctype html>
<html lang=pl>
<head>
  <meta charset=utf-8>
  <meta name=viewport content="width=device-width, initial-scale=1">
  <title>SearXNG OpenAPI Tool – Test UI</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:24px;line-height:1.4}
    h1{font-size:20px;margin:0 0 12px}
    h2{font-size:16px;margin:20px 0 8px}
    fieldset{border:1px solid #ddd;padding:12px;margin-bottom:16px}
    legend{padding:0 6px;color:#444}
    label{display:block;margin:6px 0 4px;font-size:13px;color:#333}
    input,select,textarea{width:100%;max-width:900px;padding:8px;border:1px solid #ccc;border-radius:6px}
    textarea{min-height:80px}
    button{margin-top:8px;padding:8px 12px;border:1px solid #1976d2;background:#1976d2;color:#fff;border-radius:6px;cursor:pointer}
    button.secondary{background:#555;border-color:#555}
    .row{display:flex;gap:12px;flex-wrap:wrap}
    .col{flex:1 1 260px}
    pre{background:#0b1021;color:#d6e1ff;padding:12px;border-radius:6px;overflow:auto;max-height:50vh}
    .muted{color:#666;font-size:12px}
  </style>
  <script>
    async function apiGet(path){
      const r = await fetch(path,{headers:{'Accept':'application/json'}});
      const t = await r.text();
      return { ok:r.ok, status:r.status, headers:Object.fromEntries(r.headers.entries()), text:t, json:safeJSON(t) };
    }
    async function apiPost(path, body){
      const r = await fetch(path,{method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json'}, body:JSON.stringify(body)});
      const t = await r.text();
      return { ok:r.ok, status:r.status, headers:Object.fromEntries(r.headers.entries()), text:t, json:safeJSON(t) };
    }
    function safeJSON(t){ try { return JSON.parse(t) } catch(_) { return null } }
    function show(id, res){
      const el = document.getElementById(id);
      const headers = res.headers||{};
      const meta = { status: res.status, ...(['x-tool-name','x-tool-version','x-tool-source','x-concurrency','x-content-truncated'].reduce((a,k)=>{ if(headers[k]) a[k]=headers[k]; return a; },{})) };
      el.textContent = JSON.stringify({ meta, body: res.json ?? res.text }, null, 2);
    }
    function q(id){ return document.getElementById(id).value }
    function buildQS(params){
      const usp = new URLSearchParams();
      Object.entries(params).forEach(([k,v])=>{ if(v!==undefined && v!==null && String(v).trim()!==''){ usp.set(k, v) } });
      return usp.toString();
    }

    // Handlers
    async function ping(){ show('out-health', await apiGet('/health')) }
    async function about(){ show('out-about', await apiGet('/about')) }
    async function runSearch(){
      const qs = buildQS({ q:q('s-q'), site:q('s-site'), time_range:q('s-tr'), page:q('s-page'), limit:q('s-limit'), language:q('s-lang'), safesearch:q('s-ss') });
      show('out-search', await apiGet('/search?'+qs));
    }
    async function runFetch(){
      const qs = buildQS({ url:q('f-url'), max_chars:q('f-max') });
      show('out-fetch', await apiGet('/fetch?'+qs));
    }
    function parseList(s){
      const t = s.trim();
      if(!t) return [];
      // Try JSON array
      try { const a = JSON.parse(t); if(Array.isArray(a)) return a.map(x=>String(x)); } catch(_){ }
      // Split only by newlines (do NOT split by commas; commas can be valid in URLs)
      const lines = t.split('\\r').join('\\n').split('\\n');
      const out = [];
      for(const line of lines){ const v = line.trim(); if(v) out.push(v); }
      return out;
    }
    async function runBatchGet(){
      const urls = parseList(q('b-urls'));
      const usp = new URLSearchParams();
      urls.forEach(u=>usp.append('url', u));
      const mx = q('b-max'); const cc = q('b-conc');
      if(mx) usp.set('max_chars', mx);
      if(cc) usp.set('concurrency', cc);
      show('out-batch', await apiGet('/fetch?'+usp.toString()));
    }
    async function runBatchPost(){
      const urls = parseList(q('b-urls'));
      const body = { urls: urls, max_chars: Number(q('b-max')||8000) };
      const cc = q('b-conc'); if(cc) body.concurrency = Number(cc);
      show('out-batch', await apiPost('/fetch', body));
    }

    // Using inline onclick attributes below to call handlers
  </script>
  </head>
  <body>
    <h1>SearXNG OpenAPI Tool – Test UI</h1>
    <p class=muted>
      Szybkie testy endpointów. Dla wielu URL-i preferuj <code>/fetch</code> z wieloma parametrami <code>url</code> lub body JSON. Dokumentacja: <a href="/docs">/docs</a>, OpenAPI: <a href="/openapi.json">/openapi.json</a>.
    </p>

    <fieldset>
      <legend>Health & About</legend>
      <div class=row>
        <div class=col>
          <button onclick="ping()">GET /health</button>
          <pre id=out-health></pre>
        </div>
        <div class=col>
          <button class=secondary onclick="about()">GET /about</button>
          <pre id=out-about></pre>
        </div>
      </div>
    </fieldset>

    <fieldset>
      <legend>Search</legend>
      <div class=row>
        <div class=col><label>q</label><input id=s-q placeholder="openai"></div>
        <div class=col><label>site</label><input id=s-site placeholder="example.com"></div>
        <div class=col><label>time_range</label><input id=s-tr placeholder="day|week|month|year"></div>
        <div class=col><label>page</label><input id=s-page type=number value=1></div>
        <div class=col><label>limit</label><input id=s-limit type=number value=5></div>
        <div class=col><label>language</label><input id=s-lang placeholder="pl|en|de"></div>
        <div class=col><label>safesearch</label><input id=s-ss placeholder="0|1|2|off|moderate|strict"></div>
      </div>
      <button onclick="runSearch()">GET /search</button>
      <pre id=out-search></pre>
    </fieldset>

    <fieldset>
      <legend>Fetch (single)</legend>
      <div class=row>
        <div class=col><label>url</label><input id=f-url placeholder="https://example.com"></div>
        <div class=col><label>max_chars</label><input id=f-max type=number value=8000></div>
      </div>
      <button onclick="runFetch()">GET /fetch</button>
      <pre id=out-fetch></pre>
    </fieldset>

    <fieldset>
      <legend>Fetch: wiele URL-i (GET / POST)</legend>
      <div class=row>
        <div class=col>
          <label>URLs (jeden na linię lub JSON [..])</label>
          <textarea id=b-urls placeholder="https://example.com\nhttps://httpbin.org/json"></textarea>
        </div>
        <div class=col>
          <label>max_chars</label>
          <input id=b-max type=number value=8000>
          <label>concurrency</label>
          <input id=b-conc type=number placeholder="auto (min(8, n))">
        </div>
      </div>
      <div class=row>
        <div class=col><button onclick="runBatchGet()">GET /fetch</button></div>
        <div class=col><button class=secondary onclick="runBatchPost()">POST /fetch</button></div>
      </div>
      <pre id=out-batch></pre>
    </fieldset>
  </body>
  </html>
  """
