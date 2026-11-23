#!/usr/bin/env python3
"""Last.fm scrobbles that ListenBrainz is missing, shaped like ListenBrainz listens.

Navidrome scrobbles to both services, but some submissions only reached Last.fm.
Last.fm records track start time while ListenBrainz often records completion time
(typically 2 to 10 minutes later for long tracks). A scrobble therefore counts as
already on ListenBrainz when a listen with the same title sits within [-120s, +660s]
of it (within a minute unconditionally, or with a shared artist across the wider
window). A leftover in that window with a shared artist and similar title is matched
as the same play under a spelling/transliteration difference. Each ListenBrainz
listen absorbs one scrobble, so a song played twice in a row keeps both plays.

Leftover scrobbles are dropped only when ListenBrainz already has the exact same
title within 90 seconds (a second client double-scrobbling the same play) or when
the next Last.fm scrobble starts under 30 seconds later (a skip). Different songs
starting immediately after an earlier ListenBrainz listen finished are kept.

The key comes from LASTFM_API_KEY or ~/.config/listen-stats/lastfm.env. Scrobbles are
cached in .state so a refresh only asks Last.fm for new pages.
"""
import bisect
import collections
import difflib
import json
import os
import re
import time
from pathlib import Path

from gen_library import TRACK_SUFFIX, norm

API = 'https://ws.audioscrobbler.com/2.0/'
USER = os.environ.get('LASTFM_USER') or os.environ.get('LB_USER') or ''
KEY_FILE = Path.home() / '.config/listen-stats/lastfm.env'
WINDOW_BEFORE = 120
WINDOW_AFTER = 660
SURE = 60
DUPLICATE = 90
SKIP = 30
# Last.fm serves this star image when a track has no artwork.
NO_ART = '2a96cbd8b46e442fc41c2b86b821562f'
RESCAN = 86400


def api_key():
    key = os.environ.get('LASTFM_API_KEY')
    if not key and KEY_FILE.is_file():
        for line in KEY_FILE.read_text(encoding='utf-8').splitlines():
            if line.startswith('LASTFM_API_KEY='):
                key = line.split('=', 1)[1].strip().strip('"\'')
    return key


def fetch_scrobbles(session, cache_path, user=USER, key=None, now=None):
    """All scrobbles as [uts, track, artist, album, track mbid, image], oldest first.

    New pages are fetched incrementally; the whole history is re-read once a day so
    edits and deletions on Last.fm are picked up.
    """
    key = key or api_key()
    if not key:
        raise RuntimeError('No Last.fm API key')
    now = now or time.time()
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.is_file() else {}
    full = now - cache.get('scanned_at', 0) > RESCAN or not cache.get('scrobbles')
    since = None if full else cache['scrobbles'][-1][0]
    fresh, page, pages = [], 1, 1
    while page <= pages:
        params = dict(method='user.getrecenttracks', user=user, api_key=key, format='json', limit=200, page=page)
        if since:
            params['from'] = since + 1
        try:
            response = session.get(API, params=params, timeout=30)
            response.raise_for_status()
        except Exception as err:
            status = getattr(getattr(err, 'response', None), 'status_code', None)
            detail = f'HTTP {status}' if status else type(err).__name__
            raise RuntimeError(f'Last.fm request failed ({detail})') from None
        block = response.json().get('recenttracks') or {}
        pages = int((block.get('@attr') or {}).get('totalPages') or 1)
        tracks = block.get('track') or []
        # A single result arrives as an object rather than a list.
        for t in [tracks] if isinstance(tracks, dict) else tracks:
            if (t.get('@attr') or {}).get('nowplaying') == 'true' or not (t.get('date') or {}).get('uts'):
                continue
            image = next((i.get('#text') for i in reversed(t.get('image') or []) if i.get('#text')), None)
            fresh.append([int(t['date']['uts']), t.get('name') or '', (t.get('artist') or {}).get('#text') or '',
                          (t.get('album') or {}).get('#text') or None, t.get('mbid') or None,
                          None if not image or NO_ART in image else image])
        page += 1
        time.sleep(0.25)
    rows = fresh if full else cache['scrobbles'] + fresh
    rows = sorted({tuple(r[:3]): r for r in rows}.values(), key=lambda r: r[0])
    cache_path.write_text(json.dumps(dict(scanned_at=now if full else cache['scanned_at'], scrobbles=rows),
                                     ensure_ascii=False), encoding='utf-8')
    return rows


def names(text):
    return {norm(p) for p in re.split(r'\s*(?:,|;|&|•|·|\bfeat\.?|\bft\.?)\s*', text or '') if p.strip()}


def same_artist(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na or bool(names(a) & names(b))


def similar_title(a, b):
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 4
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.65


def missing(lb_listens, scrobbles):
    """(scrobbles with no matching ListenBrainz listen, as listens; counts per outcome)."""
    lb = sorted(lb_listens, key=lambda l: l['listened_at'])
    times = [l['listened_at'] for l in lb]
    by_title = collections.defaultdict(list)
    lb_info = []
    for i, l in enumerate(lb):
        tm = l['track_metadata']
        artists = [tm.get('artist_name') or ''] + [a.get('artist_credit_name') or ''
                                                   for a in (tm.get('mbid_mapping') or {}).get('artists') or []]
        title_key = norm(TRACK_SUFFIX.sub('', tm.get('track_name') or ''))
        by_title[title_key].append((l['listened_at'], i, artists))
        lb_info.append((l['listened_at'], i, title_key, artists))

    def near_range(rows, lo_ts, hi_ts):
        return rows[bisect.bisect_left(rows, lo_ts, key=lambda r: r[0]):
                    bisect.bisect_right(rows, hi_ts, key=lambda r: r[0])]

    used, outcome = set(), {}
    # Pass 1: same title in [-WINDOW_BEFORE, +WINDOW_AFTER] (LFM logs start, LB often logs end).
    for k, (uts, track, artist, *_) in enumerate(scrobbles):
        rows = by_title.get(norm(TRACK_SUFFIX.sub('', track)), [])
        fits = [r for r in near_range(rows, uts - WINDOW_BEFORE, uts + WINDOW_AFTER) if r[1] not in used
                and (abs(r[0] - uts) <= SURE or any(same_artist(a, artist) for a in r[2]))]
        if fits:
            used.add(min(fits, key=lambda r: abs(r[0] - uts))[1])
            outcome[k] = 'matched'
    # Pass 2: a leftover in the window with a shared artist and similar title is the same
    # play under a transliteration or spelling difference—never an unrelated adjacent song.
    for k, (uts, track, artist, *_) in enumerate(scrobbles):
        if k in outcome:
            continue
        title_key = norm(TRACK_SUFFIX.sub('', track))
        lo = bisect.bisect_left(times, uts - WINDOW_BEFORE)
        hi = bisect.bisect_right(times, uts + WINDOW_AFTER)
        fits = [lb_info[i] for i in range(lo, hi) if i not in used
                and any(same_artist(a, artist) for a in lb_info[i][3])
                and similar_title(title_key, lb_info[i][2])]
        if fits:
            used.add(min(fits, key=lambda r: abs(r[0] - uts))[1])
            outcome[k] = 'retitled'
    out, counts = [], collections.Counter(outcome.values())
    for k, (uts, track, artist, album, mbid, image) in enumerate(scrobbles):
        if k in outcome:
            continue
        title_key = norm(TRACK_SUFFIX.sub('', track))
        if near_range(by_title.get(title_key, []), uts - DUPLICATE, uts + DUPLICATE):
            counts['duplicate'] += 1
        elif k + 1 < len(scrobbles) and scrobbles[k + 1][0] - uts < SKIP:
            counts['skip'] += 1
        else:
            # Last.fm carries the recording ID Navidrome submitted; metadata lookups key on it.
            out.append(dict(listened_at=uts, _source='lastfm', _lastfm_cover=image, track_metadata=dict(
                track_name=track, artist_name=artist, release_name=album, additional_info={},
                mbid_mapping={'recording_mbid': mbid} if mbid else {})))
    counts['missing'] = len(out)
    return out, counts


def lastfm_extras(session, lb_listens, cache_path, user=None):
    """(missing listens, counts). Without a session, only the cache is used."""
    if session is None:
        cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.is_file() else {}
        scrobbles = cache.get('scrobbles') or []
    else:
        scrobbles = fetch_scrobbles(session, cache_path, user=user or USER)
    extra, counts = missing(lb_listens, scrobbles)
    counts['scrobbles'] = len(scrobbles)
    return extra, counts
