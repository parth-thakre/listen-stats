"""Short-lived Spotify portrait URLs from verified MusicBrainz artist links.

Photos stay on Spotify's CDN, not in the repository. Existing Wikimedia Commons
portraits always win; this fills artists without a Commons photo.
"""
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests

from artist_portraits import MBID, Resolver, BudgetExceeded, ROOT

ARTIST_URL = re.compile(r'https://open\.spotify\.com/artist/([A-Za-z0-9]{22})\Z')
THUMB_HOSTS = {'i.scdn.co', 'image-cdn-ak.spotifycdn.com', 'image-cdn-fa.spotifycdn.com'}
TTL = 86400
RETRY = 3600


def _read_response(session, url, *, params=None, limit=100_000):
    with session.get(url, params=params, timeout=(3, 8), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Unexpected Spotify oEmbed response')
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError('Spotify oEmbed response too large')
        return bytes(content)


def valid_record(record, mbid, now):
    if not isinstance(record, dict) or record.get('artist_mbid') != mbid:
        return False
    if not isinstance(record.get('artist_url'), str) or not ARTIST_URL.fullmatch(record['artist_url']):
        return False
    expires = record.get('expires_at')
    if not isinstance(expires, (int, float)) or not now < expires <= now + TTL:
        return False
    try:
        image = urlsplit(record.get('url', ''))
        return (image.scheme == 'https' and image.netloc in THUMB_HOSTS
                and re.fullmatch(r'/image/[A-Za-z0-9]+', image.path) is not None
                and not image.query and not image.fragment)
    except (ValueError, TypeError):
        return False


def resolve(resolver, mbid):
    artist = resolver.get(f'https://musicbrainz.org/ws/2/artist/{mbid}',
                          params={'inc': 'url-rels', 'fmt': 'json'})
    if artist.get('id') != mbid or artist.get('type') not in {None, 'Person', 'Group', 'Choir', 'Orchestra'}:
        raise ValueError('MusicBrainz artist identity mismatch')
    urls = {r.get('url', {}).get('resource') for r in artist.get('relations', [])
            if r.get('type') in {'free streaming', 'streaming', 'paid streaming'}
            and isinstance(r.get('url', {}).get('resource'), str)
            and ARTIST_URL.fullmatch(r['url']['resource'])}
    if len(urls) != 1:
        raise ValueError('No unambiguous Spotify artist link')
    source = urls.pop()
    data = json.loads(_read_response(resolver.session, 'https://open.spotify.com/oembed',
                                     params={'url': source}, limit=100_000))
    if not isinstance(data, dict):
        raise ValueError('Invalid Spotify metadata')
    embedded = urlsplit(data.get('iframe_url', ''))
    if (data.get('provider_name') != 'Spotify' or embedded.scheme != 'https'
            or embedded.netloc != 'open.spotify.com'
            or embedded.path != '/embed/artist/' + ARTIST_URL.fullmatch(source)[1]):
        raise ValueError('Spotify artist identity mismatch')
    record = {'artist_mbid': mbid, 'artist_url': source,
              'url': data.get('thumbnail_url'), 'expires_at': time.time() + TTL}
    if not valid_record(record, mbid, time.time()):
        raise ValueError('Untrusted Spotify portrait URL')
    return record


def enrich_spotify_portraits(rows, *, index_path=ROOT / '.state/spotify-portraits.json',
                             session=None, max_artists=35, seconds=60):
    index_path = Path(index_path)
    try:
        entries = json.loads(index_path.read_text(encoding='utf-8'))
        if not isinstance(entries, dict):
            entries = {}
    except (OSError, ValueError):
        entries = {}
    groups = {}
    for row in rows:
        row.pop('spotify_portrait', None)
        mbid = row.get('artist_mbid')
        if not row.get('portrait') and isinstance(mbid, str) and MBID.fullmatch(mbid):
            groups.setdefault(mbid, []).append(row)
    own = session is None
    session = session or requests.Session()
    session.headers.setdefault('User-Agent', 'listen-stats/1.0 (artist portraits)')
    resolver = Resolver(session, seconds=seconds)
    stats = dict(attempted=0, resolved=0, cached=0, errors=0)
    try:
        for mbid, artists in groups.items():
            entry = entries.get(mbid, {})
            if not isinstance(entry, dict):
                entry = {}
            record = entry.get('record')
            if valid_record(record, mbid, time.time()):
                stats['cached'] += 1
            else:
                record = None
                retry = entry.get('retry_after', 0)
                if (isinstance(retry, (int, float)) and retry > time.time()
                        or stats['attempted'] >= max_artists or time.monotonic() >= resolver.deadline):
                    continue
                stats['attempted'] += 1
                try:
                    record = resolve(resolver, mbid)
                    stats['resolved'] += 1
                    entries[mbid] = {'record': record}
                except BudgetExceeded:
                    resolver.deadline = 0
                except (requests.RequestException, OSError, ValueError, TypeError, KeyError) as error:
                    entries[mbid] = {'retry_after': time.time() + RETRY, 'reason': type(error).__name__}
                    stats['errors'] += 1
            if record:
                for row in artists:
                    row['spotify_portrait'] = record
        entries = {mid: entry for mid, entry in entries.items() if isinstance(entry, dict)
                   and (valid_record(entry.get('record'), mid, time.time())
                        or isinstance(entry.get('retry_after'), (int, float))
                        and entry['retry_after'] > time.time())}
        index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = index_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(entries), encoding='utf-8')
        temporary.chmod(0o600)
        os.replace(temporary, index_path)
    finally:
        if own:
            session.close()
    return stats
