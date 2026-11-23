#!/usr/bin/env python3
"""Build window.PROFILE from raw listens plus cached recording metadata.

Unlike gen_data.py, artists here are counted the way ListenBrainz does: every
credited artist on a listen receives that listen. Hours use the configured timezone.
"""
import collections
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
RANGES = (('week', 'Last 7 days'), ('month', 'Last 30 days'), ('year', 'This year'), ('all', 'All time'))


COUNTRY_OF = {
    'Mumbai': 'India', 'Maharashtra': 'India', 'New Delhi': 'India', 'Delhi': 'India', 'Kolkata': 'India',
    'Calcutta': 'India', 'Chennai': 'India', 'Madras': 'India', 'Bengaluru': 'India', 'Bangalore': 'India',
    'Hyderabad': 'India', 'Pune': 'India', 'West Bengal': 'India', 'Tamil Nadu': 'India', 'Karnataka': 'India',
    'Kerala': 'India', 'Punjab': 'India', 'Gujarat': 'India', 'Uttar Pradesh': 'India', 'Bihar': 'India',
    'Andhra Pradesh': 'India', 'Telangana': 'India', 'Rajasthan': 'India', 'Goa': 'India', 'Jaipur': 'India',
    'Lucknow': 'India', 'Ahmedabad': 'India', 'Amritsar': 'India', 'Chandigarh': 'India', 'Patna': 'India',
    'Lahore': 'Pakistan', 'Karachi': 'Pakistan', 'Islamabad': 'Pakistan', 'Faisalabad': 'Pakistan',
    'Dhaka': 'Bangladesh', 'Kathmandu': 'Nepal', 'Colombo': 'Sri Lanka',
    'London': 'United Kingdom', 'England': 'United Kingdom', 'Scotland': 'United Kingdom', 'Wales': 'United Kingdom',
    'Northern Ireland': 'United Kingdom', 'Manchester': 'United Kingdom', 'Liverpool': 'United Kingdom',
    'Birmingham': 'United Kingdom', 'Glasgow': 'United Kingdom', 'Dublin': 'Ireland',
    'New York': 'United States', 'Los Angeles': 'United States', 'California': 'United States', 'Texas': 'United States',
    'Florida': 'United States', 'Georgia': 'United States', 'Illinois': 'United States', 'Chicago': 'United States',
    'Nashville': 'United States', 'Tennessee': 'United States', 'Atlanta': 'United States', 'Washington': 'United States',
    'Michigan': 'United States', 'Detroit': 'United States', 'Pennsylvania': 'United States', 'Philadelphia': 'United States',
    'New Jersey': 'United States', 'Massachusetts': 'United States', 'Boston': 'United States', 'Ohio': 'United States',
    'Minnesota': 'United States', 'Seattle': 'United States', 'Miami': 'United States', 'Houston': 'United States',
    'Brooklyn': 'United States', 'Manhattan': 'United States', 'Hawaii': 'United States', 'Arizona': 'United States',
    'Colorado': 'United States', 'Oregon': 'United States', 'Nevada': 'United States', 'Las Vegas': 'United States',
    'Seoul': 'South Korea', 'Busan': 'South Korea', 'Tokyo': 'Japan', 'Osaka': 'Japan', 'Toronto': 'Canada',
    'Ontario': 'Canada', 'Quebec': 'Canada', 'Vancouver': 'Canada', 'Montreal': 'Canada', 'Stockholm': 'Sweden',
    'Sydney': 'Australia', 'Melbourne': 'Australia', 'Auckland': 'New Zealand', 'Berlin': 'Germany', 'Paris': 'France',
    'Amsterdam': 'Netherlands', 'Oslo': 'Norway', 'Copenhagen': 'Denmark', 'Madrid': 'Spain', 'Barcelona': 'Spain',
    'Rome': 'Italy', 'Milan': 'Italy', 'Lisbon': 'Portugal', 'Kingston': 'Jamaica', 'Lagos': 'Nigeria',
    'Mexico City': 'Mexico', 'Buenos Aires': 'Argentina', 'São Paulo': 'Brazil', 'Rio de Janeiro': 'Brazil',
    'Jammu and Kashmir': 'India', 'Reims': 'France', 'Cambridge': 'United Kingdom', 'Bristol': 'United Kingdom',
    'Olympia': 'United States', 'Sacramento': 'United States', 'Dallas': 'United States', 'Oxford': 'United Kingdom',
}


def country(area):
    return COUNTRY_OF.get(area, area)


def caa(mbid, caa_id):
    if mbid and caa_id:
        return f'https://archive.org/download/mbid-{mbid}/mbid-{mbid}-{caa_id}_thumb250.jpg'
    return None


def cover_of(listen, meta):
    from local_artwork import trusted_local_cover
    local = trusted_local_cover(listen)
    if local:
        return local
    mapping = listen['track_metadata'].get('mbid_mapping') or {}
    remote = caa(mapping.get('caa_release_mbid'), mapping.get('caa_id'))
    if remote:
        return remote
    release = (meta or {}).get('release') or {}
    return caa(release.get('caa_release_mbid'), release.get('caa_id'))


def credited(listen):
    mapping = listen['track_metadata'].get('mbid_mapping') or {}
    rows = [(a.get('artist_credit_name'), (a.get('artist_mbid') or '').lower() or None)
            for a in mapping.get('artists') or [] if a.get('artist_credit_name')]
    return rows or [(listen['track_metadata'].get('artist_name', 'Unknown'), None)]


def minutes(listen, meta):
    info = listen['track_metadata'].get('additional_info') or {}
    if info.get('duration_ms'):
        return info['duration_ms'] / 60000
    if info.get('duration'):
        return info['duration'] / 60
    if (meta or {}).get('length'):
        return meta['length'] / 60000
    return 3.9


def first_cover(rows):
    return next((r['_c'] for r in rows if r['_c']), None)


def rank_block(rows, total_minutes=None):
    """Top artists (credited), albums and tracks for one slice of listens."""
    artists, albums, tracks = collections.Counter(), collections.Counter(), collections.Counter()
    amin = collections.Counter()
    aid, acover, alcover, tcover, tlen, tartist = {}, {}, {}, {}, {}, {}
    for l in rows:
        for name, mbid in l['_credit']:
            artists[name] += 1
            amin[name] += l['_m']
            aid.setdefault(name, mbid)
            if l['_c']:
                acover.setdefault(name, l['_c'])
        album = l['track_metadata'].get('release_name')
        if album:
            key = (album, l['_a'])
            albums[key] += 1
            if l['_c']:
                alcover.setdefault(key, l['_c'])
        key = (l['_tr'], l['_a'])
        tracks[key] += 1
        if l['_c']:
            tcover.setdefault(key, l['_c'])
        tlen.setdefault(key, l['_len'])
    return dict(
        listens=len(rows),
        minutes=round(sum(l['_m'] for l in rows)),
        artists=len(artists), albums=len(albums), tracks=len(tracks),
        top_artists=[dict(name=name, count=count, minutes=round(amin[name]), artist_mbid=aid.get(name),
                          portrait=None, portrait_source=None, portrait_attribution=None)
                     for name, count in artists.most_common(20)],
        top_albums=[dict(album=album, artist=artist, count=count, cover=alcover.get((album, artist)))
                    for (album, artist), count in albums.most_common(20)],
        top_tracks=[dict(track=track, artist=artist, count=count, cover=tcover.get((track, artist)),
                         length=tlen.get((track, artist)))
                    for (track, artist), count in tracks.most_common(20)],
    )


def release_year(meta, latest):
    """The recording's first release year, else the release year; None if implausible."""
    meta = meta or {}
    year = str(meta.get('first_release_date') or '')[:4]
    if not year.isdigit():
        year = str((meta.get('release') or {}).get('year') or '')[:4]
    return int(year) if year.isdigit() and 1900 <= int(year) <= latest else None


def genre_tags(meta):
    """Recording tags first, then release group, then artist tags; at most three."""
    g = (meta or {}).get('genres') or {}
    return [t for t, _ in (g.get('recording') or g.get('release_group') or g.get('artist') or [])[:3]]


def prepare(report, metadata):
    """Sorted listens annotated with the credit, cover and length fields both builders use."""
    listens = sorted(report['raw_listens'], key=lambda x: x['listened_at'])
    if not listens:
        raise ValueError('No listens')
    for l in listens:
        mapping = l['track_metadata'].get('mbid_mapping') or {}
        meta = metadata.get((mapping.get('recording_mbid') or '').lower())
        l['_meta'] = meta
        l['_t'] = datetime.fromtimestamp(l['listened_at'], IST)
        l['_credit'] = credited(l)
        l['_a'] = ', '.join(name for name, _ in l['_credit'][:3]) if len(l['_credit']) > 1 else l['_credit'][0][0]
        l['_tr'] = l['track_metadata'].get('track_name', '?')
        l['_c'] = cover_of(l, meta)
        l['_m'] = minutes(l, meta)
        l['_len'] = (meta or {}).get('length')
    return listens


def build_profile(report, metadata, similar=None, listens=None):
    listens = listens or prepare(report, metadata)
    last = listens[-1]['_t']
    end_day = last.date()
    ranges = {}
    for key, label in RANGES:
        if key == 'week':
            start = datetime.combine(end_day - timedelta(days=6), datetime.min.time(), IST)
        elif key == 'month':
            start = datetime.combine(end_day - timedelta(days=29), datetime.min.time(), IST)
        elif key == 'year':
            start = datetime(end_day.year, 1, 1, tzinfo=IST)
        else:
            start = listens[0]['_t']
        rows = [l for l in listens if l['_t'] >= start]
        ranges[key] = dict(label=label, start=start.strftime('%Y-%m-%d'), end=end_day.isoformat(), **rank_block(rows))

    recent = [dict(track=l['_tr'], artist=l['_a'], album=l['track_metadata'].get('release_name'),
                   at=l['listened_at'], cover=l['_c']) for l in listens[::-1][:60]]

    weeks = collections.OrderedDict()
    for l in listens:
        monday = (l['_t'].date() - timedelta(days=l['_t'].weekday())).isoformat()
        weeks[monday] = weeks.get(monday, 0) + 1
    week_list = [dict(week=k, count=v) for k, v in weeks.items()]

    # Artist origins from the credited artists' MusicBrainz area.
    origin_listens, origin_artists, origin_top = collections.Counter(), collections.defaultdict(set), collections.defaultdict(collections.Counter)
    placed = 0
    for l in listens:
        areas = {}
        for a in (l['_meta'] or {}).get('artists') or []:
            if a.get('area') and a.get('name'):
                areas[a['name']] = country(a['area'])
        if not areas:
            continue
        placed += 1
        for area in set(areas.values()):
            origin_listens[area] += 1
        for name, area in areas.items():
            origin_artists[area].add(name)
            origin_top[area][name] += 1
    mbid_of = {}
    for l in listens:
        for name, mbid in l['_credit']:
            if mbid:
                mbid_of.setdefault(name, mbid)
    origins = []
    for area, count in origin_listens.most_common(30):
        lead_name = origin_top[area].most_common(1)[0][0]
        origins.append(dict(area=area, listens=count, artists=len(origin_artists[area]),
                            top=[dict(name=n, count=c, artist_mbid=mbid_of.get(n), portrait=None,
                                      portrait_source=None, portrait_attribution=None)
                                 for n, c in origin_top[area].most_common(10)],
                            lead=dict(name=lead_name, count=origin_top[area][lead_name], artist_mbid=mbid_of.get(lead_name),
                                      portrait=None, portrait_source=None, portrait_attribution=None)))

    # Eras from the recording's first release year (release year as a fallback).
    years = collections.Counter()
    dated = []
    for l in listens:
        year = release_year(l['_meta'], last.year)
        if year:
            years[year] += 1
            dated.append((year, l))
    dated.sort(key=lambda x: x[0])
    def song(row):
        y, l = row
        return dict(year=y, track=l['_tr'], artist=l['_a'], cover=l['_c'])
    decade_albums = collections.defaultdict(collections.Counter)
    decade_cover = {}
    for y, l in dated:
        album = l['track_metadata'].get('release_name')
        if album:
            key = (album, l['_a'])
            decade_albums[y // 10 * 10][key] += 1
            if l['_c']:
                decade_cover.setdefault(key, l['_c'])
    decades = []
    for d in sorted({y // 10 * 10 for y, _ in dated}):
        count = sum(c for y, c in years.items() if y // 10 * 10 == d)
        best = decade_albums[d].most_common(1)
        top = dict(album=best[0][0][0], artist=best[0][0][1], count=best[0][1], cover=decade_cover.get(best[0][0])) if best else None
        decades.append(dict(decade=d, count=count, album=top))
    eras = dict(decades=decades, years=[dict(year=y, count=c) for y, c in sorted(years.items())],
                dated=len(dated),
                oldest=song(dated[0]) if dated else None,
                newest=song(dated[-1]) if dated else None,
                median=statistics.median([y for y, _ in dated]) if dated else None)

    # Genres: recording tags first, then release group, then artist tags.
    genre_listens, genre_hours = collections.Counter(), collections.defaultdict(lambda: [0] * 24)
    tagged = 0
    for l in listens:
        tags = genre_tags(l['_meta'])
        if not tags:
            continue
        tagged += 1
        for tag in tags:
            genre_listens[tag] += 1
            genre_hours[tag][l['_t'].hour] += 1
    genres = dict(tagged=tagged,
                  top=[dict(genre=g, count=c, hours=genre_hours[g]) for g, c in genre_listens.most_common(14)])

    # Career start years of the primary credited artist.
    starts = collections.Counter()
    earliest = None
    start_years = []
    for l in listens:
        arts = (l['_meta'] or {}).get('artists') or []
        if not arts or not arts[0].get('begin_year'):
            continue
        y = int(arts[0]['begin_year'])
        if not 1850 <= y <= last.year:
            continue
        starts[y // 10 * 10] += 1
        start_years.append(y)
        if earliest is None or y < earliest['year']:
            earliest = dict(year=y, name=arts[0].get('name'), artist_mbid=arts[0].get('mbid'),
                            portrait=None, portrait_source=None, portrait_attribution=None)
    careers = dict(decades=[dict(decade=d, count=c) for d, c in sorted(starts.items())],
                   median=int(statistics.median(start_years)) if start_years else None,
                   earliest=earliest, known=len(start_years))

    # Evolution: monthly ranks of the all-time top eight credited artists.
    by_month = collections.defaultdict(collections.Counter)
    for l in listens:
        for name, _ in l['_credit']:
            by_month[l['_t'].strftime('%Y-%m')][name] += 1
    leaders = [a['name'] for a in ranges['all']['top_artists'][:8]]
    months = sorted(by_month)
    evolution = dict(months=months, series=[])
    for name in leaders:
        points = []
        for m in months:
            ranking = [n for n, _ in by_month[m].most_common()]
            points.append(dict(rank=ranking.index(name) + 1 if name in ranking else None, count=by_month[m].get(name, 0)))
        evolution['series'].append(dict(name=name, points=points))

    # Track lengths and collaborations.
    lengths = [(l['_len'], l) for l in listens if isinstance(l['_len'], int) and l['_len'] > 30000]
    lengths.sort(key=lambda x: x[0])
    def track(row):
        ms, l = row
        return dict(track=l['_tr'], artist=l['_a'], ms=ms, cover=l['_c'])
    length_stats = dict(known=len(lengths),
                        average=round(statistics.mean(ms for ms, _ in lengths)) if lengths else None,
                        shortest=track(lengths[0]) if lengths else None,
                        longest=track(lengths[-1]) if lengths else None)
    pairs = collections.Counter()
    for l in listens:
        names = sorted({name for name, _ in l['_credit']})
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pairs[(names[i], names[j])] += 1
    collaborations = [dict(pair=list(k), count=v) for k, v in pairs.most_common(6)]

    hours = [0] * 24
    weekday_hours, weekend_hours = [0] * 24, [0] * 24
    for l in listens:
        hours[l['_t'].hour] += 1
        (weekend_hours if l['_t'].weekday() >= 5 else weekday_hours)[l['_t'].hour] += 1

    def window(days_back, length):
        end = datetime.combine(end_day - timedelta(days=days_back), datetime.min.time(), IST) + timedelta(days=1)
        start = end - timedelta(days=length)
        rows = [l for l in listens if start <= l['_t'] < end]
        before = {name for l in listens if l['_t'] < start for name, _ in l['_credit']}
        names = collections.Counter(name for l in rows for name, _ in l['_credit'])
        tracks = collections.Counter((l['_tr'], l['_a']) for l in rows)
        by_day = collections.Counter(l['_t'].strftime('%Y-%m-%d') for l in rows)
        top_track = tracks.most_common(1)
        lead = names.most_common(1)
        return dict(start=start.strftime('%Y-%m-%d'), end=(end - timedelta(days=1)).strftime('%Y-%m-%d'),
                    listens=len(rows), minutes=round(sum(l['_m'] for l in rows)), artists=len(names),
                    new_artists=sum(1 for name in names if name not in before), active_days=len(by_day),
                    biggest_day=max(by_day.values()) if by_day else 0,
                    top_artist=dict(name=lead[0][0], count=lead[0][1], artist_mbid=mbid_of.get(lead[0][0]),
                                    portrait=None, portrait_source=None, portrait_attribution=None) if lead else None,
                    top_track=dict(track=top_track[0][0][0], artist=top_track[0][0][1], count=top_track[0][1],
                                   cover=next((l['_c'] for l in rows if (l['_tr'], l['_a']) == top_track[0][0] and l['_c']), None)) if top_track else None)
    compare = dict(week=dict(now=window(0, 7), prev=window(7, 7)), month=dict(now=window(0, 30), prev=window(30, 30)))

    return dict(
        user=report.get('username', 'listener'), generated_at=report['generated_at'],
        total=len(listens), credited_artists=len({n for l in listens for n, _ in l['_credit']}),
        span_start=listens[0]['listened_at'], span_end=listens[-1]['listened_at'],
        metadata_coverage=sum(1 for l in listens if l['_meta']),
        ranges=ranges, recent=recent, weeks=week_list, hours=hours,
        weekday_hours=weekday_hours, weekend_hours=weekend_hours, compare=compare,
        origins=dict(placed=placed, areas=origins), eras=eras, genres=genres, careers=careers,
        evolution=evolution, lengths=length_stats, collaborations=collaborations,
        similar_users=similar or [],
    )


def main():
    audit_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '.state/latest-listens.json'
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'site'
    from lb_metadata import load_index
    report = json.loads(audit_path.read_text(encoding='utf-8'))
    metadata = {m: (e or {}).get('record') for m, e in load_index().items()}
    listens = prepare(report, metadata)
    profile = build_profile(report, metadata, listens=listens)
    serialized = json.dumps(profile, ensure_ascii=False)
    (output_dir / 'profile.json').write_text(serialized, encoding='utf-8')
    (output_dir / 'profile.js').write_text('window.PROFILE = ' + serialized + ';\n', encoding='utf-8')
    print('profile', profile['total'], 'listens; metadata for', profile['metadata_coverage'],
          '; origins', [(o['area'], o['listens']) for o in profile['origins']['areas'][:5]],
          '; eras', profile['eras']['oldest'], '; genres', [(g['genre'], g['count']) for g in profile['genres']['top'][:5]])


if __name__ == '__main__':
    main()
