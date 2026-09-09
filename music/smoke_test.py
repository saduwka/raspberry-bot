#!/usr/bin/env python3
"""Live smoke test for Yandex music player on DietPi.

Skips when token/mpv prerequisites are missing.
Usage: python3 /root/bot/music/smoke_test.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BOT_ROOT = Path("/root/bot")
sys.path.insert(0, str(BOT_ROOT))


def main() -> int:
    try:
        from config import YANDEX_MUSIC_TOKEN
    except Exception as exc:
        print(f"SKIP: cannot import config ({exc})")
        return 0

    if not YANDEX_MUSIC_TOKEN:
        print("SKIP: YANDEX_MUSIC_TOKEN not set")
        return 0

    if not os.path.exists("/usr/bin/mpv") and not os.path.exists("/usr/local/bin/mpv"):
        print("SKIP: mpv not installed")
        return 0

    from music.player import MusicPlayerService

    player = MusicPlayerService()
    print("== start_my_wave ==")
    t0 = time.time()
    msg = player.start_my_wave()
    print(f"  {msg} ({time.time() - t0:.2f}s)")
    time.sleep(1.0)
    np = player.get_now_playing()
    print("  now:", np.get("track_id"), np.get("title"), "playing=", np.get("playing"))
    if not np.get("playing") or not np.get("track_id"):
        print("FAIL: wave did not start")
        try:
            player.stop()
        except Exception:
            pass
        return 1
    first_id = np["track_id"]

    print("== pause ==")
    print(" ", player.toggle_pause())
    time.sleep(0.3)
    np = player.get_now_playing()
    if not np.get("paused"):
        print("FAIL: expected paused")
        player.stop()
        return 1
    print(" ", player.toggle_pause())

    print("== next ==")
    t0 = time.time()
    print(" ", player.next_track(), f"({time.time() - t0:.2f}s)")
    elapsed = time.time() - t0
    if elapsed > 2.5:
        print(f"WARN: next slower than expected ({elapsed:.2f}s)")
    # allow mpv settle
    changed = False
    for _ in range(10):
        time.sleep(0.2)
        np = player.get_now_playing()
        if np.get("track_id") and np["track_id"] != first_id:
            changed = True
            break
    print("  now:", np.get("track_id"), np.get("title"))
    if not changed:
        print("FAIL: track_id did not change after next")
        player.stop()
        return 1

    print("== stop ==")
    print(" ", player.stop())
    time.sleep(0.3)
    np = player.get_now_playing()
    if np.get("playing"):
        print("FAIL: still playing after stop")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
