import os
import re
import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from threading import Lock
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "https://anitaku.by")

search_cache   = TTLCache(maxsize=200, ttl=300)
info_cache     = TTLCache(maxsize=200, ttl=3600)
episodes_cache = TTLCache(maxsize=500, ttl=600)
sources_cache  = TTLCache(maxsize=500, ttl=180)
cache_lock     = Lock()

app = FastAPI(title="Anime Scraper API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL + "/",
}


def get_html(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch: {url}")
        return resp.text
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Connection error: {str(e)}")


@app.get("/")
def root():
    return {
        "name": "Anime Scraper API",
        "version": "3.0.0",
        "source": BASE_URL,
        "docs": "/docs",
        "endpoints": {
            "GET /search?q=naruto": "Search anime",
            "GET /info?id=naruto-shippuden": "Anime info",
            "GET /episodes?id=naruto-shippuden": "Episode list",
            "GET /stream?ep_id=naruto-shippuden-episode-1": "Streaming sources",
            "GET /health": "Health check",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "source": BASE_URL}


@app.get("/search")
def search(q: str = Query(..., min_length=2)):
    key = q.lower().strip()
    with cache_lock:
        if key in search_cache:
            return search_cache[key]

    html = get_html(f"{BASE_URL}/search.html?keyword={requests.utils.quote(q)}")
    soup = BeautifulSoup(html, "html.parser")

    results = []
    for item in soup.select("ul.items li"):
        a = item.select_one("p.name a")
        img = item.select_one("div.img a img")
        released = item.select_one("p.released")
        if not a:
            continue
        href = a.get("href", "")
        anime_id = href.strip("/").replace("category/", "")
        results.append({
            "id": anime_id,
            "title": a.get_text(strip=True),
            "poster": img.get("src", "") if img else "",
            "released": released.get_text(strip=True).replace("Released:", "").strip() if released else None,
            "url": BASE_URL + href,
        })

    response = {"query": q, "count": len(results), "results": results}
    with cache_lock:
        search_cache[key] = response
    return response


@app.get("/info")
def anime_info(id: str = Query(..., description="Anime ID e.g. naruto-shippuden")):
    with cache_lock:
        if id in info_cache:
            return info_cache[id]

    html = get_html(f"{BASE_URL}/category/{id}")
    soup = BeautifulSoup(html, "html.parser")

    title = (soup.select_one("div.anime_info_body_bg h1") or {}).get_text(strip=True) if soup.select_one("div.anime_info_body_bg h1") else ""
    poster_tag = soup.select_one("div.anime_info_body_bg img")
    poster = poster_tag.get("src", "") if poster_tag else ""

    info = {}
    for p in soup.select("div.anime_info_body_bg p.type"):
        span = p.find("span")
        if span:
            label = span.get_text(strip=True).rstrip(":").lower()
            value = p.get_text(strip=True).replace(span.get_text(strip=True), "").strip()
            info[label] = value

    genres = [a.get_text(strip=True) for a in soup.select("p.type a[href*='genre']")]

    # Get episode range
    ep_start = soup.select_one("#episode_page a")
    ep_end = soup.select_one("#episode_page a:last-child")
    total_eps = None
    if ep_end:
        try:
            total_eps = int(ep_end.get("ep_end", 0))
        except Exception:
            pass

    # Get anime ID for ajax calls
    movie_id = None
    movie_id_tag = soup.select_one("#movie_id")
    if movie_id_tag:
        movie_id = movie_id_tag.get("value")

    result = {
        "id": id,
        "title": title,
        "poster": poster,
        "genres": genres,
        "type": info.get("type"),
        "status": info.get("status"),
        "released": info.get("released"),
        "other_name": info.get("other name"),
        "summary": info.get("plot summary"),
        "total_episodes": total_eps,
        "movie_id": movie_id,
        "url": f"{BASE_URL}/category/{id}",
    }

    with cache_lock:
        info_cache[id] = result
    return result


@app.get("/episodes")
def episodes(id: str = Query(..., description="Anime ID e.g. naruto-shippuden")):
    with cache_lock:
        if id in episodes_cache:
            return episodes_cache[id]

    # First get the movie_id and episode range from the info page
    html = get_html(f"{BASE_URL}/category/{id}")
    soup = BeautifulSoup(html, "html.parser")

    movie_id_tag = soup.select_one("#movie_id")
    if not movie_id_tag:
        raise HTTPException(status_code=404, detail=f"Anime not found: {id}")
    movie_id = movie_id_tag.get("value")

    # Get episode range
    ep_pages = soup.select("#episode_page a")
    if not ep_pages:
        raise HTTPException(status_code=404, detail="No episodes found")

    ep_start = ep_pages[0].get("ep_start", "0")
    ep_end = ep_pages[-1].get("ep_end", "0")

    # Fetch episode list via ajax
    ajax_url = f"https://ajax.gogocdn.net/ajax/load-list-episode?ep_start={ep_start}&ep_end={ep_end}&id={movie_id}"
    ajax_html = get_html(ajax_url)
    ajax_soup = BeautifulSoup(ajax_html, "html.parser")

    eps = []
    for li in reversed(ajax_soup.select("li")):
        a = li.select_one("a")
        ep_num = li.select_one(".name")
        sub = li.select_one(".cate")
        if not a:
            continue
        href = a.get("href", "").strip()
        ep_id = href.strip("/")
        number = ep_num.get_text(strip=True).replace("EP", "").strip() if ep_num else ""
        eps.append({
            "id": ep_id,
            "number": number,
            "type": sub.get_text(strip=True) if sub else "SUB",
            "url": BASE_URL + "/" + ep_id,
        })

    result = {"anime_id": id, "total": len(eps), "episodes": eps}
    with cache_lock:
        episodes_cache[id] = result
    return result


@app.get("/stream")
def stream(ep_id: str = Query(..., description="Episode ID e.g. naruto-shippuden-episode-1")):
    with cache_lock:
        if ep_id in sources_cache:
            return sources_cache[ep_id]

    html = get_html(f"{BASE_URL}/{ep_id}")
    soup = BeautifulSoup(html, "html.parser")

    sources = []

    # Get all server links
    for li in soup.select("div.anime_muti_link ul li"):
        a = li.select_one("a")
        if not a:
            continue
        server_name = li.get("class", ["unknown"])[0]
        data_video = a.get("data-video", "")
        if data_video:
            if not data_video.startswith("http"):
                data_video = "https:" + data_video
            sources.append({
                "server": server_name,
                "url": data_video,
            })

    # Also grab the default embed
    default_embed = soup.select_one("div.play-video iframe")
    if default_embed:
        src = default_embed.get("src", "")
        if src:
            if not src.startswith("http"):
                src = "https:" + src
            sources.insert(0, {"server": "default", "url": src})

    result = {
        "episode_id": ep_id,
        "sources": sources,
        "watch_url": f"{BASE_URL}/{ep_id}",
    }

    with cache_lock:
        sources_cache[ep_id] = result
    return result
