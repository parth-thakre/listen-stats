#!/usr/bin/env python3
"""Build site/library.js from ListenBrainz (+ optional Last.fm & artwork) or synthetic demo data."""
import argparse
import collections
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
STATE = ROOT / ".state"


def _tz_offset_seconds(tz_name):
    now = datetime.now(ZoneInfo(tz_name))
    offset = now.utcoffset()
    return int(offset.total_seconds()) if offset else 0


def _top_artist_rows(library, limit=80):
    counts = collections.Counter()
    tracks = library["tracks"]
    for row in library["listens"]:
        for artist_index in tracks[row[1]][2]:
            counts[artist_index] += 1
    rows = []
    for artist_index, _ in counts.most_common(limit):
        name, mbid, _area = library["artists"][artist_index]
        if mbid:
            rows.append(dict(name=name, artist_mbid=mbid, portrait=None,
                             portrait_source=None, portrait_attribution=None))
    return rows


def _collect_portraits(rows, *, include_spotify=True):
    import re
    spotify_re = re.compile(r"^https://(?:i\.scdn\.co|image-cdn-(?:ak|fa)\.spotifycdn\.com)/image/[A-Za-z0-9]+$")
    portraits, credits = {}, []
    for row in rows or ():
        name = (row or {}).get("name")
        if not name or name in portraits:
            continue
        portrait = (row or {}).get("portrait")
        if portrait:
            portraits[name] = portrait
            attr = row.get("portrait_attribution")
            src = row.get("portrait_source")
            if isinstance(attr, dict):
                author = attr.get("artist") or attr.get("attribution") or attr.get("credit") or ""
                credits.append({
                    "name": name,
                    "attribution": author,
                    "source": attr.get("source_url") or src or "",
                    "license": attr.get("license") or "",
                    "license_url": attr.get("license_url") or "",
                })
            elif attr or src:
                credits.append({
                    "name": name,
                    "attribution": str(attr or ""),
                    "source": str(src or ""),
                    "license": "",
                    "license_url": "",
                })
            continue
        if include_spotify:
            sp = row.get("spotify_portrait")
            if (sp and row.get("artist_mbid") and sp.get("artist_mbid") == row["artist_mbid"]
                    and isinstance(sp.get("url"), str) and spotify_re.match(sp["url"])):
                portraits[name] = sp["url"]
    return portraits, credits


def build_from_api(args):
    from audit_lb_apis import fetch_raw_listens, make_session
    from cache_gallery_art import is_trusted_cover_url
    from gen_library import build_library
    from gen_profile import prepare
    from lb_metadata import fetch_metadata, fetch_similar_users

    STATE.mkdir(exist_ok=True, mode=0o700)
    SITE.mkdir(exist_ok=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    try:
        raw = fetch_raw_listens(session, args.user)
        generated_at = datetime.now(timezone.utc).isoformat()
        report = {"username": args.user, "generated_at": generated_at, "raw_listens": raw}

        extra = []
        if args.lastfm_user:
            try:
                from lastfm_listens import lastfm_extras
                extra, counts = lastfm_extras(
                    session, raw, STATE / "lastfm-scrobbles.json", user=args.lastfm_user
                )
                print("Last.fm:", json.dumps(counts), flush=True)
            except Exception as err:
                print(f"Last.fm unavailable ({type(err).__name__}); using ListenBrainz only", flush=True)

        if not args.public and os.environ.get("LB_LIBRARY_ROOTS"):
            from local_artwork import enrich_listens
            enrich_listens(raw, asset_dir=out_dir / "assets/library", index_path=STATE / "library-art-index.json")
            if extra:
                enrich_listens(extra, asset_dir=out_dir / "assets/library", index_path=STATE / "library-art-index.json")

        metadata, mstats = fetch_metadata(session, [*raw, *extra], index_path=STATE / "recording-metadata.json")
        print("Recording metadata:", json.dumps(mstats), flush=True)

        listens = prepare(report, metadata)
        merged = listens
        if extra:
            added = prepare({"raw_listens": list(extra)}, metadata)
            for item in added:
                item["_c"] = item["_c"] or item.get("_lastfm_cover")
            merged = sorted(listens + added, key=lambda l: l["listened_at"])

        library = build_library(merged, generated_at)
        library["user"] = args.user
        library["standalone"] = True
        library["tz_name"] = args.tz
        library["tz_offset"] = _tz_offset_seconds(args.tz)
        library["portraits"] = {}
        library["portrait_credits"] = []

        if args.public:
            library["similar_users"] = []
            if args.covers:
                # Keep only verified HTTPS cover URLs and fill missing top covers with remote iTunes URLs.
                for album in library["albums"]:
                    if album[2] and not is_trusted_cover_url(album[2]):
                        album[2] = None
                for track in library["tracks"]:
                    if track[3] and not is_trusted_cover_url(track[3]):
                        track[3] = None
                from itunes_artwork import enrich_library_itunes
                istats = enrich_library_itunes(
                    library, public=out_dir, index_path=STATE / "itunes-art.json",
                    max_lookups=args.itunes_lookups, remote_only=True,
                )
                print("iTunes artwork (remote):", json.dumps(istats), flush=True)
            else:
                for album in library["albums"]:
                    album[2] = None
                for track in library["tracks"]:
                    track[3] = None
        else:
            library["similar_users"] = fetch_similar_users(session, args.user) or []
            if args.cache_covers:
                from cache_gallery_art import cache_library_art
                cstats = cache_library_art(
                    library, public=out_dir, failure_path=STATE / "cover-failures.json", max_downloads=60
                )
                print("Cover cache:", json.dumps(cstats), flush=True)
            if args.covers:
                from itunes_artwork import enrich_library_itunes
                istats = enrich_library_itunes(
                    library, public=out_dir, index_path=STATE / "itunes-art.json",
                    max_lookups=args.itunes_lookups, remote_only=not args.cache_covers,
                )
                print("iTunes artwork:", json.dumps(istats), flush=True)

        if args.portraits:
            from artist_portraits import enrich_portraits
            from spotify_portraits import enrich_spotify_portraits
            rows = _top_artist_rows(library, limit=80)
            pstats = enrich_portraits(
                {}, {}, raw, public=out_dir, index_path=STATE / "artist-portraits.json",
                seconds=60, max_artists=args.portrait_lookups, extra=rows,
            )
            sstats = enrich_spotify_portraits(
                rows, index_path=STATE / "spotify-portraits.json",
                max_artists=args.portrait_lookups, seconds=60,
            )
            portraits, credits = _collect_portraits(rows, include_spotify=True)
            library["portraits"] = portraits
            library["portrait_credits"] = credits
            print("Artist portraits (Commons):", json.dumps(pstats), flush=True)
            print("Artist portraits (Spotify):", json.dumps(sstats), f"total_published={len(portraits)}", flush=True)

        out = out_dir / "library.js"
        out.write_text(
            "window.LIBRARY = " + json.dumps(library, ensure_ascii=False, separators=(",", ":")) + ";\n",
            encoding="utf-8",
        )
        al_cov = sum(1 for a in library["albums"] if a[2])
        tr_cov = sum(1 for t in library["tracks"] if t[3])
        print(
            f"Published {len(library['listens']):,} listens -> {out} "
            f"(covers: {tr_cov}/{len(library['tracks'])} tracks, {al_cov}/{len(library['albums'])} albums; "
            f"portraits: {len(library['portraits'])})",
            flush=True,
        )
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Build static listening-stats snapshot.")
    parser.add_argument("--demo", action="store_true", help="Build synthetic initials-only demo data.")
    parser.add_argument("--user", default=os.environ.get("LB_USER", ""), help="ListenBrainz username.")
    parser.add_argument("--lastfm-user", default=os.environ.get("LASTFM_USER", ""), help="Optional Last.fm username.")
    parser.add_argument("--tz", default=os.environ.get("LB_TZ", "UTC"), help="IANA timezone name (default: UTC).")
    parser.add_argument("--public", action="store_true", help="Public-safe build: no similar users or local paths.")
    parser.add_argument("--covers", action="store_true", help="Include remote album/track covers (CAA, Last.fm, iTunes).")
    parser.add_argument("--portraits", action="store_true", help="Fetch CC-licensed Wikimedia Commons artist portraits.")
    parser.add_argument("--itunes-lookups", type=int, default=60, help="Max new iTunes lookups per run (default: 60).")
    parser.add_argument("--portrait-lookups", type=int, default=40, help="Max new portrait lookups per run (default: 40).")
    parser.add_argument("--cache-covers", action="store_true", help="Self-hosted mode: cache covers locally as WebP.")
    parser.add_argument("--out", default=str(SITE), help="Output directory (default: site/).")
    args = parser.parse_args()

    if args.demo:
        from demo_data import build_demo_library
        out = Path(args.out) / "library.js"
        out.parent.mkdir(parents=True, exist_ok=True)
        lib = build_demo_library()
        out.write_text(
            "window.LIBRARY = " + json.dumps(lib, ensure_ascii=False, separators=(",", ":")) + ";\n",
            encoding="utf-8",
        )
        print(f"Wrote synthetic demo ({len(lib['listens'])} listens) -> {out}")
        return
    if not args.user:
        parser.error("Provide --user <listenbrainz_username> (or pass --demo for synthetic data).")
    build_from_api(args)


if __name__ == "__main__":
    main()
