# app.py
"""
SearXNG OpenAPI Tool

- Autor: Seweryn Sitarski, Kat (asysta kodowa)
- Kontakt: seweryn.sitarski@gmail.com
- Wersja: 0.7.1
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
from fastapi.responses import HTMLResponse, ORJSONResponse
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
import orjson
import ast
import os
import re

import trafilatura
from trafilatura.settings import use_config

# Readability (python-readability / readability-lxml)
from readability.readability import Document
from markdownify import markdownify as md

__title__ = "searxng-openapi-tool"
__version__ = "0.7.1"
__author__ = "Seweryn Sitarski, Kat"
__license__ = "MIT"
__contact__ = "seweryn.sitarski@gmail.com"
__url__ = "https://example.local/searxng-openapi-tool"

app = FastAPI(
    title=__title__,
    version=__version__,
    contact={"name": __author__, "email": __contact__},
    default_response_class=ORJSONResponse,
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
SEARCH_SHOW_HINTS = (
    os.environ.get("SEARCH_SHOW_HINTS", "0").strip().lower() in {"1", "true", "yes", "on"}
)
INCLUDE_EXCERPT = (
    os.environ.get("INCLUDE_EXCERPT", "0").strip().lower() in {"1", "true", "yes", "on"}
)

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


class BatchSearchResponse(BaseModel):
    results: List[Dict[str, object]]
    errors: Dict[str, str] = Field(default_factory=dict)

class FetchResult(BaseModel):
    model_config = ConfigDict(ser_json_exclude_none=True)
    url: str
    title: Optional[str]
    content_markdown: str
    excerpt: Optional[str] = Field(
        default=None,
        description="Short excerpt (<=400 chars) returned only when INCLUDE_EXCERPT admin flag is true",
    )
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


def _expand_query_values(values: List[str]) -> List[str]:
    normalized: List[str] = []
    for raw in values:
        s = (raw or "").strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = orjson.loads(s)
                if isinstance(parsed, list):
                    for item in parsed:
                        val = str(item).strip()
                        if val:
                            normalized.append(val)
                    continue
            except Exception:
                try:
                    parsed = ast.literal_eval(s)
                    if isinstance(parsed, list):
                        for item in parsed:
                            val = str(item).strip()
                            if val:
                                normalized.append(val)
                        continue
                except Exception:
                    pass
        normalized.append(s)
    return normalized


def _normalize_site(site: Optional[str]) -> str:
    site_norm = (site or "").strip()
    return "" if site_norm.lower() in {"null", "none", "undefined", "-"} else site_norm


def _normalize_time_range(time_range: Optional[str]) -> Optional[str]:
    if not time_range:
        return None
    candidate = time_range.strip()
    if not candidate:
        return None
    if candidate.lower() in {"null", "none", "undefined", "-"}:
        return None
    allowed = {"day", "week", "month", "year"}
    if candidate not in allowed:
        raise HTTPException(422, f"invalid time_range; allowed: {', '.join(sorted(allowed))}")
    return candidate


def _parse_safesearch(safesearch: Optional[str]) -> Optional[str]:
    if safesearch is None:
        return None
    ss_map = {
        "0": "0",
        "off": "0",
        "none": "0",
        "1": "1",
        "moderate": "1",
        "med": "1",
        "2": "2",
        "strict": "2",
        "safe": "2",
    }
    key = str(safesearch).strip().lower()
    value = ss_map.get(key)
    if value is None:
        raise HTTPException(422, "invalid safesearch; allowed: 0|1|2 or off|moderate|strict")
    return value


def _serialize_webresult(result: WebResult) -> Dict[str, object]:
    payload = result.model_dump()
    if not SEARCH_SHOW_HINTS or not payload.get("batch_fetch_hint_get"):
        payload.pop("batch_fetch_hint_get", None)
    return payload

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
@app.get("/about", response_model=About, operation_id="about")
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
            "SEARCH_SHOW_HINTS": str(SEARCH_SHOW_HINTS),
            "INCLUDE_EXCERPT": str(INCLUDE_EXCERPT),
        },
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        examples={
            "health": 'curl -sS http://localhost:7000/health',
            "about": 'curl -sS http://localhost:7000/about',
            "search": 'curl -sS "http://localhost:7000/search?q=openai&limit=3&safesearch=moderate"',
            "search_multi": 'curl -sS "http://localhost:7000/search?q=openai&q=python&limit=2"',
            "fetch_single_get": 'curl -sS "http://localhost:7000/fetch?url=https://example.com&max_chars=2000"',
            "fetch_multi_post": 'curl -sS -H "Content-Type: application/json" -d \'{"urls":["https://example.com","https://httpbin.org/json"],"max_chars":1500,"concurrency":4}\' http://localhost:7000/fetch',
            "fetch_multi_get": 'curl -sS "http://localhost:7000/fetch?url=https://example.com&url=https://httpbin.org/json&max_chars=1500&concurrency=4"',
        },
    )

@app.get("/health", operation_id="health")
async def health():
    return {"status": "ok"}

async def _execute_search_query(
    query: str,
    site_norm: str,
    time_range_value: Optional[str],
    page: int,
    limit: int,
    language: Optional[str],
    safesearch_value: Optional[str],
) -> WebResult:
    if client is None:
        raise HTTPException(503, "client not ready")

    q_effective = f"site:{site_norm} {query}" if site_norm else query
    params = {"q": q_effective, "format": "json", "pageno": str(page)}
    if time_range_value:
        params["time_range"] = time_range_value
    if language:
        params["language"] = language
    if safesearch_value is not None:
        params["safesearch"] = safesearch_value

    url = searx_search_url(SEARXNG_URL)
    try:
        r = await client.get(url, params=params)
    except httpx.RequestError as e:
        raise HTTPException(502, f"search upstream error: {e}") from e

    if r.status_code != 200:
        raise HTTPException(r.status_code, f"searxng error: {r.text[:500]}")

    try:
        data = orjson.loads(r.content)
    except orjson.JSONDecodeError:
        raise HTTPException(502, f"invalid JSON from upstream: {r.text[:500]}")

    results_raw = data.get("results", []) or []
    items: List[WebItem] = []
    for hit in results_raw[:limit]:
        title = (hit.get("title") or "")[:200]
        url_ = hit.get("url") or ""
        snippet = (hit.get("content") or hit.get("snippet") or "")[:500]
        if url_:
            items.append(WebItem(title=title, url=url_, snippet=snippet))

    next_page = page + 1 if len(results_raw) > limit else None

    hint = None
    if SEARCH_SHOW_HINTS:
        try:
            from urllib.parse import quote_plus

            urls = [it.url for it in items if it.url]
            if urls:
                qs = "&".join(f"url={quote_plus(u)}" for u in urls)
                conc = min(8, len(urls))
                hint = f"/fetch?{qs}&concurrency={conc}"
        except Exception:
            hint = None

    return WebResult(
        query=query,
        items=items,
        next_page=next_page,
        batch_fetch_hint_get=hint if hint else None,
    )


@app.get(
    "/search",
    response_model=Union[WebResult, BatchSearchResponse],
    response_model_exclude={"batch_fetch_hint_get"} if not SEARCH_SHOW_HINTS else set(),
    operation_id="search",
)
async def search(
    q: List[str] = Query(..., description="Search query (repeat ?q=... for batch mode)"),
    site: Optional[str] = Query(None, description="Limit to domain, e.g. example.com (values 'null'/'none'/'undefined' treated as empty)"),
    time_range: Optional[str] = Query(
        None,
        description="Time filter: day|week|month|year",
    ),
    page: int = Query(1, ge=1, description="Results page number (>=1)"),
    limit: int = Query(
        5,
        ge=1,
        le=20,
        description="Maximum number of results per query (1-20); in batch mode each query receives up to this many items",
    ),
    language: Optional[str] = Query(None, description="Preferred language, e.g. en, pl, de"),
    safesearch: Optional[str] = Query(
        None,
        description="Safe search level: 0|1|2 or off|moderate|strict",
        example="moderate",
    ),
    response: Response = None,
):
    if client is None:
        raise HTTPException(503, "client not ready")

    queries = _expand_query_values(q)
    if not queries:
        raise HTTPException(422, "no queries provided in 'q' parameter")
    for item in queries:
        if len(item) < 2:
            raise HTTPException(422, "each query must be at least 2 characters")

    site_norm = _normalize_site(site)
    time_range_value = _normalize_time_range(time_range)
    safesearch_value = _parse_safesearch(safesearch)

    async def _run(single_query: str):
        try:
            result = await _execute_search_query(
                query=single_query,
                site_norm=site_norm,
                time_range_value=time_range_value,
                page=page,
                limit=limit,
                language=language,
                safesearch_value=safesearch_value,
            )
            return single_query, result, None
        except HTTPException as he:
            return single_query, None, he
        except Exception as exc:
            return single_query, None, exc

    concurrency = max(1, min(len(queries), 8))
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(single_query: str):
        async with sem:
            return await _run(single_query)

    tasks = [_bounded(query_text) for query_text in queries]
    execution = await asyncio.gather(*tasks)

    results: List[WebResult] = []
    errors: Dict[str, str] = {}
    raw_errors: Dict[str, Exception] = {}
    for query_text, res, err in execution:
        if res is not None:
            results.append(res)
        else:
            raw_errors[query_text] = err if err else Exception("unknown error")
            if isinstance(err, HTTPException):
                errors[query_text] = f"{err.status_code}: {err.detail}"
            else:
                errors[query_text] = str(err)

    if response is not None:
        response.headers["X-Tool-Name"] = __title__
        response.headers["X-Tool-Version"] = __version__
        response.headers["X-Tool-Source"] = "search"
        response.headers["X-Query-Count"] = str(len(queries))

    if len(queries) == 1:
        single_query = queries[0]
        if single_query in raw_errors:
            err = raw_errors[single_query]
            if isinstance(err, HTTPException):
                raise err
            raise HTTPException(500, str(err))
        if results:
            return _serialize_webresult(results[0])

    serialized_results = [_serialize_webresult(res) for res in results]
    return BatchSearchResponse(results=serialized_results, errors=errors)

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
            excerpt=text[:400] if INCLUDE_EXCERPT and text else None,
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

    # Quick readability pass first; only escalate to Trafilatura if the output looks weak.
    read_md, read_meta = await _run_readability()
    sc_read = score_markdown(read_md)

    chosen_md = read_md
    chosen_meta = read_meta
    chosen_source = "readability"

    traf_md = ""
    traf_meta: Dict[str, Optional[str]] = {"title": None, "author": None, "date": None}
    sc_traf = 0

    if sc_read < MIN_OUTPUT_CHARS:
        traf_md, traf_meta = await _run_trafilatura()
        sc_traf = score_markdown(traf_md)
        if sc_traf >= sc_read:
            chosen_md, chosen_meta, chosen_source = traf_md, traf_meta, "trafilatura"

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
        excerpt=content[:400] if INCLUDE_EXCERPT and content else None,
        author=author,
        date=date,
        content_type=ctype,
        source=chosen_source,
    )


@app.get(
    
    "/fetch",
    response_model=FetchResponse,
    response_model_exclude_none=True,
    summary="Fetch content from one or more URLs (GET)",
    description=(
        "Repeat the url parameter multiple times or pass a single JSON list value. "
        "For more than one URL the tool runs requests in parallel. Concurrency: 1-20 (default min(8, n)). "
        "Administrator can enable INCLUDE_EXCERPT=1 to add short summaries; default is disabled to save model context."
    ),
    operation_id="fetch_get",
)
async def fetch_get(
    url: List[str] = Query(..., description="Repeatable ?url=... parameter for multiple addresses. Accepts one value that is a JSON list."),
    max_chars: int = Query(8000, ge=500, le=1000000, description="Markdown character limit"),
    concurrency: Optional[int] = Query(None, ge=1, le=20, description="Concurrency 1-20; default min(8, len(url))"),
    response: Response = None,
):
    if client is None:
        raise HTTPException(503, "client not ready")

    # Normalize url values: repeated parameters, JSON list in a single parameter, and CSV-like strings
    normalized: List[str] = []
    for u in url:
        s = (u or '').strip()
        if not s:
            continue
        if (s.startswith('[') and s.endswith(']')):
            try:
                arr = orjson.loads(s)
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
    response_model_exclude_none=True,
    summary="Fetch content from one or more URLs (POST)",
    description=(
        "JSON body: {url: string|array, urls: array}. For multiple URLs requests run in parallel. "
        "Concurrency 1-20; default min(8, n). Short summaries appear only when admin sets INCLUDE_EXCERPT=1."
    ),
    operation_id="fetch_post",
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
                    arr = orjson.loads(s)
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


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
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
        <div class=col><label>q</label><input id=s-q value="openai" placeholder="openai"></div>
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
        <div class=col><label>url</label><input id=f-url value="https://example.com" placeholder="https://example.com"></div>
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
          <textarea id=b-urls>https://example.com
https://httpbin.org/json</textarea>
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
