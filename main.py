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

BASE_URL = os.getenv("BASE_URL", "https://gogoanime3.cc")

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
            raise HTTPException(status_code=resp.status_code, detail=f"Failed: {url} status={resp.status_code}")
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

    url = f"{BASE_URL}/search.html?keyword={requests.utils.quote(q)}"
    html = get_html(url)
    soup = BeautifulSoup(html, "html.parser")

    results = []

    # Try multiple selectors since gogoanime changes their HTML
    items = soup.select("ul.items li") or soup.select("div.last_episodes ul li") or soup.select("div.anime_list_body ul li")

    for item in items:
        a = item.select_one("p.name a") or item.select_one("div.name a") or item.select_one("a")
        img = item.select_one("img")
        released = item.select_one("p.released") or item.select_one("p.year")
        if not a:
            continue
        href = a.get("href", "")
        anime_id = href.strip("/").replace("category/", "")
        results.append({
            "id": anime_id,
            "title": a.get("title") or a.get_text(strip=True),
            "poster": img.get("src", "") if img else "",
            "released": released.get_text(strip=True).replace("Released:", "").strip() if released else None,
            "url": BASE_URL + href if not href.startswith("http") else href,
        })

    response = {"query": q, "count": len(results), "results": results}
    with cache_lock:
        search_cache[key] = response
    return response


@app.get("/info")
def anime_info(id: str = Query(...)):
    with cache_lock:
        if id in info_cache:
            return info_cache[id]

    html = get_html(f"{BASE_URL}/category/{id}")
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("div.anime_info_body_bg h1") or soup.select_one("h1")
    title = title_tag.get_text(strip=True) if title_tag else ""

    poster_tag = soup.select_one("div.anime_info_body_bg img") or soup.select_one("div.anime-info img")
    poster = poster_tag.get("src", "") if poster_tag else ""

    info = {}
    for p in soup.select("div.anime_info_body_bg p.type, div.anime-info p"):
        span = p.find("span")
        if span:
            label = span.get_text(strip=True).rstrip(":").lower()
            value = p.get_text(strip=True).replace(span.get_text(strip=True), "").strip()
            info[label] = value

    genres = [a.get_text(strip=True) for a in soup.select("p.type a[href*='genre']")]

    movie_id_tag = soup.select_one("#movie_id")
    movie_id = movie_id_tag.get("value") if movie_id_tag else None

    ep_pages = soup.select("#episode_page a")
    total_eps = None
    if ep_pages:
        try:
            total_eps = int(ep_pages[-1].get("ep_end", 0))
        except Exception:
            pass

    result = {
        "id": id,
        "title": title,
        "poster": poster,
        "genres": genres,
        "type": info.get("type"),
        "status": info.get("status"),
        "released": info.get("released"),
        "summary": info.get("plot summary"),
        "total_episodes": total_eps,
        "movie_id": movie_id,
        "url": f"{BASE_URL}/category/{id}",
    }

    with cache_lock:
        info_cache[id] = result
    return result


@app.get("/episodes")
def episodes(id: str = Query(...)):
    with cache_lock:
        if id in episodes_cache:
            return episodes_cache[id]

    html = get_html(f"{BASE_URL}/category/{id}")
    soup = BeautifulSoup(html, "html.parser")

    movie_id_tag = soup.select_one("#movie_id")
    if not movie_id_tag:
        raise HTTPException(status_code=404, detail=f"Anime not found: {id}")
    movie_id = movie_id_tag.get("value")

    ep_pages = soup.select("#episode_page a")
    if not ep_pages:
        raise HTTPException(status_code=404, detail="No episodes found")

    ep_start = ep_pages[0].get("ep_start", "0")
    ep_end = ep_pages[-1].get("ep_end", "0")

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
def stream(ep_id: str = Query(...)):
    with cache_lock:
        if ep_id in sources_cache:
            return sources_cache[ep_id]

    html = get_html(f"{BASE_URL}/{ep_id}")
    soup = BeautifulSoup(html, "html.parser")

    sources = []

    for li in soup.select("div.anime_muti_link ul li, div.list-server-items li"):
        a = li.select_one("a")
        if not a:
            continue
        server_name = a.get_text(strip=True) or li.get("class", ["unknown"])[0]
        data_video = a.get("data-video", "") or a.get("href", "")
        if data_video and data_video != "#":
            if not data_video.startswith("http"):
                data_video = "https:" + data_video
            sources.append({
                "server": server_name,
                "url": data_video,
            })

    default_embed = soup.select_one("div.play-video iframe, div.anime-video-body iframe")
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

    @app.get("/debug")
def debug(url: str = Query(...)):
    html = get_html(url)
    soup = BeautifulSoup(html, "html.parser")
    # Return first 2000 chars of the page and all class names found
    classes = list(set([c for tag in soup.find_all(class_=True) for c in tag.get("class", [])]))
    return {
        "url": url,
        "title": soup.title.get_text() if soup.title else "",
        "classes_found": sorted(classes)[:50],
        "html_preview": html[:2000],
    }
    return result

@app.get("/debug")
def debug(url: str = Query(...)):
    html = get_html(url)
    soup = BeautifulSoup(html, "html.parser")
    # Return first 2000 chars of the page and all class names found
    classes = list(set([c for tag in soup.find_all(class_=True) for c in tag.get("class", [])]))
    return {
        "url": url,
        "title": soup.title.get_text() if soup.title else "",
        "classes_found": sorted(classes)[:50],
        "html_preview": html[:2000],
    }
