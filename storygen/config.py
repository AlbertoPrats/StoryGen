"""Configuration: defaults, overridable by a TOML file (storygen.toml) and CLI flags."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


@dataclass
class StoryCfg:
    target_seconds: float = 52.0    # narration length Claude aims for
    max_seconds: float = 58.0       # hard cap for the whole clip (Shorts / Reels / TikTok safe)
    min_seconds: float = 30.0
    words_per_second: float = 2.6   # measured: 157 words took 59.9 s at Kokoro speed 1.1 (incl. pauses)
    review: bool = True             # second Claude pass: a retention editor tightens the script
    # Hashtags: these fixed ones on every clip, then `hashtags` topic-specific ones from Claude. More than 15 in
    # total makes YouTube ignore all of them; the first three show above the title.
    core_hashtags: list = field(default_factory=lambda: [
        "#shorts", "#scarystories", "#horrorstory", "#scary", "#creepy", "#horror", "#storytime"])
    hashtags: int = 2               # topic-specific hashtags after the fixed ones
    # Genres picked at random (one per story) unless the user gives a theme. Variety keeps a feed fresh.
    # Mystery / horror / tension only. One is picked at random per story unless the user gives a theme.
    genres: list = field(default_factory=lambda: [
        "finding out a partner is cheating through one small wrong detail, and the truth is worse",
        "someone has been secretly living in the narrator's house",
        "a family secret uncovered by accident (a hidden sibling, a fake identity, a buried past)",
        "a stranger who knows things about the narrator they could not possibly know",
        "a night-shift or babysitting job with strange rules that the narrator breaks",
        "a message, call or voicemail from someone who should not be able to send it",
        "a best friend or partner living a double life",
        "something deeply wrong with the new neighbour",
        "a missing person case the narrator gets pulled into",
        "a remote cabin, road trip or hike where the narrator realises they are being followed",
        "a smart device, app or old phone that reveals a dark secret",
        "a supernatural event that turns out to be human, which is scarier",
    ])
    avoid_recent: int = 40          # recent premises sent to Claude so it does not repeat itself


@dataclass
class VoiceCfg:
    # chatterbox: Chatterbox Turbo (MIT), expressive, in its own venv (.venv-tts), ~4x slower than real time
    #             on a 6-core CPU. kokoro: Kokoro-82M (Apache-2.0), fast but flatter; also the fallback.
    engine: str = "chatterbox"
    # Narrator gender for every story: "male" (always the Chatterbox copy of am_onyx), "female", or "auto"
    # (Claude picks per story).
    narrator: str = "male"
    chatterbox_python: str = ".venv-tts/bin/python"
    # Chatterbox copies the voice of a short reference clip made once with this Kokoro voice
    # ("builtin" = Chatterbox's own voice).
    chatterbox_male: list = field(default_factory=lambda: ["am_onyx"])
    chatterbox_female: list = field(default_factory=lambda: ["af_heart"])
    max_stretch: float = 1.15       # Chatterbox has no speed control: fastest time-stretch to fit max_seconds
    threads: int = 6
    model: str = "assets/models/kokoro-v1.0.int8.onnx"
    voices_file: str = "assets/models/voices-v1.0.bin"
    male: list = field(default_factory=lambda: ["am_michael", "am_adam", "am_onyx", "bm_george"])
    female: list = field(default_factory=lambda: ["af_heart", "af_bella", "af_nicole", "bf_emma"])
    speed: float = 1.1              # Kokoro speed
    max_speed: float = 1.3          # fastest Kokoro speed used to squeeze a long script under max_seconds
    sentence_pause: float = 0.12    # silence between sentences
    paragraph_pause: float = 0.35   # silence after a beat change (blank line in the script)
    align: bool = True              # word timings from faster-whisper (else spread by letters)
    whisper_model: str = "base"


@dataclass
class SceneCfg:
    style: str = "random"           # random | layers | escape (see scenes.STYLES)
    fps: int = 30
    width: int = 1080
    height: int = 1920
    center_y: float = 0.40          # arena centre (fraction of height); captions sit below it
    radius: float = 0.44            # outermost ring radius (fraction of width)
    ball_radius: float = 20.0
    gravity: float = 1500.0         # px/s^2
    ring_gap: float = 30.0          # px between rings
    ring_width: float = 9.0
    shrink_speed: float = 30.0      # px/s: rings slowly close in, new ones appear outside
    close_in: float = 0.8           # extra px/s of shrink per px the inner ring is beyond inner_target
    inner_target: float = 0.45      # innermost ring radius the arena drifts towards (fraction of radius)
    min_speed: float = 750.0        # the ball never slows below this (keeps it lively)
    max_speed: float = 2100.0
    escape_gap_deg: float = 65.0    # "escape" style: opening in each ring
    sfx_volume: float = 0.10        # bounce/break tones under the voice (0.22 until session 4: too loud)
    drone: float = 0.18             # low synth drone, only when there is no music (0 = off)
    crf: int = 20
    preset: str = "veryfast"


@dataclass
class MusicCfg:
    enabled: bool = True
    dir: str = "assets/music"       # CC0 tracks from tracks.tsv (install.sh) + any audio file put there
    volume: float = 0.22            # gain after normalising each track to -20 LUFS (~14 dB under the voice)
    duck: float = 2.5               # sidechain ratio while the narrator speaks (1 = no ducking)
    fade_in: float = 0.6            # seconds; short so the hook starts with the music already there


@dataclass
class CaptionCfg:
    font: str = "Anton"
    size: int = 104
    color: str = "#FFFFFF"
    highlight: str = "#FFE135"
    outline: int = 7
    words_per_group: int = 3
    max_chars: int = 16
    position_y: float = 0.775       # caption centre, fraction of height (below the arena)
    title_box: bool = True          # the story title in a white box at the top
    title_size: int = 62


@dataclass
class LLMCfg:
    backend: str = "claude"         # claude | ollama
    model: str | None = None        # claude model alias (e.g. "sonnet"); None = account default
    timeout: int = 900
    retries: int = 2
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    fallback: bool = True
    force_subscription: bool = True # strip ANTHROPIC_API_KEY so the Pro plan login is used


@dataclass
class TelegramCfg:
    token: str = ""                 # a NEW bot from @BotFather (not the clipgen one), or env STORYGEN_BOT_TOKEN
    allowed_users: list = field(default_factory=list)
    api_url: str = "https://api.telegram.org"
    max_upload_mb: float = 49.0
    poll_timeout: int = 50


@dataclass
class Config:
    work_dir: str = "work"
    output_dir: str = "output"
    count: int = 1                  # stories per request
    channel_handle: str = ""
    story: StoryCfg = field(default_factory=StoryCfg)
    voice: VoiceCfg = field(default_factory=VoiceCfg)
    scene: SceneCfg = field(default_factory=SceneCfg)
    music: MusicCfg = field(default_factory=MusicCfg)
    captions: CaptionCfg = field(default_factory=CaptionCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else ROOT / q


def _merge(obj, data: dict):
    for f in fields(obj):
        if f.name not in data:
            continue
        cur = getattr(obj, f.name)
        if is_dataclass(cur) and isinstance(data[f.name], dict):
            _merge(cur, data[f.name])
        else:
            setattr(obj, f.name, data[f.name])
    unknown = set(data) - {f.name for f in fields(obj)}
    if unknown:
        raise ValueError(f"Unknown config keys for {type(obj).__name__}: {sorted(unknown)}")
    return obj


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    candidates = [Path(path)] if path else [Path.cwd() / "storygen.toml", ROOT / "storygen.toml"]
    for c in candidates:
        if c.is_file():
            with open(c, "rb") as fh:
                _merge(cfg, tomllib.load(fh))
            break
    return cfg
