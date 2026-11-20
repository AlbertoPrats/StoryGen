"""Stage 3 - the relaxing but attention-grabbing background: a ball bouncing inside layered rings.

Styles (STYLES):
  layers  - every ring the ball touches shatters; the ball bounces off it and flies on to the next one.
  escape  - each ring has a rotating opening; the ball bounces off the solid part and the ring only
            shatters when the ball slips through the opening.

The rings slowly close in and new ones fade in at the outside, so any duration works and the arena
never runs out of layers. `simulate()` is pure and deterministic for a seed (it returns per-frame
states plus the hit/break events the sound track is built from); `render_frames()` draws them.
"""
from __future__ import annotations

import colorsys
import math
import random
from dataclasses import dataclass, field

import numpy as np

from .config import SceneCfg

STYLES = ("layers", "escape")
SUBSTEPS = 8
TRAIL = 14


@dataclass
class Ring:
    r: float
    hue: float
    rot: float = 0.0          # rad, centre of the opening (escape style)
    spin: float = 0.0         # rad/s
    gap: float = 0.0          # rad, 0 = closed ring
    born: float = 0.0
    flash: float = -9.0       # time of the last bounce on it


@dataclass
class Frame:
    t: float
    ball: tuple[float, float]
    trail: list[tuple[float, float]]
    rings: list[tuple[float, float, float, float, float, float]]   # r, hue, rot, gap, alpha, flash age
    ball_hue: float


@dataclass
class Event:
    t: float
    kind: str                 # "bounce" | "break"
    x: float
    y: float
    hue: float
    r: float = 0.0            # ring radius (break)
    n: int = 0                # running count of this kind


@dataclass
class Sim:
    style: str
    frames: list[Frame] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    center: tuple[float, float] = (0.0, 0.0)


def pick_style(cfg: SceneCfg, rng: random.Random, wanted: str | None = None) -> str:
    style = wanted or cfg.style
    return rng.choice(STYLES) if style in ("random", "", None) else style


def simulate(cfg: SceneCfg, duration: float, style: str = "layers", seed: int = 0) -> Sim:
    if style not in STYLES:
        raise ValueError(f"unknown scene style {style!r}; choose one of {STYLES}")
    rng = random.Random(seed)
    W, H = cfg.width, cfg.height
    cx, cy = W / 2, H * cfg.center_y
    R_out = W * cfg.radius
    br = cfg.ball_radius
    hue0 = rng.random()
    counter = [0]

    def new_ring(r: float, t: float) -> Ring:
        counter[0] += 1
        ring = Ring(r=r, hue=(hue0 + counter[0] * 0.031) % 1.0, born=t)
        if style == "escape":
            ring.gap = math.radians(cfg.escape_gap_deg)
            ring.rot = rng.uniform(0, 2 * math.pi)
            ring.spin = (1 if counter[0] % 2 else -1) * rng.uniform(0.8, 1.6)
        return ring

    first = br * 5
    target = R_out * cfg.inner_target
    rings = [new_ring(r, -1.0) for r in np.arange(first, R_out + 0.1, cfg.ring_gap)]
    p = np.array([cx + rng.uniform(-10, 10), cy - first * 0.3])
    ang = rng.uniform(0, 2 * math.pi)
    v = np.array([math.cos(ang), math.sin(ang)]) * cfg.min_speed
    ball_hue = rings[0].hue
    sim = Sim(style=style, center=(cx, cy))
    trail: list[tuple[float, float]] = []
    dt = 1.0 / cfg.fps / SUBSTEPS
    n_frames = int(math.ceil(duration * cfg.fps))
    counts = {"bounce": 0, "break": 0}

    def event(kind: str, t: float, ring: Ring) -> None:
        counts[kind] += 1
        sim.events.append(Event(t, kind, float(p[0]), float(p[1]), ring.hue, ring.r, counts[kind]))

    for f in range(n_frames):
        for s in range(SUBSTEPS):
            t = (f * SUBSTEPS + s) * dt
            v[1] += cfg.gravity * dt
            p += v * dt
            # close in faster while the ball has a lot of room, slower when it is cramped, so the
            # arena keeps about the same number of layers whatever the break rate
            room = (rings[0].r if rings else R_out) - target
            shrink = max(cfg.shrink_speed * 0.3, cfg.shrink_speed + cfg.close_in * room)
            for ring in rings:
                ring.r -= shrink * dt
                ring.rot += ring.spin * dt
            if not rings or rings[-1].r < R_out - cfg.ring_gap:
                rings.append(new_ring(R_out, t))
            inner = rings[0]
            d_vec = p - (cx, cy)
            d = float(np.hypot(*d_vec)) or 1e-6
            n = d_vec / d
            if d + br >= inner.r - cfg.ring_width / 2:
                theta = math.atan2(n[1], n[0])
                in_gap = inner.gap > 0 and abs(_wrap(theta - inner.rot)) < inner.gap / 2 - br / max(inner.r, 1)
                squeezed = inner.r < br * 2.4
                if in_gap or squeezed:
                    event("break", t, inner)        # escape: through the opening
                    ball_hue = inner.hue
                    rings.pop(0)
                    continue
                vn = float(v @ n)
                if vn > 0:
                    v -= 2 * vn * n
                    # a little sideways kick so the path never settles into a loop
                    tang = np.array([-n[1], n[0]])
                    v += tang * rng.uniform(-90, 90)
                p = np.array([cx, cy]) + n * (inner.r - cfg.ring_width / 2 - br - 0.5)
                if style == "layers":
                    event("break", t, inner)
                    ball_hue = inner.hue
                    rings.pop(0)
                elif vn > 0:
                    inner.flash = t
                    event("bounce", t, inner)
                speed = float(np.hypot(*v))
                if speed < cfg.min_speed:
                    v *= cfg.min_speed / max(speed, 1e-6)
                elif speed > cfg.max_speed:
                    v *= cfg.max_speed / speed
        t = (f + 1) / cfg.fps
        trail = (trail + [(float(p[0]), float(p[1]))])[-TRAIL:]
        sim.frames.append(Frame(
            t=f / cfg.fps, ball=(float(p[0]), float(p[1])), trail=list(trail), ball_hue=ball_hue,
            rings=[(rg.r, rg.hue, rg.rot, rg.gap, min(1.0, max(0.0, (t - rg.born) / 0.6)),
                    t - rg.flash) for rg in rings]))
    return sim


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def bgr(hue: float, s: float = 0.75, v: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, s, v)
    return int(b * 255), int(g * 255), int(r * 255)


def background(W: int, H: int, hue: float) -> np.ndarray:
    """Dark vertical gradient tinted with the scene's hue, plus a vignette."""
    top = np.array(bgr(hue, 0.55, 0.16), dtype=np.float32)
    bot = np.array(bgr(hue + 0.08, 0.6, 0.05), dtype=np.float32)
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    img = top * (1 - y) + bot * y
    img = np.repeat(img, W, axis=1)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    vig = 1 - 0.45 * (((xx - W / 2) / W) ** 2 + ((yy - H * 0.42) / H) ** 2) * 2.2
    return np.clip(img * vig[..., None], 0, 255).astype(np.uint8)


def render_frames(sim: Sim, cfg: SceneCfg, seed: int = 0):
    """Yields BGR uint8 frames (H, W, 3)."""
    import cv2

    W, H = cfg.width, cfg.height
    cx, cy = sim.center
    S = 16                                        # cv2 sub-pixel shift 4 -> coordinates * 16
    hue = sim.frames[0].rings[0][1] if sim.frames and sim.frames[0].rings else 0.6
    bg = background(W, H, hue)
    breaks = [e for e in sim.events if e.kind == "break"]
    bi = 0
    live: list[tuple[Event, list]] = []           # recent breaks and their fragments
    width = max(1, int(round(cfg.ring_width)))
    for fr in sim.frames:
        img = bg.copy()
        while bi < len(breaks) and breaks[bi].t <= fr.t:
            e = breaks[bi]
            rng = random.Random(seed * 100003 + bi)
            frags = []
            for _ in range(46):
                a = rng.uniform(0, 2 * math.pi)
                spd = rng.uniform(150, 520)
                frags.append((a, spd, rng.uniform(-8, 8), rng.uniform(10, 28)))
            live.append((e, frags))
            bi += 1
        live = [(e, fs) for e, fs in live if fr.t - e.t < 1.1]
        for r, h, rot, gap, alpha, flash_age in fr.rings:
            if r <= 0:
                continue
            col = bgr(h, 0.7, 1.0)
            if flash_age < 0.12:
                col = tuple(int(c + (255 - c) * (1 - flash_age / 0.12)) for c in col)
            col = tuple(int(c * alpha) for c in col)
            if gap > 0:
                start = math.degrees(rot + gap / 2)
                cv2.ellipse(img, (int(cx * S), int(cy * S)), (int(r * S), int(r * S)), 0, start,
                            start + 360 - math.degrees(gap), col, width, cv2.LINE_AA, 4)
            else:
                cv2.circle(img, (int(cx * S), int(cy * S)), int(r * S), col, width, cv2.LINE_AA, 4)
        for e, frags in live:                     # shattered ring: arcs flying outwards, falling
            age = fr.t - e.t
            fade = max(0.0, 1 - age / 1.1)
            col = tuple(int(c * fade) for c in bgr(e.hue, 0.6, 1.0))
            for a, spd, spin, length in frags:
                ox = cx + math.cos(a) * (e.r + spd * age)
                oy = cy + math.sin(a) * (e.r + spd * age) + 700 * age * age
                d = a + math.pi / 2 + spin * age
                dx, dy = math.cos(d) * length / 2, math.sin(d) * length / 2
                cv2.line(img, (int((ox - dx) * S), int((oy - dy) * S)), (int((ox + dx) * S), int((oy + dy) * S)),
                         col, max(1, width - 3), cv2.LINE_AA, 4)
        bcol = bgr(fr.ball_hue, 0.6, 1.0)
        for k, (x, y) in enumerate(fr.trail[:-1]):            # comet trail
            f = (k + 1) / len(fr.trail)
            cv2.circle(img, (int(x * S), int(y * S)), int(cfg.ball_radius * (0.35 + 0.55 * f) * S),
                       tuple(int(c * f * 0.55) for c in bcol), -1, cv2.LINE_AA, 4)
        bx, by = fr.ball
        cv2.circle(img, (int(bx * S), int(by * S)), int(cfg.ball_radius * S), (255, 255, 255), -1, cv2.LINE_AA, 4)
        cv2.circle(img, (int(bx * S), int(by * S)), int(cfg.ball_radius * S), bcol, 4, cv2.LINE_AA, 4)
        small = cv2.resize(img, (W // 4, H // 4), interpolation=cv2.INTER_AREA)   # cheap neon glow
        glow = cv2.GaussianBlur(small, (0, 0), 6)
        img = cv2.addWeighted(img, 1.0, cv2.resize(glow, (W, H), interpolation=cv2.INTER_LINEAR), 0.9, 0)
        yield img
