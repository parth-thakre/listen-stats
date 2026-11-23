"""Verified iTunes Search API fallback for albums and tracks with no local or CAA cover art.

Only accepts results whose normalized title and artist both agree with the library entry.
In remote_only mode (for public builds), returns direct Apple CDN HTTPS URLs without
downloading or redistributing image files; otherwise saves WebP thumbnails locally under
site/assets/covers/. Results are cached in .state/itunes-art.json.
"""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time

import requests
from PIL import Image

from cache_gallery_art import PUBLIC, STATE, is_trusted_cover_url
from gen_library import ALBUM_SUFFIX, TRACK_SUFFIX, norm
from lastfm_listens import same_artist

SEARCH_URL = "https://itunes.apple.com/search"
INDEX_PATH = STATE / "itunes-art.json"
MISS_TTL = 7 * 86400
SIZE_RE = re.compile(r"/\d+x\d+bb\.(?:jpg|jpeg|png)$", re.I)
# Apple's Search API rate limit is ~20 requests/min; stay just under to avoid 429 bursts.
MIN_INTERVAL = 3.1


def _clean_title(title, is_album):
    stripped = (ALBUM_SUFFIX if is_album else TRACK_SUFFIX).sub("", str(title or ""))
    stripped = re.sub(r"\s*(?:[(-]\s*)?(?:www\.)?[a-z0-9-]+\.(?:com|pk|in|net)\)?", "", stripped, flags=re.I)
    return stripped.strip()


def _title_matches(wanted, candidate, is_album):
    nw = norm(_clean_title(wanted, is_album))
    nc = norm(_clean_title(candidate, is_album))
    if not nw or not nc:
        return False
    if nw == nc:
        return True
    short, long_ = (nw, nc) if len(nw) <= len(nc) else (nc, nw)
    return len(short) >= 5 and short in long_ and len(short) / len(long_) >= 0.45


def _artist_matches(wanted_artists, candidate_artist):
    if not candidate_artist:
        return False
    if not wanted_artists:
        return True
    return any(same_artist(w, candidate_artist) for w in wanted_artists if w)


def _upgrade_artwork(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    upgraded = SIZE_RE.sub("/600x600bb.jpg", url)
    return upgraded if is_trusted_cover_url(upgraded) else None


def _download_webp(session, url, asset_dir, public):
    target = asset_dir / (sha256(url.encode()).hexdigest()[:24] + ".webp")
    if target.is_file():
        return str(target.relative_to(public))
    with session.get(url, timeout=(8, 20), stream=True) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 5_000_000:
                raise ValueError("Artwork larger than 5 MB")
    with Image.open(BytesIO(content)) as image:
        image.thumbnail((512, 512))
        tmp = target.with_suffix(".tmp")
        image.convert("RGB").save(tmp, "WEBP", quality=88)
        os.replace(tmp, target)
    return str(target.relative_to(public))


def _parse_year(value):
    s = str(value or "")[:4]
    return int(s) if s.isdigit() and 1900 <= int(s) <= 2100 else None


def _query_terms(clean, wanted_artists):
    """Try primary artist first, then additional credited artists (e.g. soundtrack composer vs singer)."""
    terms = []
    seen = set()
    candidates = [a.strip() for a in wanted_artists if isinstance(a, str) and a.strip()]
    for artist in candidates[:3]:
        # Split combined strings like "Shail Hada & A. M. Turaz" so iTunes search stays focused.
        first_name = re.split(r"\s*(?:,|;|&|\bfeat\.?|\bft\.?)\s*", artist, maxsplit=1)[0].strip()
        if not first_name:
            continue
        term = f"{clean} {first_name}".strip()
        if term.casefold() not in seen:
            seen.add(term.casefold())
            terms.append(term)
    if not terms and clean:
        terms.append(clean)
    return terms


def enrich_library_itunes(library, *, public=None, index_path=None, max_lookups=40, remote_only=False):
    """Fill missing album and track covers (and missing year/duration) via verified iTunes lookups."""
    public = Path(public) if public else PUBLIC
    asset_dir = public / "assets" / "covers"
    if not remote_only:
        asset_dir.mkdir(parents=True, exist_ok=True)
    index_path = Path(index_path) if index_path else INDEX_PATH
    now = time.time()
    try:
        cache = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}

    artists = library.get("artists") or []
    albums = library.get("albums") or []
    tracks = library.get("tracks") or []
    listens = library.get("listens") or []

    al_plays, tr_plays = {}, {}
    track_to_album, album_to_tracks = {}, {}
    for _, t, al, *_ in listens:
        tr_plays[t] = tr_plays.get(t, 0) + 1
        if al >= 0:
            al_plays[al] = al_plays.get(al, 0) + 1
            track_to_album.setdefault(t, al)
            album_to_tracks.setdefault(al, set()).add(t)

    stats = {"cached_hits": 0, "fetched_hits": 0, "misses": 0, "attempted": 0, "requests": 0, "errors": 0}
    session = requests.Session()
    session.headers["User-Agent"] = "listen-stats/1.0"
    last_req = 0.0
    rate_limited = False

    def api_search(term, entity, country):
        nonlocal last_req, rate_limited
        for attempt in range(2):
            wait = MIN_INTERVAL - (time.monotonic() - last_req)
            if wait > 0:
                time.sleep(wait)
            last_req = time.monotonic()
            stats["requests"] += 1
            try:
                resp = session.get(
                    SEARCH_URL,
                    params={"term": term, "media": "music", "entity": entity, "limit": 8, "country": country},
                    timeout=(6, 12),
                )
                if resp.status_code == 429:
                    if attempt == 0:
                        time.sleep(15)
                        continue
                    rate_limited = True
                    return None, "rate_limited"
                resp.raise_for_status()
                return (resp.json() or {}).get("results") or [], "ok"
            except (requests.RequestException, ValueError):
                return None, "error"
        return None, "error"

    def lookup(title, wanted_artists, *, is_album):
        entity = "album" if is_album else "song"
        clean = _clean_title(title, is_album)
        terms = _query_terms(clean, wanted_artists)
        had_error = False
        for term in terms:
            for country in ("IN", "US"):
                if rate_limited:
                    return None, "rate_limited"
                results, status = api_search(term, entity, country)
                if status == "rate_limited":
                    return None, "rate_limited"
                if status != "ok":
                    had_error = True
                    continue
                for item in results:
                    cand_title = item.get("collectionName" if is_album else "trackName") or ""
                    cand_artist = item.get("artistName") or ""
                    if not _title_matches(title, cand_title, is_album):
                        continue
                    if not _artist_matches(wanted_artists, cand_artist):
                        continue
                    art_url = _upgrade_artwork(item.get("artworkUrl100"))
                    if art_url:
                        return {
                            "url": art_url,
                            "year": _parse_year(item.get("releaseDate")),
                            "length": item.get("trackTimeMillis") if isinstance(item.get("trackTimeMillis"), int) else None,
                        }, "hit"
                # If country=IN had results for this term, try next artist term before falling back to US
                if country == "IN" and len(terms) > 1 and term != terms[-1]:
                    break
        return None, ("error" if had_error else "miss")

    def resolve(kind, title, wanted_artists):
        is_album = kind == "album"
        clean = _clean_title(title, is_album)
        primary_artist = wanted_artists[0] if wanted_artists else ""
        key = f"v2:{kind}:{norm(clean)}|{norm(primary_artist)}"
        entry = cache.get(key)
        if not isinstance(entry, dict):
            # Reuse verified hits from v1 cache keys, but re-check v1 misses with multi-artist queries.
            legacy = cache.get(f"{kind}:{norm(clean)}|{norm(primary_artist)}")
            if isinstance(legacy, dict) and (legacy.get("url") or legacy.get("cover")):
                entry = legacy
        if isinstance(entry, dict):
            if remote_only:
                url = entry.get("url")
                if url and is_trusted_cover_url(url):
                    stats["cached_hits"] += 1
                    return url, entry.get("year"), entry.get("length")
                if not url and now - entry.get("checked_at", 0) < MISS_TTL:
                    return None, None, None
            else:
                cover = entry.get("cover")
                if cover and (public / cover).is_file():
                    stats["cached_hits"] += 1
                    return cover, entry.get("year"), entry.get("length")
                if not cover and now - entry.get("checked_at", 0) < MISS_TTL:
                    return None, None, None
        if rate_limited or (max_lookups is not None and stats["attempted"] >= max_lookups):
            return None, None, None
        if not clean:
            return None, None, None
        stats["attempted"] += 1
        hit, status = lookup(title, wanted_artists, is_album=is_album)
        if status == "rate_limited" or status == "error":
            stats["errors"] += 1
            return None, None, None
        if hit:
            art_url = hit["url"]
            if remote_only:
                cache[key] = {"url": art_url, "year": hit["year"], "length": hit["length"], "checked_at": now}
                stats["fetched_hits"] += 1
                return art_url, hit["year"], hit["length"]
            try:
                rel = _download_webp(session, art_url, asset_dir, public)
                cache[key] = {
                    "cover": rel, "url": art_url, "year": hit["year"], "length": hit["length"], "checked_at": now
                }
                stats["fetched_hits"] += 1
                return rel, hit["year"], hit["length"]
            except (requests.RequestException, OSError, ValueError):
                stats["errors"] += 1
                return None, None, None
        cache[key] = {"cover": None, "url": None, "checked_at": now}
        stats["misses"] += 1
        return None, None, None

    def apply_album(i):
        wanted = [artists[a][0] for a in albums[i][1] if a < len(artists)]
        cover, year, _ = resolve("album", albums[i][0], wanted)
        if cover:
            albums[i][2] = cover
            for t in album_to_tracks.get(i, ()):
                if not tracks[t][3]:
                    tracks[t][3] = cover
                if year and not tracks[t][5]:
                    tracks[t][5] = year

    def apply_track(i):
        wanted = [artists[a][0] for a in tracks[i][2] if a < len(artists)]
        if not wanted and tracks[i][1]:
            wanted = [tracks[i][1]]
        cover, year, length = resolve("track", tracks[i][0], wanted)
        if cover:
            tracks[i][3] = cover
            if length and not tracks[i][4]:
                tracks[i][4] = length
            if year and not tracks[i][5]:
                tracks[i][5] = year
            al = track_to_album.get(i)
            if al is not None and not albums[al][2]:
                albums[al][2] = cover

    try:
        # Prioritize multi-play albums and tracks first so the most visible pages always get art.
        missing_albums = sorted([i for i, a in enumerate(albums) if not a[2]], key=lambda i: -al_plays.get(i, 0))
        for i in [i for i in missing_albums if al_plays.get(i, 0) >= 2]:
            apply_album(i)

        missing_tracks = sorted([i for i, t in enumerate(tracks) if not t[3]], key=lambda i: -tr_plays.get(i, 0))
        for i in [i for i in missing_tracks if tr_plays.get(i, 0) >= 2]:
            if not tracks[i][3]:
                apply_track(i)

        for i in [i for i in missing_albums if al_plays.get(i, 0) < 2]:
            if not albums[i][2]:
                apply_album(i)

        for i in [i for i in missing_tracks if tr_plays.get(i, 0) < 2]:
            if not tracks[i][3]:
                apply_track(i)

        for al, tset in album_to_tracks.items():
            if not albums[al][2]:
                best_t = max(tset, key=lambda t: (bool(tracks[t][3]), tr_plays.get(t, 0)))
                if tracks[best_t][3]:
                    albums[al][2] = tracks[best_t][3]
    finally:
        session.close()

    try:
        index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, index_path)
    except OSError:
        pass
    return stats
