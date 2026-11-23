# Listening Stats

A fast, zero-framework, fully clickable listening-history web app powered by [ListenBrainz](https://listenbrainz.org/), [MusicBrainz](https://musicbrainz.org/), and optionally [Last.fm](https://www.last.fm/).

Every artist, song, album, day, week, month, calendar tile, genre, decade, and country is a clickable route with instant `/` search, global `7d / 30d / This year / All time` filtering, and Plays / Time / Recent sorting—compiled into a single static `site/library.js` bundle with no backend database.

## Quickstart (synthetic demo)

No API keys or personal data required:

```sh
python3 build.py --demo
python3 serve.py
```

Open **http://127.0.0.1:8877/**.

## Build with your own ListenBrainz data

```sh
pip install -r requirements.txt

# Public-safe build (initials-only artwork, no similar-user list)
python3 build.py --user YOUR_LB_USERNAME --tz UTC --public

# Optional: merge missed Last.fm scrobbles (set LASTFM_API_KEY in env)
LASTFM_API_KEY=... python3 build.py --user YOUR_LB_USERNAME --lastfm-user YOUR_LFM_USERNAME --tz America/New_York --public
```

### Deploy on GitHub Pages (auto-refreshed)

Fork the repo, enable **Settings → Pages → Source: GitHub Actions**, and configure repository Variables/Secrets (nothing personal needs to be committed to Git):

- **Variables** (*Settings → Secrets and variables → Actions → Variables*):
  - `LB_USER` — your ListenBrainz username (if unset, GitHub Pages builds the synthetic demo)
  - `LB_TZ` — optional IANA timezone name (e.g. `America/New_York`, default `UTC`)
  - `LASTFM_USER` — optional Last.fm username to merge missed scrobbles
- **Secrets** (*Settings → Secrets and variables → Actions → Secrets*):
  - `LASTFM_API_KEY` — optional Last.fm API key (required only when `LASTFM_USER` is set)

The included workflow rebuilds in `--public` mode on every push to `main`, manual dispatch, and every 6 hours, caching `.state/` between runs.

### Self-hosted private mode (with artwork)

For a private server (not redistributed publicly on GitHub Pages):

```sh
# Pull embedded/folder art from your own music library + cache covers locally
LB_LIBRARY_ROOTS=/path/to/music python3 build.py --user YOUR_LB_USERNAME --cache-covers
```

## Public hosting & copyright notes

- **Public builds (`--public`)** use deterministic gradient monogram tiles so you never redistribute copyrighted album covers or violate third-party API terms.
- **Last.fm data** is optional and subject to the [Last.fm API Terms of Service](https://www.last.fm/api/tos) (non-commercial use with attribution).
- **Artist & recording metadata** comes from [ListenBrainz](https://listenbrainz.org/) and [MusicBrainz](https://musicbrainz.org/).

## Tests

```sh
python3 -m unittest discover -s tests -v
```
