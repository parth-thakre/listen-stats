#!/usr/bin/env python3
"""Export and compare ListenBrainz recording stats with the raw listens feed."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_ROOT = "https://api.listenbrainz.org/1"


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=6,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "listen-stats/1.0"
    return session


def get_json(session: requests.Session, url: str, params: dict[str, Any]) -> dict:
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_stats_recordings(session: requests.Session, username: str) -> tuple[list[dict], dict]:
    url = f"{API_ROOT}/stats/user/{username}/recordings"
    recordings: list[dict] = []
    offset = 0
    metadata: dict[str, Any] = {}

    while True:
        payload = get_json(
            session,
            url,
            {"range": "all_time", "count": 1000, "offset": offset},
        )["payload"]
        page = payload.get("recordings", [])
        recordings.extend(page)
        page_metadata = {key: value for key, value in payload.items() if key != "recordings"}
        if not metadata:
            metadata = page_metadata
        total = int(payload.get("total_recording_count", len(recordings)))
        print(f"Stats recordings: {len(recordings)}/{total}", flush=True)
        # The endpoint reports the full cardinality but exposes at most the top 1,000.
        if not page or len(recordings) >= min(total, 1000):
            break
        offset += len(page)
        time.sleep(0.2)

    return recordings, metadata


def fetch_raw_listens(session: requests.Session, username: str) -> list[dict]:
    url = f"{API_ROOT}/user/{username}/listens"
    listens: list[dict] = []
    max_ts: int | None = None
    seen: Counter = Counter()

    while True:
        params: dict[str, Any] = {"count": 1000}
        if max_ts is not None:
            params["max_ts"] = max_ts
        page = get_json(session, url, params)["payload"].get("listens", [])
        if not page:
            break

        page_seen: Counter = Counter()
        for listen in page:
            # Preserve identical records within a page, but don't count the
            # overlapping boundary page twice. A multiset matters for imports
            # that contain two identical listens in the same second.
            metadata = listen.get("track_metadata") or {}
            key = (listen.get("inserted_at"), int(listen["listened_at"]),
                   listen.get("recording_msid"),
                   json.dumps(metadata, sort_keys=True, ensure_ascii=False))
            page_seen[key] += 1
            if page_seen[key] > seen[key]:
                listens.append(listen)
        seen |= page_seen

        oldest = min(int(item["listened_at"]) for item in page)
        print(f"Raw listens: {len(listens)}", flush=True)
        if len(page) < 1000:
            break
        # max_ts is exclusive: include the oldest second again, so ties split
        # across pages survive. Fail closed if a whole page shares one second.
        next_max = oldest + 1
        if max_ts is not None and next_max >= max_ts:
            raise RuntimeError("Pagination made no progress; snapshot not replaced")
        max_ts = next_max
        time.sleep(0.2)

    return listens
