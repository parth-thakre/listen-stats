#!/usr/bin/env python3
"""Build site/library.js from ListenBrainz (+ optional Last.fm & local art) or synthetic demo data."""
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


def build_from_api(args):
    from audit_lb_apis import fetch_raw_listens, make_session
    from gen_library import build_library
    from gen_profile import prepare
    from lb_metadata import fetch_metadata, fetch_similar_users

    STATE.mkdir(exist_ok=True, mode=0o700)
    SITE.mkdir(exist_ok=True)
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
            enrich_listens(raw, asset_dir=SITE / "assets/library", index_path=STATE / "library-art-index.json")
            if extra:
                enrich_listens(extra, asset_dir=SITE / "assets/library", index_path=STATE / "library-art-index.json")

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
            # Public/Pages builds ship zero redistributed copyrighted covers and zero other users' names.
            for album in library["albums"]:
                album[2] = None
            for track in library["tracks"]:
                track[3] = None
            library["similar_users"] = []
        else:
            library["similar_users"] = fetch_similar_users(session, args.user) or []
            if args.cache_covers:
                from cache_gallery_art import cache_library_art
                cstats = cache_library_art(
                    library, public=SITE, failure_path=STATE / "cover-failures.json", max_downloads=60
                )
                print("Cover cache:", json.dumps(cstats), flush=True)

        out = Path(args.out) / "library.js"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "window.LIBRARY = " + json.dumps(library, ensure_ascii=False, separators=(",", ":")) + ";\n",
            encoding="utf-8",
        )
        print(f"Published {len(library['listens']):,} listens -> {out}", flush=True)
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Build static listening-stats snapshot.")
    parser.add_argument("--demo", action="store_true", help="Build synthetic initials-only demo data.")
    parser.add_argument("--user", default=os.environ.get("LB_USER", ""), help="ListenBrainz username.")
    parser.add_argument("--lastfm-user", default=os.environ.get("LASTFM_USER", ""), help="Optional Last.fm username.")
    parser.add_argument("--tz", default=os.environ.get("LB_TZ", "UTC"), help="IANA timezone name (default: UTC).")
    parser.add_argument("--public", action="store_true", help="Public-safe build: initials only, no similar users.")
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
