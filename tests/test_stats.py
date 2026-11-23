from pathlib import Path
import re
import tempfile
from unittest.mock import MagicMock
import unittest

import cache_gallery_art as cga
import demo_data
import gen_library as gl
import gen_profile as gp
import lastfm_listens as lf

REPO_ROOT = Path(__file__).resolve().parent.parent


def lb_listen(ts, track, artist, album=None, *, map_rel=None, ai_rel=None):
    tm = {"track_name": track, "artist_name": artist, "additional_info": {}, "mbid_mapping": {}}
    if album is not None:
        tm["release_name"] = album
    if map_rel:
        tm["mbid_mapping"]["release_mbid"] = map_rel
    if ai_rel:
        tm["additional_info"]["release_mbid"] = ai_rel
    return {"listened_at": ts, "track_metadata": tm}


class CoreLogicTests(unittest.TestCase):
    def test_different_song_after_lb_end_is_not_dropped(self):
        lb = [lb_listen(1189, "Debora", "T. Rex")]
        sc = [[1000, "Debora", "T. Rex", None, None, None], [1198, "Smooth Operator", "Sade", None, None, None]]
        extra, counts = lf.missing(lb, sc)
        self.assertEqual(counts["matched"], 1)
        self.assertEqual([e["track_metadata"]["track_name"] for e in extra], ["Smooth Operator"])

    def test_lastfm_error_never_leaks_api_key(self):
        secret = "SUPER_SECRET_KEY_999"
        resp = MagicMock(status_code=503)
        err = Exception(f"503 for https://ws.audioscrobbler.com/2.0/?api_key={secret}")
        err.response = resp
        resp.raise_for_status.side_effect = err
        sess = MagicMock()
        sess.get.return_value = resp
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError) as ctx:
                lf.fetch_scrobbles(sess, Path(td) / "cache.json", user="demo", key=secret)
        self.assertNotIn(secret, str(ctx.exception))

    def test_compilation_mapping_does_not_merge_distinct_albums(self):
        comp = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        rows = [
            lb_listen(1700000000, "Song A", "Artist 1", "Album One", map_rel=comp, ai_rel="11111111-1111-1111-1111-111111111111"),
            lb_listen(1700000300, "Song B", "Artist 2", "Album Two", map_rel=comp, ai_rel="22222222-2222-2222-2222-222222222222"),
            lb_listen(1700000600, "Song B", "Artist 2", "Hits Compilation", map_rel=comp),
        ]
        lib = gl.build_library(gp.prepare({"raw_listens": rows, "generated_at": "2026-01-01T00:00:00Z"}, {}), "2026-01-01T00:00:00Z")
        self.assertEqual(sorted(a[0] for a in lib["albums"]), ["Album One", "Album Two", "Hits Compilation"])

    def test_trusted_cover_hosts(self):
        self.assertTrue(cga.is_trusted_cover_url("https://archive.org/download/mbid-1/a.jpg"))
        self.assertFalse(cga.is_trusted_cover_url("https://untrusted.example/x.jpg"))
        self.assertFalse(cga.is_trusted_cover_url("http://archive.org/download/mbid-1/a.jpg"))

    def test_demo_build_is_initials_only_and_standalone(self):
        lib = demo_data.build_demo_library()
        self.assertTrue(lib["standalone"])
        self.assertGreater(len(lib["listens"]), 300)
        self.assertTrue(all(a[2] is None for a in lib["albums"]))
        self.assertTrue(all(t[3] is None for t in lib["tracks"]))
        self.assertEqual(lib["similar_users"], [])

    def test_repo_contains_no_binary_artwork_or_private_host_paths(self):
        private_patterns = (
            re.compile(r"\." + "ts" + r"\." + "net\b", re.I),
            re.compile(r"/(?:" + "mnt" + "|" + "home" + r")/[A-Za-z0-9._-]+"),
        )
        for p in REPO_ROOT.rglob("*"):
            if ".git" in p.parts or "__pycache__" in p.parts or p.name == "library.js":
                continue
            if p.is_file():
                self.assertNotIn(p.suffix.lower(), {".jpg", ".jpeg", ".png", ".webp"}, f"Binary artwork in repo: {p}")
                if p.resolve() == Path(__file__).resolve():
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
                for rx in private_patterns:
                    self.assertIsNone(rx.search(text), f"Private path/host pattern matched in {p}")


if __name__ == "__main__":
    unittest.main()
