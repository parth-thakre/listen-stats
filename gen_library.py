#!/usr/bin/env python3
"""Build window.LIBRARY: every listen, so the stats app can open any artist, album, track or day.

Listens are grouped by identity rather than by text. Clients submit the same song
under several spellings and credit lines, and only some listens have a MusicBrainz
mapping, so grouping by the raw strings splits one album into a dozen. Here:

- Artists are one per MusicBrainz ID. Unmapped names join an ID when a mapped
  listen used the same name (ignoring case, accents, spaces and punctuation).
- Albums merge on a shared release or release group, or on the same title (without
  soundtrack, deluxe and similar suffixes) when they share an artist. Albums with the
  same title and no shared artist still merge when their songs date from within two
  years of each other, unless the title is generic or the album is self-titled.
- Tracks merge on a shared recording ID, or on the same title (without "(From ...)"
  style suffixes) when they share an artist or an album.

A listen of the same song less than a minute after its previous one is dropped as a
double submission. Counts therefore differ slightly from PROFILE, which groups by
credit line and keeps every row.
"""
import collections
import json
import re
import sys
import unicodedata
from pathlib import Path

from gen_profile import country, genre_tags, prepare, release_year

ROOT = Path(__file__).resolve().parent
DOUBLE = 60
TRACK_SUFFIX = re.compile(r'\s*(?:[(\[](?:from|feat\.?|ft\.?|featuring)\b[^)\]]*[)\]]|\s-\s(?:from|feat\.?|ft\.?)\s.*)$', re.I)
GENERIC_ALBUMS = {'greatesthits', 'bestof', 'thebestof', 'hits', 'live', 'singles', 'remixes', 'unplugged',
                  'soundtrack', 'untitled', 'collection', 'thecollection', 'essentials', 'theessentials', 'love'}
ALBUM_SUFFIX = re.compile(
    r'\s*(?:[(\[][^)\]]*\b(?:soundtrack|ost|deluxe|edition|remaster(?:ed)?|expanded|version)\b[^)\]]*[)\]]'
    r'|\s-\s(?:original\s+motion\s+picture\s+soundtrack|soundtrack|ost|deluxe(?:\s+edition)?))\s*$',
    re.I,
)


def norm(text):
    """Case-, accent-, space- and punctuation-insensitive key; keeps non-Latin letters."""
    text = unicodedata.normalize('NFKD', str(text or '')).casefold()
    key = re.sub(r'[\W_]+', '', ''.join(c for c in text if not unicodedata.combining(c)))
    return key or str(text).strip()


class Groups:
    """Union-find over string node ids."""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[a] = b


def credit_of(listen, known):
    """[(name, mbid or None, mapped?)] for one listen, best source first."""
    tm = listen['track_metadata']
    mapping = tm.get('mbid_mapping') or {}
    mapped = [(a['artist_credit_name'], (a.get('artist_mbid') or '').lower() or None, True)
              for a in mapping.get('artists') or [] if a.get('artist_credit_name')]
    if mapped:
        return mapped
    info = tm.get('additional_info') or {}
    names, ids = info.get('artist_names') or [], info.get('artist_mbids') or []
    if names and len(names) == len(ids):
        return [(n, (m or '').lower() or None, True) for n, m in zip(names, ids)]
    # The whole credit line arrives as one name; split on bullets and commas, keeping
    # adjacent pieces whole when together they name a known artist ("Tyler, The Creator").
    out = []
    for piece in re.split(r'\s+[•·]\s+', tm.get('artist_name') or 'Unknown'):
        out += split_commas(piece, known, out)
    return out


def split_commas(text, known, taken):
    parts, out = text.split(', '), []
    while parts:
        if len(parts) > 1 and norm(f'{parts[0]}, {parts[1]}') in known:
            parts[:2] = [f'{parts[0]}, {parts[1]}']
        name = parts.pop(0).strip()
        if name and all(norm(name) != norm(n) for n, _, _ in out + taken):
            out.append((name, None, False))
    return out


def best(counter, preferred=()):
    """Most common value, favouring values from the preferred (mapped) set."""
    pool = collections.Counter({k: v for k, v in counter.items() if k in preferred}) or counter
    return pool.most_common(1)[0][0]


def merge_same_titles(rows, records, latest):
    """Join album groups that share a title and an era, e.g. the same soundtrack under two releases."""
    years, sizes, titles, self_titled = (collections.defaultdict(list), collections.Counter(),
                                         collections.defaultdict(set), set())
    for r in rows:
        if not r['album']:
            continue
        root = records.find(r['record'])
        sizes[root] += 1
        titles[r['album_key']].add(root)
        if any(norm(name) == r['album_key'] for name, _, _ in r['credit']):
            self_titled.add(root)
        year = release_year(r['listen']['_meta'], latest)
        if year:
            years[root].append(year)
    for title, roots in titles.items():
        roots = [x for x in roots if x not in self_titled]
        if len(roots) < 2 or title in GENERIC_ALBUMS:
            continue
        dated = sorted((sorted(years[x])[len(years[x]) // 2], x) for x in roots if years[x])
        undated = [x for x in roots if not years[x]]
        for (y1, a), (y2, b) in zip(dated, dated[1:]):
            if y2 - y1 <= 2:
                records.union(a, b)
        if undated:
            # Without a year, join the biggest group of that title.
            target = max(dated or [(0, x) for x in undated], key=lambda d: sizes[d[1]])[1]
            for x in undated:
                records.union(x, target)


def build_library(listens, generated_at):
    latest = listens[-1]['_t'].year
    known = {norm(name) for l in listens for name, mbid in l['_credit'] if mbid}
    people, songs, records = Groups(), Groups(), Groups()

    rows = []
    for l in listens:
        tm = l['track_metadata']
        mapping, info = tm.get('mbid_mapping') or {}, tm.get('additional_info') or {}
        credit = credit_of(l, known)
        nodes = []
        for name, mbid, _ in credit:
            node = f'n:{norm(name)}'
            if mbid:
                people.union(node, f'm:{mbid}')
            nodes.append(node)
        # Only submitted release IDs (from the player's own tags) prove album identity.
        # ListenBrainz's automatic mbid_mapping.release_mbid frequently points tracks at
        # unrelated compilations, which transitively merges distinct soundtracks together.
        rows.append(dict(
            listen=l, credit=credit, nodes=nodes,
            recording=(mapping.get('recording_mbid') or info.get('recording_mbid') or '').lower() or None,
            release=(info.get('release_mbid') or '').lower() or None,
            group=(info.get('release_group_mbid') or '').lower() or None,
            album=tm.get('release_name')))

    for r in rows:
        roots = [people.find(n) for n in r['nodes']]
        r['roots'] = roots
        if r['album']:
            name = norm(ALBUM_SUFFIX.sub('', r['album']))
            record = f"e:{r['release']}" if r['release'] else f'a:{name}|{roots[0]}'
            if r['release'] and r['group']:
                records.union(record, f"g:{r['group']}")
            if name not in GENERIC_ALBUMS:
                for root in roots:
                    records.union(record, f'a:{name}|{root}')
            r['record'] = record
            r['album_key'] = name
    merge_same_titles(rows, records, latest)

    for r in rows:
        roots = r['roots']
        title = norm(TRACK_SUFFIX.sub('', r['listen']['_tr']))
        song = f"r:{r['recording']}" if r['recording'] else f't:{title}|{roots[0]}'
        for root in roots:
            songs.union(song, f't:{title}|{root}')
        if r['album']:
            songs.union(song, f'ta:{title}|{records.find(r["record"])}')
        r['song'] = song

    # Assign indexes in order of first listen so the output is stable between runs.
    artist_ix, track_ix, album_ix = {}, {}, {}
    names, mapped_names, mbids, areas = (collections.defaultdict(collections.Counter), collections.defaultdict(set),
                                         {}, {})
    t_title, t_credit, t_mapped, t_cover, t_meta = (collections.defaultdict(collections.Counter),
                                                   collections.defaultdict(collections.Counter),
                                                   collections.defaultdict(set), {}, {})
    a_title, a_people, a_cover, a_release = (collections.defaultdict(collections.Counter),
                                             collections.defaultdict(collections.Counter), {}, {})
    out_rows = []
    for r in rows:
        l = r['listen']
        meta = l['_meta'] or {}
        place = {a['name']: country(a['area']) for a in meta.get('artists') or [] if a.get('area') and a.get('name')}
        credit_ix = []
        for (name, mbid, is_mapped), root in zip(r['credit'], r['roots']):
            i = artist_ix.setdefault(root, len(artist_ix))
            names[i][name] += 1
            if is_mapped:
                mapped_names[i].add(name)
            if mbid:
                mbids.setdefault(i, mbid)
            if place.get(name):
                areas.setdefault(i, place[name])
            if i not in credit_ix:
                credit_ix.append(i)
        t = track_ix.setdefault(songs.find(r['song']), len(track_ix))
        t_title[t][l['_tr']] += 1
        t_credit[t][tuple(credit_ix)] += 1
        if any(m for _, _, m in r['credit']):
            t_mapped[t].add(tuple(credit_ix))
        if l['_c'] and (t not in t_cover or not t_cover[t].startswith('assets/') and l['_c'].startswith('assets/')):
            t_cover[t] = l['_c']
        t_meta.setdefault(t, dict(length=l['_len'], year=release_year(meta, latest), genres=genre_tags(meta)))
        if not t_meta[t]['length'] and l['_len']:
            t_meta[t]['length'] = l['_len']
        al = -1
        if r['album']:
            al = album_ix.setdefault(records.find(r['record']), len(album_ix))
            a_title[al][r['album']] += 1
            for i in credit_ix:
                a_people[al][i] += 1
            if l['_c'] and (al not in a_cover or not a_cover[al].startswith('assets/') and l['_c'].startswith('assets/')):
                a_cover[al] = l['_c']
            if r['release']:
                a_release.setdefault(al, r['release'])
        row = [l['listened_at'], t, al, round(l['_m'] * 60)]
        if l.get('_source') == 'lastfm':
            row.append(1)
        out_rows.append(row)

    # A song logged again within a minute (or 30% of its length, if shorter) of its
    # previous play is the same play submitted twice, not a replay.
    kept, previous, doubles = [], {}, 0
    for row in out_rows:
        t = row[1]
        gap = min(DOUBLE, (t_meta[t]['length'] or 200_000) / 1000 * 0.3)
        if t in previous and row[0] - previous[t] < gap:
            doubles += 1
            continue
        previous[t] = row[0]
        kept.append(row)
    out_rows = kept

    # Tracks without art borrow their album's; albums without any art fall back to the
    # Cover Art Archive front image for their release (the page shows a letter if none).
    track_album = {}
    for _, t, al, *_ in out_rows:
        if al >= 0:
            track_album.setdefault(t, al)
    caa = lambda al: f'https://coverartarchive.org/release/{a_release[al]}/front-250' if al in a_release else None
    album_cover = lambda al: a_cover.get(al) or caa(al)
    genre_ix = {}
    artists = [None] * len(artist_ix)
    for i in range(len(artists)):
        artists[i] = [best(names[i], mapped_names[i]), mbids.get(i), areas.get(i)]
    tracks = [None] * len(track_ix)
    for t in range(len(tracks)):
        credit = list(best(t_credit[t], t_mapped[t]))
        line = ', '.join(artists[i][0] for i in credit[:3])
        al = track_album.get(t)
        tracks[t] = [best(t_title[t]), line, credit, t_cover.get(t) or (album_cover(al) if al is not None else None),
                     t_meta[t]['length'], t_meta[t]['year'],
                     [genre_ix.setdefault(g, len(genre_ix)) for g in t_meta[t]['genres']]]
    albums = [[best(a_title[al]), [i for i, _ in a_people[al].most_common(3)], album_cover(al)] for al in range(len(album_ix))]
    return dict(
        generated_at=generated_at,
        artists=artists,                      # [name, mbid, country]
        albums=albums,                        # [title, [top artist indexes], cover]
        tracks=tracks,                        # [title, credit line, [artist indexes], cover, ms, year, [genre indexes]]
        genres=list(genre_ix),
        listens=out_rows,                     # [unix time, track index, album index or -1, seconds, 1 if from Last.fm]
        sources=dict(listenbrainz=sum(len(r) == 4 for r in out_rows), lastfm=sum(len(r) == 5 for r in out_rows),
                     double_submissions_dropped=doubles),
    )


def main():
    audit_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '.state/latest-listens.json'
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'site'
    from lb_metadata import load_index
    report = json.loads(audit_path.read_text(encoding='utf-8'))
    metadata = {m: (e or {}).get('record') for m, e in load_index().items()}
    library = build_library(prepare(report, metadata), report['generated_at'])
    (output_dir / 'library.js').write_text('window.LIBRARY = ' + json.dumps(library, ensure_ascii=False, separators=(',', ':')) + ';\n',
                                           encoding='utf-8')
    print('library', len(library['listens']), 'listens;', len(library['tracks']), 'tracks;',
          len(library['albums']), 'albums;', len(library['artists']), 'artists')


if __name__ == '__main__':
    main()
