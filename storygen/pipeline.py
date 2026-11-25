"""Whole run for one story clip: story -> voice -> word timings -> scene -> sounds -> captions -> render.

Every stage writes into work/stories/<id>/ (story.json, voice.wav, words.json, sfx.wav, captions.ass),
the finished clip goes to output/<id>_<slug>/ with a thumbnail and a JSON with the post text.
"""
from __future__ import annotations

import random
import secrets
import time
from pathlib import Path

from . import music as music_mod
from .captions import build_ass
from .config import Config
from .render import render, thumbnail
from .scenes import pick_style, simulate
from .sound import track
from .story import StoryState, shorten_story, word_count, write_story, youtube_text, youtube_title
from .utils import log, slugify, write_json
from .voice import engine_of, pick_voice, synthesize, word_timings

TAIL = 0.9          # seconds of animation after the last word


def narrate(cfg: Config, story: dict, voice: str, wav: Path, progress=None, tts=None,
            engine: str | None = None) -> tuple[dict, dict]:
    """Synthesise; if the clip would pass max_seconds, speed up (Kokoro: voice speed up to max_speed;
    Chatterbox: time-stretch up to max_stretch, reusing the cached takes), then shorten the script with
    Claude once. If Chatterbox fails, the narration falls back to Kokoro. Returns (story, narration info)."""
    engine = "kokoro" if tts is not None else (engine or engine_of(cfg))
    limit = cfg.story.max_seconds - TAIL
    base = cfg.voice.speed if engine == "kokoro" else 1.0
    top = cfg.voice.max_speed if engine == "kokoro" else cfg.voice.max_stretch
    speed = base
    try:
        info = synthesize(cfg, story["script"], voice, speed, wav, tts=tts, engine=engine, progress=progress)
    except Exception as e:  # noqa: BLE001 - a broken Chatterbox install must not stop the clip
        if engine != "chatterbox":
            raise
        log.warning("Chatterbox failed (%s); falling back to Kokoro", str(e)[-300:])
        # The reference clip was made with a Kokoro voice: fall back to that same voice when there is one.
        if voice != "builtin":
            kvoice = voice
        else:
            kvoice = cfg.voice.female[0] if voice in cfg.voice.chatterbox_female else cfg.voice.male[0]
        return narrate(cfg, story, kvoice, wav, progress, engine="kokoro")
    shortened = False
    for _ in range(4):
        if info["duration"] <= limit:
            return story, info
        if engine == "kokoro":
            speech = sum(s["t1"] - s["t0"] for s in info["sentences"])
            fixed = info["duration"] - speech              # Kokoro pauses do not get faster
            need = speed * speech / max(limit - fixed, 1e-3) * 1.02
        else:
            need = speed * info["duration"] / limit * 1.01  # the stretch shortens everything
        if need <= top:
            speed = need
            log.info("  narration %.1f s > %.1f s: speeding up to %.2f", info["duration"], limit, speed)
        elif not shortened:
            words = int(word_count(story["script"]) * top / need * 0.95)
            if progress:
                progress("Shortening the story")
            story = shorten_story(cfg, story, words)
            speed, shortened = base, True
        else:
            break
        info = synthesize(cfg, story["script"], voice, speed, wav, tts=tts, engine=engine, progress=progress)
    if info["duration"] > limit:
        raise RuntimeError(f"narration is {info['duration']:.0f} s, over the {cfg.story.max_seconds:.0f} s limit")
    return story, info


def make_story(cfg: Config, theme: str | None = None, style: str | None = None, progress=None,
               seed: int | None = None, story: dict | None = None) -> dict:
    """Make one clip. Returns its metadata (video, thumbnail, title, caption, hashtags, ...)."""
    progress = progress or (lambda msg: None)
    seed = seed if seed is not None else secrets.randbits(31)
    rng = random.Random(seed)
    t0 = time.time()
    job = time.strftime("%Y%m%d-%H%M%S") + f"-{seed % 10000:04d}"
    work = cfg.path(cfg.work_dir) / "stories" / job
    work.mkdir(parents=True, exist_ok=True)

    progress("Writing the story")
    state = StoryState(cfg)
    if story is None:
        story = write_story(cfg, theme=theme, rng=rng, state=state)
    write_json(work / "story.json", story)

    progress("Recording the voice-over")
    engine = engine_of(cfg)
    voice = pick_voice(cfg, story.get("narrator", "male"), rng, engine)
    story, narration = narrate(cfg, story, voice, work / "voice.wav", progress, engine=engine)
    voice = narration["voice"]
    write_json(work / "story.json", story)

    progress("Timing the captions")
    words = word_timings(cfg, work / "voice.wav", narration["sentences"])
    write_json(work / "words.json", words)
    duration = narration["duration"] + TAIL

    progress("Animating the background")
    style = pick_style(cfg.scene, rng, style)
    sim = simulate(cfg.scene, duration, style, seed=seed)
    progress("Mixing the music")
    music = music_mod.prepare(cfg, duration, work / "music.wav", rng)
    track(sim.events, duration, cfg.scene.width, work / "sfx.wav", seed=seed,
          drone_level=0.0 if music else cfg.scene.drone)
    ass = work / "captions.ass"
    ass.write_text(build_ass(words, story["title"], duration, cfg.captions, cfg.scene.width, cfg.scene.height),
                   encoding="utf-8")

    yt_title, yt_text = youtube_title(story), youtube_text(story)
    name = slugify(yt_title, 80)                       # the file is named after the clip, not "story.mp4"
    out_dir = cfg.path(cfg.output_dir) / f"{job}_{slugify(yt_title)}"
    video = out_dir / f"{name}.mp4"
    tags = {"title": yt_title, "description": yt_text, "synopsis": yt_text, "comment": story.get("premise"),
            "keywords": ", ".join(t.lstrip("#") for t in story.get("hashtags") or []), "genre": "Horror",
            "artist": cfg.channel_handle, "date": time.strftime("%Y-%m-%d")}
    render(cfg, sim, work / "voice.wav", work / "sfx.wav", ass, duration, video, seed=seed, progress=progress,
           music_wav=work / "music.wav" if music else None, tags=tags)
    thumb = thumbnail(video, out_dir / f"{name}.jpg")

    meta = {
        "rank": 1, "video": str(video), "thumbnail": str(thumb), "title": story["title"],
        "hook": (narration["sentences"] or [{"text": ""}])[0]["text"],
        "youtube_title": yt_title, "description": yt_text,
        "hashtags": story["hashtags"], "script": story["script"], "premise": story["premise"],
        "genre": story.get("genre"), "theme": theme, "review": story.get("review"),
        "voice": voice, "voice_engine": narration["engine"], "voice_speed": round(narration["speed"], 3), "style": style, "seed": seed,
        "music": (music or {}).get("track"),
        "duration": round(duration, 2), "words": word_count(story["script"]),
        "rings_broken": sum(1 for e in sim.events if e.kind == "break"),
        "made_in_s": round(time.time() - t0, 1), "work": str(work),
    }
    if story.get("review"):
        meta["score"] = story["review"].get("final_score")
    write_json(out_dir / "story.json", meta)
    (out_dir / "script.txt").write_text(f"{story['title']}\n\n{story['script']}\n\n", encoding="utf-8")
    (out_dir / "youtube.txt").write_text(f"{meta['youtube_title']}\n\n{meta['description']}\n", encoding="utf-8")
    log.info("Story clip ready: %s (%.1f s, %s, %s) in %.0f s", video, duration, style, voice, time.time() - t0)
    return meta
