"""Cache release-art URLs locally as WebP thumbnails so browsers never hit flaky remote hosts."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests
from PIL import Image

PUBLIC = Path(__file__).resolve().parent / "site"
STATE = Path(__file__).resolve().parent / ".state"
FALLBACK = "assets/cover-unavailable.svg"
ALLOWED_HOSTS = {
    "archive.org",
    "coverartarchive.org",
    "lastfm.freetls.fastly.net",
    "lastfm-img.freetls.fastly.net",
}
MZSTATIC_RE = re.compile(r"^is\d+-ssl\.mzstatic\.com$", re.I)
FAILURE_TTL = 86400


def is_trusted_cover_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return host in ALLOWED_HOSTS or bool(MZSTATIC_RE.match(host))


def _save_one(url, directory, public, *, failures=None, now=None, allow_download=True):
    target = directory / (sha256(url.encode()).hexdigest()[:24] + ".webp")
    if target.is_file():
        return url, str(target.relative_to(public)), False
    parsed = urlsplit(url)
    if not parsed.scheme and not parsed.netloc:
        local = (public / parsed.path).resolve()
        if (local.is_relative_to((public / "assets").resolve()) and local.is_file()
                and local.suffix.lower() in {".webp", ".jpg", ".jpeg", ".png", ".svg"}):
            return url, str(local.relative_to(public)), False
        return url, None, False
    if not is_trusted_cover_url(url):
        return url, None, False
    if failures is not None and now is not None:
        failed_at = failures.get(url)
        if failed_at and now - failed_at < FAILURE_TTL:
            return url, None, False
    if not allow_download:
        return url, url, False
    try:
        with requests.get(url, timeout=(8, 20), stream=True,
                          headers={"User-Agent": "listen-stats/1.0"}) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 5_000_000:
                    raise ValueError("Cover larger than 5 MB")
        with Image.open(BytesIO(content)) as image:
            image.thumbnail((512, 512))
            temporary = target.with_suffix(".tmp")
            image.convert("RGB").save(temporary, "WEBP", quality=88)
            os.replace(temporary, target)
        return url, str(target.relative_to(public)), True
    except (requests.RequestException, OSError, ValueError) as error:
        print(f"Cover unavailable ({type(error).__name__}): {url}", flush=True)
        if failures is not None and now is not None:
            failures[url] = now
        return url, None, True


def cache_art(*snapshots):
    directory = PUBLIC / "assets" / "covers"
    directory.mkdir(parents=True, exist_ok=True)
    urls = set()

    def walk(value, replace=None):
        if isinstance(value, dict):
            if "cover" in value:
                url = value["cover"]
                if replace is not None:
                    value["cover"] = replace.get(url, FALLBACK)
                elif isinstance(url, str) and url:
                    urls.add(url)
            for child in value.values():
                walk(child, replace)
        elif isinstance(value, list):
            for child in value:
                walk(child, replace)

    walk(list(snapshots))

    def save(url):
        _, resolved, _ = _save_one(url, directory, PUBLIC)
        return url, (resolved or FALLBACK)

    with ThreadPoolExecutor(max_workers=4) as pool:
        mapping = dict(pool.map(save, sorted(urls)))
    walk(list(snapshots), mapping)
    saved = sum(path != FALLBACK for path in mapping.values())
    print(f"Release artwork: {saved}/{len(urls)} cached; unavailable covers use a placeholder", flush=True)


def cache_library_art(library, *, public=None, failure_path=None, max_downloads=60, workers=6):
    """Cache remote cover URLs referenced by LIBRARY albums and tracks into assets/covers/.

    Returns stats dict. Failed downloads are cleared to None so iTunes fallback or
    the client's empty-state monogram handles them cleanly instead of timing out in-browser.
    """
    public = Path(public) if public else PUBLIC
    directory = public / "assets" / "covers"
    directory.mkdir(parents=True, exist_ok=True)
    failure_path = Path(failure_path) if failure_path else (STATE / "cover-failures.json")
    now = time.time()
    try:
        failures = json.loads(failure_path.read_text(encoding="utf-8")) if failure_path.is_file() else {}
        if not isinstance(failures, dict):
            failures = {}
    except (OSError, ValueError):
        failures = {}

    urls = []
    seen = set()
    for album in library.get("albums", ()):
        u = album[2]
        if isinstance(u, str) and u and u not in seen:
            seen.add(u)
            urls.append(u)
    for track in library.get("tracks", ()):
        u = track[3]
        if isinstance(u, str) and u and u not in seen:
            seen.add(u)
            urls.append(u)

    mapping = {}
    to_fetch = []
    for u in urls:
        parsed = urlsplit(u)
        if not parsed.scheme and not parsed.netloc:
            _, resolved, _ = _save_one(u, directory, public)
            mapping[u] = resolved
            continue
        target = directory / (sha256(u.encode()).hexdigest()[:24] + ".webp")
        if target.is_file():
            mapping[u] = str(target.relative_to(public))
            failures.pop(u, None)
            continue
        if not is_trusted_cover_url(u):
            mapping[u] = None
            continue
        failed_at = failures.get(u)
        if failed_at and now - failed_at < FAILURE_TTL:
            mapping[u] = None
            continue
        to_fetch.append(u)

    batch = to_fetch if max_downloads is None else to_fetch[:max_downloads]
    skipped = to_fetch[len(batch):]
    for u in skipped:
        mapping[u] = u

    fetched_ok = 0
    fetched_fail = 0
    if batch:
        def worker(url):
            return _save_one(url, directory, public, failures=failures, now=now, allow_download=True)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for url, resolved, _ in pool.map(worker, batch):
                mapping[url] = resolved
                if resolved:
                    fetched_ok += 1
                    failures.pop(url, None)
                else:
                    fetched_fail += 1

    # Prune stale failure timestamps and persist atomically.
    failures = {u: ts for u, ts in failures.items() if now - ts < FAILURE_TTL * 7}
    try:
        failure_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = failure_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(failures), encoding="utf-8")
        os.replace(tmp, failure_path)
    except OSError:
        pass

    for album in library.get("albums", ()):
        if album[2] in mapping:
            album[2] = mapping[album[2]]
    for track in library.get("tracks", ()):
        if track[3] in mapping:
            track[3] = mapping[track[3]]

    cached_local = sum(1 for v in mapping.values() if isinstance(v, str) and v.startswith("assets/"))
    return {
        "total_urls": len(urls),
        "cached_local": cached_local,
        "fetched_ok": fetched_ok,
        "fetched_fail": fetched_fail,
        "deferred": len(skipped),
    }
