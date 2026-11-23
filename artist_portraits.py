"""Artist-only portraits from MBID -> MusicBrainz -> Wikidata P18 -> Commons.

Release artwork is never a candidate. The private index caches successes and
misses; only resized JPEGs and their public attribution reach the gallery.
"""
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit

import requests
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / 'site'
INDEX_PATH = ROOT / '.state/artist-portraits.json'
MBID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
PORTRAIT = re.compile(r'assets/portraits/([0-9a-f]{64})\.jpg\Z')
SUCCESS_TTL = 30 * 86400
MISS_TTL = 86400
ERROR_TTL = 300
CHANGES = 'Resized to a maximum of 400 pixels and converted to JPEG; metadata removed.'


def artist_name(listen):
    """Use exactly the generators' first-credit grouping, not all collaborators."""
    meta = listen['track_metadata']
    artists = (meta.get('mbid_mapping') or {}).get('artists')
    return artists[0]['artist_credit_name'] if artists else meta.get('artist_name', 'Unknown')


def artist_identities(listens):
    """A name cohort needs one consistent first-credit MBID, with no unknowns."""
    candidates = {}
    for listen in listens:
        name = artist_name(listen)
        artists = (listen['track_metadata'].get('mbid_mapping') or {}).get('artists') or []
        mbid = artists[0].get('artist_mbid') if artists else None
        mbid = mbid.lower() if isinstance(mbid, str) else None
        if not mbid or not MBID.fullmatch(mbid):
            mbid = None
        candidates.setdefault(name, set()).add(mbid)
    return {name: next(iter(ids)) if len(ids) == 1 else None for name, ids in candidates.items()}


def portrait_fields(mbid=None):
    return dict(artist_mbid=mbid, portrait=None, portrait_source=None, portrait_attribution=None)


def safe_url(value, hosts=None):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and bool(url.hostname) and not url.username
                and not url.password and url.port in (None, 443)
                and (hosts is None or url.hostname in hosts))
    except ValueError:
        return False


def commons_image_filename(url):
    if not safe_url(url, {'thumb.wikimedia.org', 'upload.wikimedia.org'}):
        return None
    match = re.fullmatch(r'/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/]+)(?:/[^/]+)?',
                         unquote(urlsplit(url).path))
    return match[1].replace('_', ' ') if match else None


def commons_title(url):
    if not safe_url(url, {'commons.wikimedia.org'}):
        return None
    path = unquote(urlsplit(url).path)
    return path[6:].replace('_', ' ') if path.startswith('/wiki/File:') else None


class CreditText(HTMLParser):
    """Publish text, never provider HTML; keep credit links as separate safe URLs."""
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.parts, self.links = [], []
        self.feed(str(value or ''))

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        self.parts.append(' ')
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = 'https://commons.wikimedia.org' + href
            # Preserve HTTP source links as attribution, but never fetch them.
            if safe_url(href) or (href.startswith('http://') and safe_url('https://' + href[7:])):
                self.links.append(href)

    @property
    def text(self):
        return ' '.join(' '.join(self.parts).split())


def claims(entity, prop):
    rows = [r for r in entity.get('claims', {}).get(prop, []) if r.get('rank') != 'deprecated']
    preferred = [r for r in rows if r.get('rank') == 'preferred']
    return [r.get('mainsnak', {}).get('datavalue', {}).get('value') for r in preferred or rows]


def license_info(metadata):
    name = CreditText(metadata.get('LicenseShortName', {}).get('value')).text
    url = metadata.get('LicenseUrl', {}).get('value', '')
    if url.startswith('//'):
        url = 'https:' + url
    if url.startswith('http://creativecommons.org/'):
        url = 'https://' + url[7:]
    if not safe_url(url, {'creativecommons.org'}):
        raise ValueError('No supported reusable license')
    path = urlsplit(url).path.rstrip('/')
    cc = re.fullmatch(r'/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)'
                      r'(?:/([a-z]{2}))?(?:/deed\.[a-z-]+)?', path)
    public = path in {'/publicdomain/zero/1.0', '/publicdomain/mark/1.0'}
    expected = (f'CC {cc[1].upper()} {cc[2]}' + (f' {cc[3]}' if cc[3] else '') if cc
                else ('CC0 1.0' if '/zero/' in path else 'Public domain'))
    if not (cc or public) or name.casefold() != expected.casefold():
        raise ValueError('Unsupported or inconsistent license')
    return name, url


class BudgetExceeded(Exception):
    pass


class ProviderError(requests.RequestException):
    """An unusable provider response, not evidence that a portrait is invalid."""


class Resolver:
    """Serial GETs with one bounded transient retry, never redirects."""
    def __init__(self, session, seconds=90, max_requests=112):
        self.session = session
        self.deadline = time.monotonic() + seconds
        self.remaining = max_requests
        self.last_mb = 0

    def get(self, url, *, params=None, image=False):
        for attempt in range(2):
            try:
                return self._get_once(url, params=params, image=image)
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as error:
                response = getattr(error, 'response', None)
                status = getattr(response, 'status_code', None)
                if attempt or (status is not None and status != 429 and not 500 <= status < 600):
                    raise
                delay = 1.1
                retry_after = response.headers.get('Retry-After') if response is not None else None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        # Do not retry early when the server supplied an HTTP date.
                        raise error
                if time.monotonic() + delay >= self.deadline or self.remaining <= 0:
                    raise
                time.sleep(delay)

    def _get_once(self, url, *, params=None, image=False):
        hosts = {'thumb.wikimedia.org', 'upload.wikimedia.org'} if image else {
            'musicbrainz.org', 'www.wikidata.org', 'commons.wikimedia.org'}
        if not safe_url(url, hosts):
            raise ValueError('Untrusted portrait endpoint')
        if image and not commons_image_filename(url):
            raise ValueError('Not a Commons image path')
        now = time.monotonic()
        if urlsplit(url).hostname == 'musicbrainz.org':
            delay = max(0, 1.1 - (now - self.last_mb))
            if now + delay >= self.deadline:
                raise BudgetExceeded()
            time.sleep(delay)
            self.last_mb = time.monotonic()
        left = self.deadline - time.monotonic()
        if self.remaining <= 0 or left <= 0:
            raise BudgetExceeded()
        self.remaining -= 1
        with self.session.get(url, params=params, timeout=(min(3, left), min(8, left)),
                              stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Redirected or empty portrait response')
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > (4_000_000 if image else 3_000_000):
                    raise ValueError('Portrait response too large')
                if time.monotonic() >= self.deadline:
                    raise BudgetExceeded()
            if image:
                return bytes(content)
            try:
                payload = json.loads(content)
            except ValueError as error:
                raise ProviderError('Invalid provider JSON') from error
            if not isinstance(payload, dict) or 'error' in payload or 'errors' in payload:
                raise ProviderError('Provider API error response')
            return payload

    def resolve(self, mbid, public):
        artist = self.get(f'https://musicbrainz.org/ws/2/artist/{mbid}',
                          params={'inc': 'url-rels', 'fmt': 'json'})
        if artist.get('id') != mbid or artist.get('type') not in {'Person', 'Group', 'Choir', 'Orchestra'}:
            raise ValueError('MusicBrainz artist identity mismatch')
        targets = set()
        for relation in artist.get('relations', []):
            url = relation.get('url', {}).get('resource', '')
            if relation.get('type') == 'wikidata' and safe_url(url, {'www.wikidata.org', 'wikidata.org'}):
                match = re.fullmatch(r'/wiki/(Q[1-9][0-9]*)', urlsplit(url).path)
                if match:
                    targets.add(match[1])
        if len(targets) != 1:
            raise ValueError('No unambiguous Wikidata relation')
        qid = targets.pop()
        entity = self.get(f'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json')['entities'][qid]
        if entity.get('id') != qid or set(claims(entity, 'P434')) != {mbid}:
            raise ValueError('Wikidata MusicBrainz identity mismatch')
        # Several photos of one verified artist are not ambiguous identities.
        # Preserve Wikidata's preferred-rank ordering and try a bounded number.
        files = list(dict.fromkeys(f for f in claims(entity, 'P18') if isinstance(f, str)))
        if not files:
            raise ValueError('No Wikidata image')
        last_error = None
        for filename in files[:3]:
            try:
                return self.resolve_photo(mbid, qid, filename, public)
            except ValueError as error:
                last_error = error
        raise last_error

    def resolve_photo(self, mbid, qid, filename, public):
        mb_url = f'https://musicbrainz.org/artist/{mbid}'
        if not isinstance(filename, str) or Path(filename).suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp'}:
            raise ValueError('Not a photographic image format')
        result = self.get('https://commons.wikimedia.org/w/api.php', params={
            'action': 'query', 'format': 'json', 'prop': 'imageinfo', 'titles': 'File:' + filename,
            'iiprop': 'url|extmetadata|mime|size', 'iiurlwidth': 400, 'iilimit': 1,
            'iiextmetadatalanguage': 'en'})
        pages = list(result.get('query', {}).get('pages', {}).values())
        title = 'File:' + filename.replace('_', ' ')
        if len(pages) != 1 or pages[0].get('title') != title or pages[0].get('imagerepository') != 'local':
            raise ValueError('Commons file identity mismatch')
        info = pages[0].get('imageinfo', [{}])[0]
        source = info.get('descriptionurl')
        if commons_title(source) != title or info.get('mime') not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise ValueError('Untrusted Commons source')
        metadata = info.get('extmetadata', {})
        # P18 is an artist image, but reject obvious non-photo exceptions too.
        description = ' '.join(str(metadata.get(k, {}).get('value', '')) for k in ('ImageDescription', 'Categories'))
        if re.search(r'\b(album[ _-]cover|album[ _-]art|record[ _-]sleeve|logo|logotype|poster)\b',
                     filename + ' ' + description, re.I):
            raise ValueError('Commons image is not an artist photograph')
        license_name, license_url = license_info(metadata)
        author = CreditText(metadata.get('Artist', {}).get('value'))
        credit = CreditText(metadata.get('Credit', {}).get('value'))
        attribution = CreditText(metadata.get('Attribution', {}).get('value'))
        if not author.text:
            raise ValueError('Missing photograph author')
        restrictions = CreditText(metadata.get('Restrictions', {}).get('value')).text
        if restrictions not in ('', 'personality'):
            raise ValueError('Photograph has additional restrictions')
        # Commons uses the unscaled file as thumburl when it is already small.
        # Accept only this file, still bound bytes and resize before publication.
        thumbnail = info.get('thumburl', '')
        if commons_image_filename(thumbnail) != filename.replace('_', ' '):
            raise ValueError('Commons thumbnail filename mismatch')
        content = self.get(thumbnail, image=True)
        with Image.open(BytesIO(content)) as image:
            if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 20_000_000:
                raise ValueError('Invalid photograph')
            image = ImageOps.exif_transpose(image)
            image.thumbnail((400, 400))
            output = BytesIO()
            image.convert('RGB').save(output, 'JPEG', quality=88)
        content = output.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        relative = f'assets/portraits/{digest}.jpg'
        target = public / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o644)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return dict(artist_mbid=mbid, portrait=relative, portrait_source=source, portrait_attribution={
            'artist': author.text, 'credit': credit.text, 'attribution': attribution.text,
            'attribution_urls': sorted(set(author.links + credit.links + attribution.links)),
            'license': license_name, 'license_url': license_url, 'source_url': source,
            'musicbrainz_url': mb_url, 'wikidata_url': f'https://www.wikidata.org/wiki/{qid}',
            'image_title': title, 'changes': CHANGES, 'restrictions': restrictions})


def valid_cached(record, mbid, public):
    """Validate local paths, content and provenance before republishing cache entries."""
    if not isinstance(record, dict) or record.get('artist_mbid') != mbid:
        return False
    if not isinstance(record.get('portrait'), str):
        return False
    match = PORTRAIT.fullmatch(record['portrait'])
    attribution = record.get('portrait_attribution')
    if not match or not isinstance(attribution, dict):
        return False
    if not all(isinstance(attribution.get(key), str) for key in
               ('artist', 'image_title', 'wikidata_url', 'license', 'license_url', 'changes')):
        return False
    if (commons_title(record.get('portrait_source')) != attribution.get('image_title')
            or not commons_title(record.get('portrait_source'))
            or attribution.get('source_url') != record['portrait_source']
            or attribution.get('musicbrainz_url') != f'https://musicbrainz.org/artist/{mbid}'
            or not re.fullmatch(r'https://www\.wikidata\.org/wiki/Q[1-9][0-9]*', attribution.get('wikidata_url', ''))
            or not attribution.get('artist') or attribution.get('changes') != CHANGES):
        return False
    try:
        license_info({'LicenseShortName': {'value': attribution.get('license')},
                      'LicenseUrl': {'value': attribution.get('license_url', '')}})
        path = public / record['portrait']
        if path.is_symlink() or not path.resolve().is_relative_to(public.resolve()) or path.stat().st_size > 1_000_000:
            return False
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != match[1]:
            return False
        with Image.open(BytesIO(content)) as image:
            return image.format == 'JPEG' and max(image.size) <= 400
    except (OSError, ValueError):
        return False


def enrich_portraits(data, day, listens, *, public=PUBLIC, index_path=INDEX_PATH,
                     seconds=90, max_artists=28, session=None, extra=None):
    """Enrich displayed artists only; failures leave null or verified cached portraits.

    `extra` rows already carry a verified artist_mbid (credited artists from the
    profile pages) and keep it; the name-grouped rows are re-identified here.
    """
    public, index_path = Path(public), Path(index_path)
    identities = artist_identities(listens)
    rows = [*data.get('top_artists', []), *day.get('artists', [])]
    for row in rows:
        row.update(portrait_fields(identities.get(row['name'])))
    for row in extra or []:
        mbid = row.get('artist_mbid')
        mbid = mbid.lower() if isinstance(mbid, str) and MBID.fullmatch(mbid.lower()) else None
        row.update(portrait_fields(mbid))
        rows.append(row)
    stats = dict(resolved=0, cached=0, unavailable=0, attempted=0, errors=0)
    try:
        index = json.loads(index_path.read_text(encoding='utf-8'))
        entries = index['artists'] if isinstance(index, dict) and index.get('version') == 1 else {}
        if not isinstance(entries, dict):
            entries = {}
    except (OSError, ValueError, KeyError, TypeError):
        entries = {}
    own_session = session is None
    session = session or requests.Session()
    session.headers.update({'User-Agent': 'listen-stats/1.0 (artist portraits)'})
    resolver = Resolver(session, seconds=seconds)
    results = {}
    failures = 0
    try:
        for mbid in dict.fromkeys(row['artist_mbid'] for row in rows if row['artist_mbid']):
            entry = entries.get(mbid, {})
            if not isinstance(entry, dict):
                entry = {}
            record = entry.get('record')
            good = valid_cached(record, mbid, public)
            if good:
                results[mbid] = record
                stats['cached'] += 1
            retry = entry.get('retry_after', 0)
            obsolete_miss = entry.get('reason') == 'No unambiguous Wikidata image'
            if not obsolete_miss and isinstance(retry, (int, float)) and retry > time.time() and (good or not record):
                continue
            if stats['attempted'] >= max_artists or failures >= 3 or time.monotonic() >= resolver.deadline:
                continue
            stats['attempted'] += 1
            try:
                record = resolver.resolve(mbid, public)
                results[mbid] = record
                entries[mbid] = {'record': record, 'retry_after': time.time() + SUCCESS_TTL}
                stats['resolved'] += 1
                failures = 0
            except BudgetExceeded:
                # Continue visiting entries so later cached portraits still publish.
                resolver.deadline = 0
            except (requests.RequestException, OSError) as error:
                # Offline/5xx/429 errors retain last known-good photos, but a
                # removed source must not keep serving a stale portrait forever.
                status = getattr(getattr(error, 'response', None), 'status_code', None)
                removed = status in (404, 410)
                if removed:
                    results.pop(mbid, None)
                entries[mbid] = {'record': record if good and not removed else None,
                                 'retry_after': time.time() + (MISS_TTL if removed else ERROR_TTL),
                                 'reason': f'HTTP {status}' if status else type(error).__name__}
                failures += not removed
                stats['errors'] += 1
            except (ValueError, KeyError, TypeError, IndexError, Image.DecompressionBombError) as error:
                # Changed/invalid identity or licensing must not revive old photos.
                results.pop(mbid, None)
                entries[mbid] = {'record': None, 'retry_after': time.time() + MISS_TTL,
                                 'reason': str(error)[:160] or type(error).__name__}
        for row in rows:
            record = results.get(row['artist_mbid'])
            if record:
                row.update({key: record[key] for key in portrait_fields()})
        stats['unavailable'] = sum(row['portrait'] is None for row in rows)
        try:
            index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = index_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version': 1, 'artists': entries}, ensure_ascii=False), encoding='utf-8')
            temporary.chmod(0o600)
            os.replace(temporary, index_path)
        except OSError:
            stats['errors'] += 1
    finally:
        if own_session:
            session.close()
    return stats
