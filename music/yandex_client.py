from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from config import YANDEX_MUSIC_TOKEN, YANDEX_MUSIC_WAVE_STATION

logger = logging.getLogger(__name__)


@dataclass
class WaveTrack:
    track_id: str
    title: str
    artists: str
    duration_sec: int
    url: str = ""
    cover_uri: str = ""


class YandexMusicController:
    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if not YANDEX_MUSIC_TOKEN:
            raise RuntimeError("Не задан YANDEX_MUSIC_TOKEN в .env")
        if self._client is None:
            from yandex_music import Client

            self._client = Client(YANDEX_MUSIC_TOKEN).init()
        return self._client

    def _pick_download_url(self, track) -> str:
        infos = []
        if hasattr(track, "get_download_info"):
            infos = track.get_download_info()
        elif hasattr(track, "download_info"):
            infos = track.download_info

        scored = []
        for info in infos or []:
            codec = getattr(info, "codec", "") or ""
            bitrate = int(getattr(info, "bitrate_in_kbps", 0) or 0)
            if codec == "mp3" and 128 <= bitrate <= 192:
                score = 20000 + bitrate
            elif codec == "mp3":
                score = 10000 + max(0, 192 - abs(bitrate - 192))
            else:
                score = bitrate
            scored.append((score, info))
        scored.sort(key=lambda item: item[0], reverse=True)

        for _, info in scored:
            direct_link = getattr(info, "direct_link", None)
            if direct_link:
                return direct_link
            if hasattr(info, "get_direct_link"):
                direct_link = info.get_direct_link()
                if direct_link:
                    return direct_link

        raise RuntimeError("Не удалось получить прямую ссылку на поток трека.")

    def _resolve_track(self, item):
        track = getattr(item, "track", None) or item
        if hasattr(track, "fetch_track"):
            track = track.fetch_track()
        return track

    @staticmethod
    def cover_uri_to_url(cover_uri: str, size: str = "200x200") -> str:
        uri = (cover_uri or "").strip()
        if not uri:
            return ""
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri.replace("%%", size)
        return f"https://{uri.replace('%%', size)}"

    def _as_wave_track(self, track, *, with_url: bool = False) -> WaveTrack:
        artists = ", ".join(artist.name for artist in getattr(track, "artists", []) if getattr(artist, "name", None))
        duration_ms = int(getattr(track, "duration_ms", 0) or 0)
        cover_uri = getattr(track, "cover_uri", None) or ""
        return WaveTrack(
            track_id=str(track.id),
            title=getattr(track, "title", "Unknown track"),
            artists=artists or "Unknown artist",
            duration_sec=duration_ms // 1000,
            url=self._pick_download_url(track) if with_url else "",
            cover_uri=str(cover_uri),
        )

    def fetch_cover_uri(self, track_id: str) -> str:
        client = self._get_client()
        tracks = client.tracks([track_id])
        if not tracks:
            return ""
        return str(getattr(tracks[0], "cover_uri", None) or "")

    def resolve_stream_url(self, track_id: str) -> str:
        client = self._get_client()
        tracks = client.tracks([track_id])
        if not tracks:
            raise RuntimeError(f"Трек {track_id} не найден.")
        time.sleep(0.4)
        return self._pick_download_url(tracks[0])

    def _extract_sequence(self, result) -> list:
        sequence = getattr(result, "sequence", None) or []
        if sequence:
            return sequence
        tracks = getattr(result, "tracks", None) or []
        if tracks:
            return tracks
        return []

    def fetch_wave_batch(self, queue_track_id: str | None = None) -> tuple[list[WaveTrack], str | None]:
        client = self._get_client()
        result = client.rotor_station_tracks(YANDEX_MUSIC_WAVE_STATION, queue=queue_track_id)
        if result is None:
            raise RuntimeError("Яндекс не вернул треки для Моей волны.")

        batch_id = getattr(result, "batch_id", None)
        items = self._extract_sequence(result)
        tracks: list[WaveTrack] = []
        for index, item in enumerate(items):
            track = self._resolve_track(item)
            tracks.append(self._as_wave_track(track, with_url=index == 0))

        if not tracks:
            raise RuntimeError("В Моей волне не удалось получить ни одного трека.")
        if not tracks[0].url:
            raise RuntimeError("Не удалось получить ссылку на первый трек.")
        return tracks, batch_id

    def _safe_feedback(self, action, *args, **kwargs) -> None:
        try:
            action(*args, **kwargs)
        except Exception as exc:
            logger.warning("Yandex feedback skipped: %s", exc)

    def _safe_feedback_async(self, action, *args, **kwargs) -> None:
        threading.Thread(
            target=self._safe_feedback,
            args=(action, *args),
            kwargs=kwargs,
            name="yandex-feedback",
            daemon=True,
        ).start()

    def mark_radio_started(self, batch_id: str | None) -> None:
        client = self._get_client()
        self._safe_feedback_async(
            client.rotor_station_feedback_radio_started,
            YANDEX_MUSIC_WAVE_STATION,
            "telegram-bot",
            batch_id=batch_id,
        )

    def mark_track_started(self, track_id: str, batch_id: str | None) -> None:
        client = self._get_client()
        self._safe_feedback_async(
            client.rotor_station_feedback_track_started,
            YANDEX_MUSIC_WAVE_STATION,
            track_id,
            batch_id=batch_id,
        )

    def mark_track_skipped(self, track_id: str, played_seconds: int, batch_id: str | None) -> None:
        client = self._get_client()
        self._safe_feedback_async(
            client.rotor_station_feedback_skip,
            YANDEX_MUSIC_WAVE_STATION,
            track_id,
            max(played_seconds, 0),
            batch_id=batch_id,
        )
