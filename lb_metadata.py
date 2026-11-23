"""Recording metadata from the ListenBrainz metadata API, cached privately.

Only MBIDs already present in the user's own listens are looked up. The index
keeps a trimmed record per recording: credited artists with area and career
start, the recording's first release date and length, the release's year and
cover identifiers, and genre tags. Provider failures leave the index as it was.
"""
import json
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
API_ROOT = 'https://api.listenbrainz.org/1'
INDEX_PATH = ROOT / '.state/recording-metadata.json'
BATCH = 50
TTL = 30 * 86400


def recording_mbids(listens):
    seen = {}
    for listen in listens:
        mbid = (listen.get('track_metadata', {}).get('mbid_mapping') or {}).get('recording_mbid')
        if isinstance(mbid, str) and len(mbid) == 36:
            seen.setdefault(mbid.lower(), True)
    return list(seen)


def _genres(rows):
    out = {}
    for row in rows or []:
        tag = row.get('tag')
        if isinstance(tag, str) and row.get('genre_mbid'):
            out[tag] = max(out.get(tag, 0), int(row.get('count') or 0))
    return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))[:6]


def trim(record):
    """Keep only the fields the gallery uses; drop external links and credits."""
    artist = record.get('artist') or {}
    recording = record.get('recording') or {}
    release = record.get('release') or {}
    tag = record.get('tag') or {}
    return {
        'artists': [{
            'mbid': a.get('artist_mbid'), 'name': a.get('name'), 'area': a.get('area'),
            'begin_year': a.get('begin_year'), 'type': a.get('type'),
        } for a in artist.get('artists') or [] if isinstance(a, dict)],
        'first_release_date': recording.get('first_release_date'),
        'length': recording.get('length'),
        'release': {
            'name': release.get('name'), 'year': release.get('year'), 'caa_id': release.get('caa_id'),
            'caa_release_mbid': release.get('caa_release_mbid'),
            'release_group_mbid': release.get('release_group_mbid'),
        },
        'genres': {
            'recording': _genres(tag.get('recording')),
            'release_group': _genres(tag.get('release_group')),
            'artist': _genres(tag.get('artist')),
        },
    }


def load_index(path=INDEX_PATH):
    try:
        index = json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(index, dict) and index.get('version') == 1 and isinstance(index.get('recordings'), dict):
            return index['recordings']
    except (OSError, ValueError):
        pass
    return {}


def save_index(entries, path=INDEX_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'recordings': entries}, ensure_ascii=False), encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def fetch_metadata(session, listens, *, index_path=INDEX_PATH, budget=40, timeout=(8, 30)):
    """Fill missing or stale entries within a bounded number of requests."""
    entries = load_index(index_path)
    wanted = recording_mbids(listens)
    now = time.time()
    missing = [m for m in wanted if not isinstance(entries.get(m), dict)
               or entries[m].get('fetched', 0) + TTL < now]
    stats = dict(known=len(wanted), missing=len(missing), fetched=0, requests=0, errors=0)
    for start in range(0, len(missing), BATCH):
        if stats['requests'] >= budget:
            break
        batch = missing[start:start + BATCH]
        stats['requests'] += 1
        try:
            response = session.get(f'{API_ROOT}/metadata/recording/',
                                   params={'recording_mbids': ','.join(batch), 'inc': 'artist tag release'},
                                   timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Unexpected metadata payload')
        except (requests.RequestException, ValueError) as error:
            stats['errors'] += 1
            print(f'Metadata batch failed ({type(error).__name__}); keeping cached entries', flush=True)
            if stats['errors'] >= 3:
                break
            continue
        for mbid in batch:
            record = payload.get(mbid)
            entries[mbid] = {'fetched': now, 'record': trim(record) if isinstance(record, dict) else None}
            stats['fetched'] += 1
    try:
        save_index(entries, index_path)
    except OSError:
        stats['errors'] += 1
    return {m: (entries.get(m) or {}).get('record') for m in wanted}, stats


def fetch_similar_users(session, user, timeout=(8, 20)):
    try:
        response = session.get(f'{API_ROOT}/user/{user}/similar-users', timeout=timeout)
        response.raise_for_status()
        rows = response.json().get('payload') or []
        return [{'user': r['user_name'], 'similarity': round(float(r['similarity']), 3)}
                for r in rows if isinstance(r, dict) and isinstance(r.get('user_name'), str)][:12]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
