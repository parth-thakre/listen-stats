#!/usr/bin/env python3
"""Read matching music tags and copy thumbnails, never music, into the gallery.

Only files shortlisted by exact normalized filename, album or artist are probed.
The private mtime/size index avoids repeat probes and retains extracted artwork
when an SMB root is temporarily offline. No source paths enter listen metadata
or the returned report. Remote CAA remains the fallback for unmatched listens.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_ROOTS = tuple(Path(p) for p in os.environ.get('LB_LIBRARY_ROOTS', '').split(os.pathsep) if p)
ASSET_DIR = ROOT / 'site/assets/library'
INDEX_PATH = ROOT / '.state/library-art-index.json'
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wav', '.aiff', '.ape', '.wv'}
COVER_PATTERN = re.compile(r'assets/library/[0-9a-f]{64}\.jpg\Z')
MBID_PATTERN = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)


def normalize(value):
    """Case/punctuation normalization only; no approximate or substring match."""
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    # Combining marks (vowel signs, etc.) are identity: dropping them would
    # equate distinct words such as Devanagari 'मन' and 'मान'.
    return ''.join(c for c in value if c.isalnum() or unicodedata.category(c).startswith('M'))


def _same(left, right):
    """Exact normalized equality; a value that normalizes to '' never matches."""
    left = normalize(left)
    return bool(left) and left == normalize(right)


def valid_thumbnail(path):
    try:
        if path.is_symlink() or not 100 < path.stat().st_size < 2_000_000:
            return False
        with path.open('rb') as image:
            if image.read(3) != b'\xff\xd8\xff':
                return False
            image.seek(-2, 2)
            return image.read() == b'\xff\xd9'
    except OSError:
        return False


def trusted_local_cover(listen, asset_dir=ASSET_DIR):
    value = listen.get('_local_cover')
    if isinstance(value, str) and COVER_PATTERN.fullmatch(value):
        if valid_thumbnail(Path(asset_dir) / value.rsplit('/', 1)[1]):
            return value
    return None


def identity(listen):
    meta = listen.get('track_metadata') or {}
    info = meta.get('additional_info') or {}
    mapping = meta.get('mbid_mapping') or {}
    result = {key: meta.get(source) or '' for key, source in
              [('track', 'track_name'), ('artist', 'artist_name'), ('album', 'release_name')]}
    result['artists'] = info.get('artist_names') or [result['artist']]
    # Submitted IDs describe the user's own release, unlike automatic mappings.
    result['recording_mbid'] = info.get('recording_mbid') or mapping.get('recording_mbid') or ''
    for key in ('release_mbid', 'release_group_mbid'):
        result[key] = info.get(key) or ''
    return result


def public_identity(item):
    return {k: item[k] for k in ('track', 'artist', 'album', 'recording_mbid', 'release_mbid')}


def _ids(value):
    return set(s.lower() for s in MBID_PATTERN.findall(str(value or '')))


def match_identity(item, tags):
    """Return a proven match type, never a title-only or artist-only guess."""
    same_album = _same(item['album'], tags.get('album'))
    release_ids = _ids(item['release_mbid'])
    tagged_releases = _ids(tags.get('musicbrainzalbumid'))
    # A recording can appear on singles, compilations and unrelated reissues.
    # Its ID alone cannot establish which release cover the listener expects.
    # An album that normalizes to '' (e.g. '÷') is unknown: it neither confirms nor rejects.
    if normalize(item['album']) and normalize(tags.get('album')) and not same_album:
        return None
    if release_ids and tagged_releases and not release_ids & tagged_releases:
        return None
    if release_ids & tagged_releases:
        return 'release_mbid'
    if same_album and _ids(item['recording_mbid']) & _ids(tags.get('musicbrainztrackid')):
        return 'recording_mbid'
    listed = {normalize(a) for a in item['artists']} - {''}
    tagged = {normalize(a) for a in re.split(r';|\x00| • ', tags.get('artists') or tags.get('artist') or '')} - {''}
    same_artist = _same(item['artist'], tags.get('artist')) or bool(listed and listed == tagged)
    if same_artist and same_album and _same(item['track'], tags.get('title')):
        return 'exact_track_artist_album'
    return None


def _probe(path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format_tags:stream=index,codec_type:stream_disposition=attached_pic:stream_tags',
         '-of', 'json', str(path)], capture_output=True, timeout=20, check=True)
    data = json.loads(result.stdout)
    streams = data.get('streams') or []
    pictures = [s for s in streams if (s.get('disposition') or {}).get('attached_pic')]
    # Ogg/Opus keep Vorbis comments on the audio stream rather than the container.
    audio = next((s for s in streams if s.get('codec_type') == 'audio' and s not in pictures), None)
    merged = {**((audio or {}).get('tags') or {}), **((data.get('format') or {}).get('tags') or {})}
    tags = {normalize(k): str(v) for k, v in merged.items()}
    pictures.sort(key=lambda s: 'front' not in str(s.get('tags', {})).casefold())
    return {'tags': tags, 'pictures': [s['index'] for s in pictures]}


def _sources(path, record):
    for stream in record.get('pictures', []):
        yield path, ['-map', f'0:{stream}'], 'embedded'
    # Folder art is considered only after the audio's identity has been verified.
    try:
        files = {p.name.casefold(): p for p in path.parent.iterdir() if not p.is_symlink()}
    except OSError:
        return
    for stem in ('cover', 'folder', 'front', 'album', 'albumart'):
        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
            image = files.get(stem + ext)
            if image:
                yield image, [], 'folder'


def _extract(path, record, asset_dir):
    for source, mapping, kind in _sources(path, record):
        try:
            stamp = source.stat()
            key = f'{source}\0{stamp.st_mtime_ns}\0{stamp.st_size}\0{mapping}\0thumbnail-v1'
            name = hashlib.sha256(key.encode('utf-8', 'surrogateescape')).hexdigest() + '.jpg'
            target = asset_dir / name
            if not valid_thumbnail(target):
                with tempfile.NamedTemporaryFile(suffix='.jpg', dir=asset_dir, delete=False) as temp:
                    temporary = Path(temp.name)
                try:
                    subprocess.run(
                        ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source), *mapping,
                         '-frames:v', '1', '-vf', 'scale=400:400:force_original_aspect_ratio=decrease',
                         '-map_metadata', '-1', '-an', '-c:v', 'mjpeg', '-q:v', '3',
                         '-threads', '1', str(temporary)],
                        capture_output=True, timeout=30, check=True)
                    if not valid_thumbnail(temporary):
                        continue
                    temporary.chmod(0o644)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return {'cover': 'assets/library/' + name, 'source': kind}
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _reachable(directory):
    """Listing triggers SMB autofs mounts, whereas exists() alone may not."""
    try:
        with os.scandir(directory):
            return True
    except OSError:
        return False


def _enumerate(roots, items):
    """Return (shortlisted audio paths, roots reachable this run)."""
    tracks = {normalize(i['track']) for i in items} - {''}
    albums = {normalize(i['album']) for i in items} - {''}
    artists = {normalize(i['artist']) for i in items} - {''}
    paths = []
    reachable = []
    for root in roots:
        root = Path(root)
        if not _reachable(root):
            continue
        reachable.append(root)
        for directory, subdirs, filenames in os.walk(root, followlinks=False):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith('.') and not (Path(directory) / d).is_symlink())
            parent = Path(directory)
            album = normalize(re.sub(r'\s*\(\d{4}\)$', '', parent.name))
            artist = normalize(parent.parent.name)
            for name in sorted(filenames):
                path = parent / name
                if path.suffix.lower() not in AUDIO_EXTENSIONS or path.is_symlink():
                    continue
                # Both stems are checked so '99 Problems' is not reduced to 'Problems'.
                stripped = normalize(re.sub(r'^\d+(?:[-.]\d+)?[\s._-]+', '', path.stem))
                if normalize(path.stem) in tracks or stripped in tracks or album in albums or artist in artists:
                    paths.append(path)
    return paths, reachable


def _valid_record(record):
    return (isinstance(record, dict)
            and isinstance(record.get('tags', {}), dict)
            and isinstance(record.get('pictures', []), list)
            and isinstance(record.get('artwork'), (dict, type(None))))


def enrich_listens(listens, *, asset_dir=ASSET_DIR, index_path=INDEX_PATH, roots=DEFAULT_ROOTS):
    """Prefer verified local release thumbnails over automatic CAA mappings."""
    asset_dir, index_path = Path(asset_dir), Path(index_path)
    roots = tuple(Path(root) for root in roots)
    asset_dir.mkdir(parents=True, exist_ok=True)
    index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        index = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        index = {}
    if not isinstance(index, dict) or index.get('version') != 1 or not isinstance(index.get('files'), dict):
        index = {}
    records = {k: v for k, v in index.get('files', {}).items() if _valid_record(v)}
    index = {'version': 1, 'files': records}
    groups = {}
    for listen in listens:
        item = identity(listen)
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        groups.setdefault(key, {'identity': item, 'listens': []})['listens'].append(listen)
        # Only this module's validated index is authoritative, not input fields.
        listen.pop('_local_cover', None)
        listen.pop('_local_cover_match', None)
    paths, reachable = _enumerate(roots, [g['identity'] for g in groups.values()]) if groups else ([], [])
    available = len(reachable)
    online = set(reachable)
    probes = 0
    errors = 0
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            # A root vanishing mid-run must not destroy its cached records.
            errors += 1
            continue
        signature = [stat.st_mtime_ns, stat.st_size]
        record = records.get(str(path))
        if record and record.get('signature') == signature:
            continue
        try:
            probes += 1
            record = _probe(path)
            record['signature'] = signature
            records[str(path)] = record
        except (OSError, ValueError, subprocess.SubprocessError):
            records.pop(str(path), None)
            errors += 1
    # Cached entries may be reused offline, but never from a removed/configured-out root.
    # Under a reachable root, a record whose file is gone or changed is stale and dropped;
    # unshortlisted files are only stat'ed, never probed.
    shortlisted = {str(p) for p in paths}
    allowed = []
    for key in list(records):
        path = Path(key)
        root = next((r for r in roots if path.is_relative_to(r)), None)
        if root is None:
            continue
        if root in online and key not in shortlisted:
            try:
                stat = path.stat()
            except OSError:
                if _reachable(root):
                    records.pop(key)
                    continue
                online.discard(root)  # went offline mid-run: keep its cache
            else:
                if records[key].get('signature') != [stat.st_mtime_ns, stat.st_size]:
                    records.pop(key)
                    continue
        allowed.append(path)
    ordered = list(dict.fromkeys([*paths, *allowed]))
    matched, unresolved = [], []
    for group in groups.values():
        item = group['identity']
        candidates = []
        for path in ordered:
            record = records.get(str(path))
            if not record:
                continue
            method = match_identity(item, record.get('tags', {}))
            if method:
                candidates.append(({'release_mbid': 0, 'exact_track_artist_album': 1, 'recording_mbid': 2}[method], path, record, method))
        candidates.sort(key=lambda c: c[0])
        found = None
        for _, path, record, method in candidates:
            artwork = record.get('artwork')
            stale = not artwork or not trusted_local_cover({'_local_cover': artwork.get('cover')}, asset_dir)
            # Folder art can be replaced without touching the audio file, so it is
            # re-derived whenever the folder is reachable; offline keeps the cached cover.
            if stale or (artwork.get('source') == 'folder' and _reachable(path.parent)):
                fresh = _extract(path, record, asset_dir)
                if fresh:
                    artwork = record['artwork'] = fresh
                elif stale:
                    artwork = None
            if artwork:
                found = {**artwork, 'match': method}
                break
        if found:
            for listen in group['listens']:
                listen['_local_cover'] = found['cover']
                listen['_local_cover_match'] = {'method': found['match'], 'source': found['source']}
            matched.append({**public_identity(item), **found, 'listens': len(group['listens'])})
        else:
            unresolved.append({**public_identity(item), 'reason': 'matched_audio_has_no_readable_art' if candidates else 'no_exact_local_match', 'listens': len(group['listens'])})
    # ensure_ascii keeps surrogate-escaped (non-UTF-8) filenames encodable and round-trippable.
    payload = json.dumps(index, ensure_ascii=True)
    # mkstemp creates the file 0600 from the start; it is never world-readable.
    fd, temporary = tempfile.mkstemp(prefix=index_path.name + '.', suffix='.tmp', dir=index_path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(payload)
        os.replace(temporary, index_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'matched': matched, 'unresolved': unresolved, 'matched_identities': len(matched),
            'unresolved_identities': len(unresolved), 'matched_listens': sum(x['listens'] for x in matched),
            'roots_available': available, 'probed_files': probes, 'probe_errors': errors}
