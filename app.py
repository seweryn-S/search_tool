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
from pydantic import BaseModel
from typing import List, Optional, Tuple, Dict
import httpx
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
        endpoints=["/health", "/search", "/fetch", "/about"],
        env={
            "SEARXNG_URL": SEARXNG_URL,
            "TIMEOUT_S": str(TIMEOUT_S),
            "MAX_CONN": str(MAX_CONN),
            "MAX_KEEP": str(MAX_KEEP),
            "HARD_MAX_CHARS": str(HARD_MAX_CHARS),
            "MIN_OUTPUT_CHARS": str(MIN_OUTPUT_CHARS),
            "ACCEPT_LANGUAGE": ACCEPT_LANG,
            "USER_AGENT": USER_AGENT,
        },
    )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/search", response_model=WebResult)
async def search(
    q: str = Query(..., min_length=2),
    site: Optional[str] = Query(None),
    time_range: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=20),
    language: Optional[str] = Query(None),
    safesearch: Optional[int] = Query(None, ge=0, le=2),
    response: Response = None,
):
    if client is None:
        raise HTTPException(503, "client not ready")

    query = f"site:{site} {q}" if site else q
    params = {"q": query, "format": "json", "pageno": str(page)}
    if time_range in {"day", "week", "month", "year"}:
        params["time_range"] = time_range
    if language:
        params["language"] = language
    if safesearch is not None:
        params["safesearch"] = str(safesearch)

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

    return WebResult(query=q, items=items, next_page=next_page)

@app.get("/fetch", response_model=FetchResult)
async def fetch_url(
    url: str = Query(...),
    max_chars: int = Query(8000, ge=500, le=1000000),
    response: Response = None,
):
    if client is None:
        raise HTTPException(503, "client not ready")

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
        raise HTTPException(502, f"fetch upstream error: {e}") from e

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()

    if not ctype.startswith("text/html"):
        text = resp.text[:max_chars]
        if response is not None:
            response.headers["X-Tool-Name"] = __title__
            response.headers["X-Tool-Version"] = __version__
            response.headers["X-Tool-Source"] = "raw"
            if truncated:
                response.headers["X-Content-Truncated"] = "true"
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
    traf_md, traf_meta = trafilatura_extract(html_bytes, url)
    read_md, read_meta = readability_extract(html_bytes)

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
        raise HTTPException(502, "content extraction failed")

    content = chosen_md.strip()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n… [truncated]"
        truncated = True

    title = traf_meta.get("title") or read_meta.get("title")
    author = safe_author(traf_meta.get("author") or read_meta.get("author"))
    date = traf_meta.get("date")

    if response is not None:
        response.headers["X-Tool-Name"] = __title__
        response.headers["X-Tool-Version"] = __version__
        response.headers["X-Tool-Source"] = chosen_source
        if truncated:
            response.headers["X-Content-Truncated"] = "true"

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

