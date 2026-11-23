#!/usr/bin/env python3
"""Generate a deterministic, synthetic, initials-only window.LIBRARY fixture for demos and CI."""
import json
from pathlib import Path
import random


CATALOG = [
    ("Neon Harbor", "United Kingdom", "indie pop", 2023, "Glass Tides", [
        ("Harbor Lights", 214000), ("Paper Boats", 198000), ("Salt Air", 232000), ("Midnight Ferry", 245000),
    ]),
    ("Kaveri Echo", "India", "indie folk", 2024, "Monsoon Postcards", [
        ("Rain on Tin", 256000), ("Banyan Shade", 241000), ("Old Tram", 229000), ("Evening Chai", 215000),
    ]),
    ("Sora & The Pines", "Japan", "city pop", 2022, "Shibuya Loop", [
        ("Velvet Escalator", 205000), ("Last Express", 221000), ("Vending Glow", 194000),
    ]),
    ("Mira Solis", "Colombia", "alt-pop", 2025, "Solaris", [
        ("Cinnamon Sky", 188000), ("Blue Balcony", 212000), ("Heatwave", 203000),
    ]),
    ("Dusk Telemetry", "Germany", "electronic", 2021, "Signal Path", [
        ("Subway Hum", 268000), ("Warm Static", 290000), ("Night Bus", 244000),
    ]),
    ("Aarav Sen", "India", "filmi", 2018, "Lantern Street", [
        ("Courtyard Song", 274000), ("First Train", 261000), ("Apricot Dawn", 238000),
    ]),
    ("Hana Kim", "South Korea", "k-pop", 2024, "Prism", [
        ("Starlight Sprint", 184000), ("Polaroid", 192000), ("Zero Gravity", 199000),
    ]),
    ("The Cedar Room", "Canada", "indie rock", 2016, "Northbound", [
        ("Gravel Road", 235000), ("Pine Smoke", 252000), ("October Porch", 219000),
    ]),
    ("Nadia Noor", "Pakistan", "qawwali fusion", 2020, "Courtyard Sessions", [
        ("Lahore Mist", 312000), ("Amber Strings", 287000),
    ]),
    ("Clara Lind", "Sweden", "synthpop", 2019, "Northern Neon", [
        ("Silver Frost", 208000), ("Icebreaker", 197000),
    ]),
]


def build_demo_library(seed=42):
    rng = random.Random(seed)
    artists = []
    albums = []
    tracks = []
    genres = []
    genre_ix = {}

    for idx, (artist, country, genre, year, album, song_list) in enumerate(CATALOG):
        artists.append([artist, None, country])
        al_idx = len(albums)
        albums.append([album, [idx], None])
        g = genre_ix.setdefault(genre, len(genres))
        if g == len(genres):
            genres.append(genre)
        for s_idx, (title, ms) in enumerate(song_list):
            credit = [idx]
            # Add a few realistic duets/collaborations.
            if s_idx == 0 and idx in (1, 5, 6):
                partner = (idx + 1) % len(CATALOG)
                credit.append(partner)
            line = ", ".join(CATALOG[c][0] for c in credit)
            tracks.append([title, line, credit, None, ms, year, [g]])

    # Weight earlier artists/tracks higher for a realistic Zipf-like listening curve.
    weights = [max(1, len(tracks) - i) ** 1.35 for i in range(len(tracks))]
    base_ts = 1767225600  # 2026-01-01T00:00:00Z
    listens = []
    for day_offset in range(180):
        # Skip occasional quiet days.
        if rng.random() < 0.14:
            continue
        day_start = base_ts + day_offset * 86400
        sessions = rng.randint(1, 3)
        for _ in range(sessions):
            hour = rng.choice([9, 10, 11, 13, 14, 15, 16, 17, 18, 20, 21, 22])
            ts = day_start + hour * 3600 + rng.randint(0, 1800)
            for _ in range(rng.randint(2, 6)):
                t_idx = rng.choices(range(len(tracks)), weights=weights, k=1)[0]
                sec = round(tracks[t_idx][4] / 1000)
                al_idx = next(i for i, a in enumerate(albums) if tracks[t_idx][2][0] in a[1])
                listens.append([ts, t_idx, al_idx, sec])
                ts += sec + rng.randint(2, 20)
    listens.sort(key=lambda r: r[0])

    return {
        "generated_at": "2026-06-30T12:00:00+00:00",
        "user": "demo-listener",
        "standalone": True,
        "tz_offset": 0,
        "tz_name": "UTC",
        "artists": artists,
        "albums": albums,
        "tracks": tracks,
        "genres": genres,
        "listens": listens,
        "portraits": {},
        "portrait_credits": [],
        "similar_users": [],
        "sources": {"listenbrainz": len(listens), "lastfm": 0, "double_submissions_dropped": 0},
    }


def main():
    out = Path(__file__).resolve().parent / "site" / "library.js"
    out.parent.mkdir(parents=True, exist_ok=True)
    lib = build_demo_library()
    out.write_text(
        "window.LIBRARY = " + json.dumps(lib, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    print(f"Wrote synthetic demo library: {len(lib['listens'])} listens -> {out}")


if __name__ == "__main__":
    main()
