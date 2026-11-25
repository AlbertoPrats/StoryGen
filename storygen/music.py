"""Stage 4b - an eerie music bed under the story.

Royalty-free (CC0) tracks live in assets/music/ (listed in tracks.tsv, downloaded by install.sh; any other
audio file dropped there is used too). Per clip: one track at random, a random start point (looped when the
track is shorter than the clip), loudness-normalised so every track sits at the same level, short fade in
and fade out. The render mixes it under the voice and ducks it gently while the narrator speaks.
"""
from __future__ import annotations

import random
from pathlib import Path

from .config import Config
from .utils import ffmpeg, log, media_info

AUDIO = {".ogg", ".mp3", ".wav", ".flac", ".m4a", ".opus"}
LUFS = -20.0         # every track is normalised to this before [music].volume is applied


def list_tracks(cfg: Config) -> list[Path]:
    d = cfg.path(cfg.music.dir)
    return sorted(p for p in d.iterdir() if p.suffix.lower() in AUDIO) if d.is_dir() else []


def prepare(cfg: Config, duration: float, out_wav: Path, rng: random.Random) -> dict | None:
    """Writes a stereo 48 kHz music bed of `duration` seconds. Returns {track, offset} or None (no music)."""
    if not cfg.music.enabled:
        return None
    tracks = list_tracks(cfg)
    if not tracks:
        log.warning("No music in %s (run install.sh); using the drone instead", cfg.music.dir)
        return None
    track = rng.choice(tracks)
    try:
        length = float(media_info(track)["duration"])
        loop = length < duration + 1.0
        offset = 0.0 if loop else round(rng.uniform(0.0, max(0.0, min(length - duration - 1.0, length * 0.5))), 2)
        fade_out = max(0.0, duration - 2.0)
        ffmpeg([*(["-stream_loop", "-1"] if loop else []), "-ss", f"{offset:.2f}", "-i", str(track),
                "-t", f"{duration:.3f}", "-vn",
                "-af", f"aresample=48000,aformat=channel_layouts=stereo,loudnorm=I={LUFS}:TP=-2:LRA=11,"
                       f"aresample=48000,afade=t=in:d={cfg.music.fade_in}:curve=qsin,"
                       f"afade=t=out:st={fade_out:.3f}:d=2",
                "-ar", "48000", "-ac", "2", str(out_wav)])
    except Exception as e:  # noqa: BLE001 - a broken track must not stop the clip
        log.warning("Music track %s failed (%s); no music for this clip", track.name, str(e)[-300:])
        return None
    log.info("  music: %s from %.1f s%s", track.name, offset, " (looped)" if loop else "")
    return {"track": track.name, "offset": offset}
