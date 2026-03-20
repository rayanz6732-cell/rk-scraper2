import os
import re
import requests
import cloudscraper
from bs4 import BeautifulSoup
from cachetools import TTLCache
from threading import Lock
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "https://animepahe.ru")

# Try multiple strategies to bypass Cloudflare
scraper = cloudscraper.create_scraper(
    browser={
        "browser": "chrome",
        "platform": "windows",
        "desktop": True,
        "mobile": False
    },
    delay=5
)

search_cache   = TTLCache(maxsize=200, ttl=300)
info_cache     = TTLCache(maxsize=200, ttl=3600)
episodes_cache = TTLCache(maxsize=500, ttl=600)
sources_cache  = TTLCache(maxsize=500, ttl=180)
m3u8_cache     = TTLCache(maxsize=500, ttl=180)
cache_lock     = Lock()

app = FastAPI(title="Animepahe Scraper API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": BASE_URL + "/",
    "Origin": BASE_URL,
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}


def get(url: str, **kwargs):
    extra_headers = kwargs.pop("headers", {})
    combined_headers = {**HEADERS, **extra_headers}

    # First try cloudscraper
    try:
        resp = scraper.get(url, headers=combined_headers, timeout=30, **kwargs)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return resp.text
    except Exception:
        pass

    # Fallback to plain requests
    try:
        resp = requests.get(url, headers=combined_headers, timeout=30, **kwargs)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return resp.text
        raise HTTPException(status_code=resp.status_code, detail=f"Upstream error: {url} - Status: {resp.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Connection error: {str(e)}")


def resolve_m3u8_from_kwik(kwik_url: str) -> str:
    headers = {
        **HEADERS,
        "Referer": BASE_URL + "/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = scraper.get(kwik_url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch kwik page")

    html = resp.text

    m3u8_match = re.search(r"source=\\?[\"'](https?://[^\"'\\]+\.m3u8[^\"'\\]*)", html)
    if m3u8_match:
        return m3u8_match.group(1).replace("\\", "")

    packed_match = re.search(r"eval\(function\(p,a,c,k,e,[sd]\).*?\)\)", html, re.DOTALL)
    if packed_match:
        packed = packed_match.group(0)
        try:
            array_match = re.search(r"\|([a-zA-Z0-9|_$]+)\|", packed)
            body_match = re.search(r"'([^']+)'\.split\('\\|'\)", packed)
            if array_match and body_match:
                words = body_match.group(1).split("|")
                unpacked = array_match.group(0)
                for i, word in enumerate(words):
                    if word:
                        unpacked = re.sub(r'\b' + str(i) + r'\b', word, unpacked)
                m3u8_in_unpacked = re.search(r"(https?://[^\s\"'\\]+\.m3u8[^\s\"'\\]*)", unpacked)
                if m3u8_in_unpacked:
                    return m3u8_in_unpacked.group(1)
        except Exception:
            pass

    m3u8_any = re.search(r"(https?://[^\s\"'\\]+\.m3u8[^\s\"'\\]*)", html)
    if m3u8_any:
        return m3u8_any.group(1)

    raise HTTPException(status_code=502, detail="Could not extract m3u8 URL from kwik")


@app.get("/")
def root():
    return {
        "name": "Animepahe Scraper API",
        "version": "2.0.0",
        "docs": "/docs",
        "endpoints": {
            "GET /search?q=naruto": "Search anime by title",
            "GET /info?session=<session>": "Full anime info",
            "GET /episodes?session=<session>&page=1": "Episode list",
            "GET /episodes?session=<session>&all=true": "All episodes",
            "GET /sources?anime_session=<s>&episode_session=<s>": "Streaming sources",
            "GET /m3u8?url=<kwik_url>": "Resolve kwik to direct stream URL",
            "GET /top?page=1": "Top anime",
            "GET /health": "Health check",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "base_url": BASE_URL}


@app.get("/search")
def search(q: str = Query(..., min_length=2)):
    key = q.lower().strip()
    with cache_lock:
        if key in search_cache:
            return search_cache[key]

    data = get(f"{BASE_URL}/api?m=search&q={q}")
    if isinstance(data, str):
        raise HTTPException(status_code=502, detail="Unexpected response from search")

    results = [
        {
            "id": item.get("id"),
            "session": item.get("session"),
            "title": item.get("title"),
            "type": item.get("type"),
            "episodes": item.get("episodes"),
            "status": item.get("status"),
            "season": item.get("season"),
            "year": item.get("year"),
            "score": item.get("score"),
            "poster": item.get("poster"),
        }
        for item in (data.get("data") or [])
    ]

    response = {"query": q, "count": len(results), "results": results}
    with cache_lock:
        search_cache[key] = response
    return response


@app.get("/info")
def anime_info(session: str = Query(...)):
    with cache_lock:
        if session in info_cache:
            return info_cache[session]

    html = get(f"{BASE_URL}/anime/{session}")
    if not isinstance(html, str):
        raise HTTPException(status_code=502, detail="Unexpected response")

    soup = BeautifulSoup(html, "lxml")

    title = (
        (soup.find("span", itemprop="name") or {}).get_text(strip=True)
        or (soup.find("h1") or {}).get_text(strip=True)
        or ""
    )
    jp_title = (soup.find("span", itemprop="alternateName") or {}).get_text(strip=True) or ""
    poster_tag = soup.select_one("div.anime-poster img")
    poster = (poster_tag.get("data-src") or poster_tag.get("src") or "") if poster_tag else ""
    synopsis_tag = soup.select_one("div.anime-synopsis") or soup.find(itemprop="description")
    synopsis = synopsis_tag.get_text(strip=True) if synopsis_tag else ""

    info = {}
    for p in soup.select("div.anime-info p"):
        strong = p.find("strong")
        if strong:
            label = strong.get_text(strip=True).rstrip(":").lower()
            value = p.get_text(strip=True).replace(strong.get_text(strip=True), "").strip()
            info[label] = value

    genres = [a.get_text(strip=True) for a in soup.select("div.anime-genre a, [itemprop='genre']")]

    result = {
        "session": session,
        "title": title,
        "japanese_title": jp_title,
        "poster": poster,
        "synopsis": synopsis,
        "genres": genres,
        "type": info.get("type"),
        "status": info.get("status"),
        "total_episodes": info.get("episodes"),
        "aired": info.get("aired"),
        "season": info.get("season"),
        "studio": info.get("studio"),
        "score": info.get("score"),
        "url": f"{BASE_URL}/anime/{session}",
    }

    with cache_lock:
        info_cache[session] = result
    return result


@app.get("/episodes")
def episodes(
    session: str = Query(...),
    page: int = Query(1, ge=1),
    all: bool = Query(False),
):
    if all:
        return _get_all_episodes(session)

    cache_key = f"{session}:{page}"
    with cache_lock:
        if cache_key in episodes_cache:
            return episodes_cache[cache_key]

    result = _fetch_episode_page(session, page)
    with cache_lock:
        episodes_cache[cache_key] = result
    return result


def _fetch_episode_page(session: str, page: int) -> dict:
    data = get(f"{BASE_URL}/api?m=release&id={session}&sort=episode_asc&page={page}")
    if isinstance(data, str):
        raise HTTPException(status_code=502, detail="Unexpected response fetching episodes")

    eps = [
        {
            "id": ep.get("id"),
            "number": ep.get("episode"),
            "title": ep.get("title") or f"Episode {ep.get('episode')}",
            "snapshot": ep.get("snapshot"),
            "duration": ep.get("duration"),
            "session": ep.get("session"),
            "filler": bool(ep.get("filler")),
            "created_at": ep.get("created_at"),
        }
        for ep in (data.get("data") or [])
    ]

    return {
        "anime_session": session,
        "total": data.get("total", len(eps)),
        "per_page": data.get("per_page", 30),
        "current_page": data.get("current_page", page),
        "last_page": data.get("last_page", 1),
        "data": eps,
    }


def _get_all_episodes(session: str) -> dict:
    cache_key = f"{session}:all"
    with cache_lock:
        if cache_key in episodes_cache:
            return episodes_cache[cache_key]

    first = _fetch_episode_page(session, 1)
    all_eps = list(first["data"])

    for page in range(2, first["last_page"] + 1):
        page_data = _fetch_episode_page(session, page)
        all_eps.extend(page_data["data"])

    result = {"anime_session": session, "total": len(all_eps), "data": all_eps}
    with cache_lock:
        episodes_cache[cache_key] = result
    return result


@app.get("/sources")
def sources(
    anime_session: str = Query(...),
    episode_session: str = Query(...),
):
    cache_key = f"{anime_session}:{episode_session}"
    with cache_lock:
        if cache_key in sources_cache:
            return sources_cache[cache_key]

    watch_url = f"{BASE_URL}/play/{anime_session}/{episode_session}"
    html = get(watch_url)
    if not isinstance(html, str):
        raise HTTPException(status_code=502, detail="Unexpected response from watch page")

    soup = BeautifulSoup(html, "lxml")
    server_results = []
    seen = set()

    for btn in soup.select("#pickServers .server-item, div.dropdown-menu a[data-src]"):
        src = btn.get("data-src") or btn.get("href") or ""
        if not src or src in seen:
            continue
        seen.add(src)
        server_results.append({
            "url": src,
            "quality": btn.get("data-res") or "unknown",
            "fansub": btn.get("data-fansub") or None,
            "audio": btn.get("data-audio") or "jpn",
        })

    if not server_results:
        kwik_matches = re.findall(r"https?://kwik\.[a-z]+/e/[a-zA-Z0-9]+", html)
        for url in dict.fromkeys(kwik_matches):
            server_results.append({"url": url, "quality": "unknown", "fansub": None, "audio": "jpn"})

    result = {
        "anime_session": anime_session,
        "episode_session": episode_session,
        "watch_url": watch_url,
        "sources": server_results,
    }

    with cache_lock:
        sources_cache[cache_key] = result
    return result


@app.get("/m3u8")
def m3u8(url: str = Query(...)):
    with cache_lock:
        if url in m3u8_cache:
            return m3u8_cache[url]

    stream_url = resolve_m3u8_from_kwik(url)
    result = {"kwik_url": url, "m3u8": stream_url}

    with cache_lock:
        m3u8_cache[url] = result
    return result


@app.get("/top")
def top_anime(page: int = Query(1, ge=1)):
    html = get(f"{BASE_URL}/?page={page}")
    if not isinstance(html, str):
        raise HTTPException(status_code=502, detail="Unexpected response")

    soup = BeautifulSoup(html, "lxml")
    results = []

    for card in soup.select("div.col-sm-6.col-md-4.col-lg-3"):
        a = card.find("a")
        img = card.find("img")
        if not a:
            continue
        href = a.get("href", "")
        session = href.split("/anime/")[-1] if "/anime/" in href else ""
        title = a.get("title") or (img.get("alt") if img else "") or ""
        poster = (img.get("data-src") or img.get("src") or "") if img else ""
        score = card.select_one(".score")
        ep = card.select_one(".ep")

        if session:
            results.append({
                "session": session,
                "title": title,
                "poster": poster,
                "score": score.get_text(strip=True) if score else None,
                "episodes_aired": ep.get_text(strip=True) if ep else None,
            })

    return {"page": page, "count": len(results), "results": results}
