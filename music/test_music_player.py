#!/usr/bin/env python3
"""Unit tests for Yandex music player core (no network / no mpv required)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BOT_ROOT = Path("/root/bot")
OLED_ROOT = Path("/root/oled")
sys.path.insert(0, str(BOT_ROOT))
sys.path.insert(0, str(OLED_ROOT))


class TestAtomicState(unittest.TestCase):
    def test_atomic_save_roundtrip(self):
        import music.player as player_mod

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "music_state.json"
            lock_path = Path(tmp) / "yamusic.lock"
            with patch.object(player_mod, "STATE_PATH", state_path), patch.object(
                player_mod, "LOCK_PATH", lock_path
            ):
                svc = player_mod.MusicPlayerService()
                payload = {
                    "batch_id": "b1",
                    "playlist": [{"track_id": "1", "title": "T", "artists": "A", "duration_sec": 10}],
                    "current_index": 0,
                    "started_at": 1,
                    "station": "user:onyourwave",
                }
                loaded = svc._test_atomic_save_roundtrip(payload)
                self.assertEqual(loaded["batch_id"], "b1")
                self.assertEqual(loaded["playlist"][0]["title"], "T")
                self.assertTrue(state_path.exists())
                # no leftover tmp
                self.assertFalse(state_path.with_suffix(".tmp").exists())


class TestNowPlayingClamp(unittest.TestCase):
    def test_index_clamped(self):
        import music.player as player_mod

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "music_state.json"
            lock_path = Path(tmp) / "yamusic.lock"
            state = {
                "batch_id": None,
                "playlist": [
                    {"track_id": "a", "title": "A", "artists": "X", "duration_sec": 100, "cover_uri": ""},
                    {"track_id": "b", "title": "B", "artists": "Y", "duration_sec": 120, "cover_uri": "c%%"},
                ],
                "current_index": 99,
                "started_at": 0,
                "station": "user:onyourwave",
            }
            state_path.write_text(json.dumps(state))
            with patch.object(player_mod, "STATE_PATH", state_path), patch.object(
                player_mod, "LOCK_PATH", lock_path
            ), patch.object(player_mod.MusicPlayerService, "_ipc_alive", return_value=False):
                svc = player_mod.MusicPlayerService()
                np = svc.get_now_playing()
                self.assertEqual(np["track_id"], "b")
                self.assertEqual(np["playlist_pos"], 1)
                self.assertEqual(np["title"], "B")


class TestLockSerialize(unittest.TestCase):
    def test_lock_serializes_two_threads(self):
        import music.player as player_mod

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "yamusic.lock"
            with patch.object(player_mod, "LOCK_PATH", lock_path):
                svc = player_mod.MusicPlayerService()
                order: list[str] = []
                barrier = threading.Barrier(2)

                def worker(name: str):
                    barrier.wait()
                    with svc._music_lock():
                        order.append(f"{name}-in")
                        time.sleep(0.15)
                        order.append(f"{name}-out")

                t1 = threading.Thread(target=worker, args=("a",))
                t2 = threading.Thread(target=worker, args=("b",))
                t1.start()
                t2.start()
                t1.join()
                t2.join()
                # Fully nested critical sections — no interleaving of in/out across threads
                self.assertEqual(len(order), 4)
                self.assertTrue(
                    order == ["a-in", "a-out", "b-in", "b-out"]
                    or order == ["b-in", "b-out", "a-in", "a-out"]
                )


class TestNextDoesNotBlockOnPrefetch(unittest.TestCase):
    def test_next_returns_before_prefetch_finishes(self):
        import music.player as player_mod

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "music_state.json"
            lock_path = Path(tmp) / "yamusic.lock"
            state = {
                "batch_id": "b",
                "playlist": [
                    {"track_id": "1", "title": "One", "artists": "A", "duration_sec": 10, "cover_uri": ""},
                    {"track_id": "2", "title": "Two", "artists": "B", "duration_sec": 10, "cover_uri": ""},
                ],
                "current_index": 0,
                "started_at": 1,
                "station": "user:onyourwave",
            }
            state_path.write_text(json.dumps(state))

            slow_started = threading.Event()
            slow_finished = threading.Event()

            def slow_run(self):
                slow_started.set()
                time.sleep(1.0)
                slow_finished.set()
                with self._prefetch_lock:
                    self._prefetch_running = False

            calls = {"n": 0}

            def fake_status(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"playlist_pos": 0, "pause": False, "time_pos": 3}
                return {"playlist_pos": 1, "pause": False, "time_pos": 0}

            with patch.object(player_mod, "STATE_PATH", state_path), patch.object(
                player_mod, "LOCK_PATH", lock_path
            ), patch.object(
                player_mod.MusicPlayerService, "get_status", fake_status
            ), patch.object(
                player_mod.MusicPlayerService,
                "_socket_request",
                return_value={"error": "success"},
            ), patch.object(
                player_mod.MusicPlayerService, "_run_prefetch_job", slow_run
            ), patch.object(
                player_mod.MusicPlayerService, "_ensure_watchdog", lambda self: None
            ):
                svc = player_mod.MusicPlayerService()
                svc.yandex = MagicMock()
                t0 = time.time()
                msg = svc.next_track()
                elapsed = time.time() - t0
                self.assertIn("Two", msg)
                self.assertLess(elapsed, 0.8, f"next blocked too long: {elapsed:.2f}s")
                self.assertTrue(slow_started.wait(0.5))
                self.assertFalse(slow_finished.is_set())


class TestBridgeStaleSnapshot(unittest.TestCase):
    def test_stale_nonforce_overwrite_rejected(self):
        import music_bridge as bridge_mod

        with patch.object(bridge_mod, "METADATA_TTL", 0.0), patch.object(
            bridge_mod, "PROGRESS_TTL", 0.0
        ):
            br = bridge_mod.MusicBridge()
            br._get_player = MagicMock()
            player = br._get_player.return_value
            player._ipc_alive.return_value = True
            player._ensure_watchdog = MagicMock()
            player.request_prefetch = MagicMock()

            player.get_now_playing.return_value = {
                "ok": True,
                "playing": True,
                "paused": False,
                "track_id": "new",
                "title": "New",
                "artists": "N",
                "time_pos": 1,
                "duration": 100,
                "cover_uri": "https://example/%%",
                "station": "user:onyourwave",
                "playlist_pos": 1,
                "playlist_len": 3,
            }
            fresh = br.snapshot(force=True)
            self.assertEqual(fresh["track_id"], "new")

            # Older in-flight non-force refresh must not clobber newer cache
            player.get_now_playing.return_value = {
                "ok": True,
                "playing": True,
                "paused": False,
                "track_id": "old",
                "title": "Old",
                "artists": "O",
                "time_pos": 50,
                "duration": 100,
                "cover_uri": "",
                "station": "user:onyourwave",
                "playlist_pos": 0,
                "playlist_len": 3,
            }
            with br._lock:
                br._last_meta_refresh = time.time() + 5
                br._last_progress_refresh = time.time() + 5
            out = br.snapshot(force=False)
            self.assertEqual(out["track_id"], "new")

class TestVolumeClamp(unittest.TestCase):
    def test_clamp(self):
        from music.player import clamp_volume
        self.assertEqual(clamp_volume(-5), 0)
        self.assertEqual(clamp_volume(150), 100)
        self.assertEqual(clamp_volume("42"), 42)
        self.assertEqual(clamp_volume(None), 70)

    def test_set_volume_persists_without_mpv(self):
        import music.player as player_mod

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "music_state.json"
            lock_path = Path(tmp) / "yamusic.lock"
            state_path.write_text(json.dumps(player_mod._empty_state()))
            with patch.object(player_mod, "STATE_PATH", state_path), patch.object(
                player_mod, "LOCK_PATH", lock_path
            ), patch.object(player_mod.MusicPlayerService, "_ipc_alive", return_value=False):
                svc = player_mod.MusicPlayerService()
                self.assertEqual(svc.set_volume(15), 15)
                loaded = json.loads(state_path.read_text())
                self.assertEqual(loaded["volume"], 15)
                np = svc.get_now_playing()
                self.assertEqual(np["volume"], 15)

if __name__ == "__main__":
    unittest.main(verbosity=2)
