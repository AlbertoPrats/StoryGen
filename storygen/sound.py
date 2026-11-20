"""Stage 4 - the satisfying hit sounds under the voice, synthesised from the scene's events.

Every hit plays the next note of a slow minor-pentatonic melody (soft mallet tone), so the bounces turn
into an uneasy tune instead of random clicks; a ring breaking adds a glassy bell and a short shimmer of
filtered noise. Under it all runs a quiet low drone (root + fifth, slowly beating) for dread. Panned by the
ball's x position. No downloads, no licences.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np

from .scenes import Event

SR = 48000
# A minor pentatonic over two octaves, walked up and down like a music box (horror stories: no major key)
SCALE = [57, 60, 62, 64, 67, 69, 72, 74, 76, 79]
MELODY = SCALE + SCALE[-2:0:-1]


def midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def mallet(freq: float, dur: float = 0.55) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    env = np.exp(-t * 7.5) * (1 - np.exp(-t * 900))
    tone = np.sin(2 * math.pi * freq * t) + 0.35 * np.sin(2 * math.pi * 2 * freq * t) * np.exp(-t * 14)
    tone += 0.12 * np.sin(2 * math.pi * 3.01 * freq * t) * np.exp(-t * 25)
    return (tone * env).astype(np.float32)


def bell(freq: float, rng: random.Random, dur: float = 1.2) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for ratio, amp, decay in ((1.0, 1.0, 3.2), (2.76, 0.45, 5.0), (5.40, 0.25, 8.0), (8.93, 0.12, 12.0)):
        out += amp * np.sin(2 * math.pi * freq * ratio * t + rng.random()) * np.exp(-t * decay)
    out *= 1 - np.exp(-t * 1500)
    nlen = int(0.18 * SR)                                      # shatter shimmer
    noise = np.array([rng.uniform(-1, 1) for _ in range(nlen)], dtype=np.float64)
    noise = np.diff(noise, prepend=0)                          # crude high-pass: glassy, not hissy
    noise *= np.exp(-np.arange(nlen) / SR * 28) * 0.35
    out[:nlen] += noise
    return (out * 0.6).astype(np.float32)


def drone(duration: float, root: float = 55.0, level: float = 0.18) -> np.ndarray:
    """Low root + fifth, slightly detuned so it beats slowly, with a slow swell. Mono float32."""
    t = np.arange(int(duration * SR)) / SR
    out = (np.sin(2 * math.pi * root * t) + 0.6 * np.sin(2 * math.pi * root * 1.003 * t)
           + 0.45 * np.sin(2 * math.pi * root * 1.5 * t) + 0.2 * np.sin(2 * math.pi * root * 2.01 * t))
    out *= 0.75 + 0.25 * np.sin(2 * math.pi * t / 7.0)
    fade = np.minimum(1.0, np.minimum(t / 2.0, (duration - t) / 1.5).clip(0))
    return (out * fade * level / 2.25).astype(np.float32)


def track(events: list[Event], duration: float, width: int, out_wav: Path, seed: int = 0,
          drone_level: float = 0.18) -> Path:
    """Stereo 48 kHz WAV of all hit sounds (peak-normalised to -6 dBFS; the mix sets the level)."""
    import soundfile as sf

    rng = random.Random(seed)
    n = int((duration + 1.5) * SR)
    buf = np.zeros((n, 2), dtype=np.float32)
    note = 0
    last_t = -1.0
    for e in sorted(events, key=lambda e: e.t):
        if e.t - last_t < 0.045:                               # merge hits closer than 45 ms
            continue
        last_t = e.t
        freq = midi_hz(MELODY[note % len(MELODY)])
        note += 1
        snd = mallet(freq) * (0.8 if e.kind == "bounce" else 0.6)
        if e.kind == "break":
            b = bell(freq * 2, rng)
            pad = np.zeros(max(len(b), len(snd)), dtype=np.float32)
            pad[: len(snd)] += snd
            pad[: len(b)] += b
            snd = pad
        pan = min(max(e.x / max(width, 1), 0.0), 1.0)          # equal-power pan
        gl, gr = math.cos(pan * math.pi / 2), math.sin(pan * math.pi / 2)
        a = int(e.t * SR)
        if a >= n:
            continue
        seg = snd[: n - a]
        buf[a: a + len(seg), 0] += seg * gl
        buf[a: a + len(seg), 1] += seg * gr
    peak = float(np.abs(buf).max())
    if peak > 0:
        buf *= 0.5 / peak
    if drone_level > 0:
        d = drone(n / SR, level=drone_level)
        buf[:, 0] += d
        buf[:, 1] += d
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), buf, SR)
    return out_wav
