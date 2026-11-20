"""Offline tests: no Claude, no Kokoro model, no network."""
from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from storygen import pipeline, story as story_mod, voice
from storygen.captions import build_ass, group_words
from storygen.config import CaptionCfg, Config, SceneCfg
from storygen.scenes import STYLES, pick_style, simulate
from storygen.sound import track
from storygen.telegram import parse_request

STORY = {
    "title": "My neighbor mows his lawn at 3 AM",
    "premise": "A neighbor mows at night to hide a sound; it is his own snoring recorded by his wife.",
    "narrator": "male",
    "script": "My neighbor mows his lawn at exactly 3:12 AM.\n\nEvery night. Same time.\n\n"
              "Last night I finally went outside. The mower was off.",
    "caption": "Nobody mows at 3 AM. Nobody.",
    "hashtags": ["#storytime", "#creepy"],
}


# ---------------------------------------------------------------- story text
def test_tidy_script_keeps_beats_and_drops_markdown():
    raw = '"**Hook** line here.\n\n\n  Second   beat.  \n\n# Third."'
    assert story_mod.tidy_script(raw) == "Hook line here.\n\nSecond beat.\n\nThird."


def test_hashtags_start_with_storytime_unique_and_capped():
    tags = story_mod.clean_hashtags(["Creepy", "#creepy", "plot twist!", *[f"t{i}" for i in range(30)]], 10)
    assert tags[0] == "#storytime" and len(tags) == 10 and len(set(tags)) == 10
    assert "#plottwist" in tags


def test_word_count():
    assert story_mod.word_count("I didn't see it. 3:12 AM! —") == 6


def test_story_state_remembers_recent_premises(tmp_path):
    cfg = Config(work_dir=str(tmp_path))
    st = story_mod.StoryState(cfg)
    st.add(STORY)
    again = story_mod.StoryState(cfg)
    assert again.recent(5) == [f"{STORY['title']} — {STORY['premise']}"]


def test_write_story_sends_genre_and_recent_premises(monkeypatch, tmp_path):
    cfg = Config(work_dir=str(tmp_path))
    cfg.story.target_seconds = 10                  # ~29 words, like the test story
    prompts = []

    def fake_ask(prompt, schema, system, llm_cfg, what=""):
        prompts.append(prompt)
        if "retention editor" in prompt:
            return {"scores": {"hook": 9, "open_loop": 8, "escalation": 8, "twist": 9, "spoken_rhythm": 9,
                               "overall": 6}, "problems": [], "final_score": 9, "title": "Better title",
                    "script": STORY["script"] + " It was never a mower.", "caption": "c"}
        return dict(STORY)

    monkeypatch.setattr(story_mod, "ask_json", fake_ask)
    st = story_mod.StoryState(cfg)
    st.add({"title": "Old", "premise": "an old premise"})
    s = story_mod.write_story(cfg, rng=random.Random(1), state=st)
    assert "Genre:" in prompts[0] and "an old premise" in prompts[0]
    assert s["title"] == "Better title" and s["review"]["final_score"] == 9
    assert s["script"].endswith("It was never a mower.")
    themed = story_mod.write_story(cfg, theme="a locked attic", rng=random.Random(1), state=st)
    assert "a locked attic" in prompts[2] and themed["theme"] == "a locked attic"


# ---------------------------------------------------------------- voice + alignment
def test_split_sentences_marks_paragraph_ends():
    sents = voice.split_sentences(STORY["script"])
    assert [s for s, _ in sents] == ["My neighbor mows his lawn at exactly 3:12 AM.", "Every night.", "Same time.",
                                     "Last night I finally went outside.", "The mower was off."]
    assert [p for _, p in sents] == [True, False, True, False, True]


def test_align_uses_whisper_times_and_fills_gaps():
    sentences = [{"text": "The mower was off.", "t0": 1.0, "t1": 2.0},
                 {"text": "Nobody was there.", "t0": 2.3, "t1": 3.3}]
    heard = [{"w": "The", "t0": 1.0, "t1": 1.2}, {"w": "mover", "t0": 1.2, "t1": 1.5},   # misheard
             {"w": "was", "t0": 1.5, "t1": 1.7}, {"w": "off.", "t0": 1.7, "t1": 2.0},
             {"w": "Nobody", "t0": 2.3, "t1": 2.7}, {"w": "there", "t0": 2.9, "t1": 3.3}]
    words = voice.align(sentences, heard)
    assert [w["w"] for w in words] == ["The", "mower", "was", "off.", "Nobody", "was", "there."]
    assert words[1]["t0"] == pytest.approx(1.2) and words[1]["t1"] == pytest.approx(1.5)
    assert words[0]["t0"] == pytest.approx(1.0) and words[4]["t0"] == pytest.approx(2.3)
    assert 2.7 <= words[5]["t0"] < words[5]["t1"] <= 2.9 + 1e-6
    for a, b in zip(words, words[1:]):
        assert a["t0"] <= b["t0"]
    for w in words:
        s = sentences[w["sentence"]]
        assert s["t0"] <= w["t0"] < w["t1"] <= s["t1"]


def test_align_without_whisper_spreads_by_letters():
    words = voice.align([{"text": "I ran extraordinarily fast.", "t0": 0.0, "t1": 2.0}], [])
    assert words[0]["t0"] == 0 and words[-1]["t1"] == pytest.approx(2.0)
    durs = [w["t1"] - w["t0"] for w in words]
    assert durs[2] == max(durs)                    # the long word gets the most time


class FakeKokoro:
    """Speech-like noise, 0.3 s per word / speed, with silence around it (trimmed by synthesize)."""

    def create(self, text, voice, speed, lang):
        rng = np.random.default_rng(len(text))
        n = int(24000 * 0.3 * len(text.split()) / speed)
        sig = (rng.standard_normal(n) * 0.2).astype(np.float32)
        pad = np.zeros(2400, dtype=np.float32)
        return np.concatenate([pad, sig, pad]), 24000


def kokoro_cfg(**kw) -> Config:
    cfg = Config(**kw)
    cfg.voice.engine = "kokoro"
    return cfg


def test_synthesize_trims_and_times_sentences(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "_kokoro", lambda m, v: FakeKokoro())
    cfg = kokoro_cfg(work_dir=str(tmp_path))
    info = voice.synthesize(cfg, STORY["script"], "am_michael", 1.0, tmp_path / "v.wav")
    s = info["sentences"]
    assert len(s) == 5 and s[0]["t0"] == pytest.approx(0.15)
    assert s[0]["t1"] - s[0]["t0"] == pytest.approx(0.3 * 9, abs=0.08)       # silence trimmed
    assert s[2]["t0"] - s[1]["t1"] == pytest.approx(cfg.voice.sentence_pause, abs=0.01)
    assert s[3]["t0"] - s[2]["t1"] == pytest.approx(cfg.voice.paragraph_pause, abs=0.01)
    import soundfile as sf
    audio, sr = sf.read(str(tmp_path / "v.wav"))
    assert sr == 24000 and len(audio) / sr == pytest.approx(info["duration"])


def test_narrate_speeds_up_then_shortens(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "_kokoro", lambda m, v: FakeKokoro())
    cfg = kokoro_cfg(work_dir=str(tmp_path))
    cfg.story.max_seconds = 8.0                    # script is ~ 27 words * 0.3 s = 8.1 s + pauses
    st, info = pipeline.narrate(cfg, dict(STORY), "am_michael", tmp_path / "v.wav")
    assert info["duration"] <= 8.0 - pipeline.TAIL and info["speed"] > cfg.voice.speed
    calls = []

    def fake_shorten(c, s, words):
        calls.append(words)
        return {**s, "script": "My neighbor mows at night. The mower was off."}

    monkeypatch.setattr(pipeline, "shorten_story", fake_shorten)
    cfg.story.max_seconds = 5.0                    # too long even at max speed -> Claude shortens
    st, info = pipeline.narrate(cfg, dict(STORY), "am_michael", tmp_path / "v.wav")
    assert calls and st["script"].startswith("My neighbor mows at night") and info["duration"] <= 5.0


# ---------------------------------------------------------------- chatterbox engine (fake worker)
FAKE_WORKER = """
import json, sys, numpy as np, soundfile as sf, os
job = json.load(open(sys.argv[-1]))
open(os.path.join(os.path.dirname(sys.argv[-1]), "calls.log"), "a").write(str(len(job["texts"])) + "\\n")
for i, t in enumerate(job["texts"]):
    n = int(24000 * 0.3 * len(t.split()))
    sig = (np.random.default_rng(i).standard_normal(n) * 0.2).astype("float32")
    sf.write(os.path.join(job["out_dir"], f"{i:03d}.wav"), np.concatenate([np.zeros(2400, "float32"), sig]), 24000)
    print(f"done {i + 1} {len(job['texts'])} 24000", flush=True)
"""


def chatterbox_cfg(tmp_path, monkeypatch) -> Config:
    import sys
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    py = tmp_path / "fakepython"
    py.write_text(f"#!/bin/sh\nexec {sys.executable} {worker} \"$2\"\n")
    py.chmod(0o755)
    monkeypatch.setattr(voice, "reference_clip", lambda cfg, v: None)
    cfg = Config(work_dir=str(tmp_path / "work"))
    cfg.voice.engine = "chatterbox"
    cfg.voice.chatterbox_python = str(py)
    return cfg


def test_split_beats_one_unit_per_paragraph():
    beats = voice.split_beats(STORY["script"])
    assert [b for b, _ in beats] == ["My neighbor mows his lawn at exactly 3:12 AM.", "Every night. Same time.",
                                     "Last night I finally went outside. The mower was off."]
    long = " ".join(["This sentence has exactly seven words here."] * 8)
    assert all(len(b.split()) <= 40 for b, _ in voice.split_beats(long))


def test_chatterbox_engine_caches_takes_and_stretches(tmp_path, monkeypatch):
    cfg = chatterbox_cfg(tmp_path, monkeypatch)
    seen = []
    info = voice.synthesize(cfg, STORY["script"], "am_onyx", 1.0, tmp_path / "w" / "v.wav", progress=seen.append)
    assert info["engine"] == "chatterbox" and len(info["sentences"]) == 3
    assert seen[-1] == "Recording the voice-over (3/3)"
    fast = voice.synthesize(cfg, STORY["script"], "am_onyx", 1.1, tmp_path / "w" / "v.wav")
    assert fast["duration"] == pytest.approx(info["duration"] / 1.1, rel=0.02)
    assert fast["sentences"][-1]["t1"] == pytest.approx(info["sentences"][-1]["t1"] / 1.1, rel=0.01)
    calls = (tmp_path / "w" / "tts_cache" / "calls.log").read_text().split()
    assert calls == ["3"]                          # the second take reused the cache


def test_narrate_falls_back_to_kokoro_when_chatterbox_breaks(tmp_path, monkeypatch):
    cfg = chatterbox_cfg(tmp_path, monkeypatch)
    (tmp_path / "fakepython").write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    monkeypatch.setattr(voice, "_kokoro", lambda m, v: FakeKokoro())
    st, info = pipeline.narrate(cfg, dict(STORY), "af_heart", tmp_path / "v.wav", engine="chatterbox")
    assert info["engine"] == "kokoro" and info["voice"] == cfg.voice.female[0]


def test_pick_voice_by_engine():
    cfg = Config()
    rng = random.Random(0)
    assert voice.pick_voice(cfg, "female", rng, "chatterbox") in cfg.voice.chatterbox_female
    assert voice.pick_voice(cfg, "male", rng, "kokoro") in cfg.voice.male


# ---------------------------------------------------------------- scene
@pytest.mark.parametrize("style", STYLES)
def test_simulation_keeps_the_ball_inside_and_the_arena_layered(style):
    c = SceneCfg()
    sim = simulate(c, 30, style, seed=7)
    cx, cy = sim.center
    assert len(sim.frames) == 30 * c.fps
    for f in sim.frames:
        d = math.hypot(f.ball[0] - cx, f.ball[1] - cy)
        assert d + c.ball_radius <= f.rings[0][0] + c.ring_width / 2 + 2 if f.rings else True
    rings = [len(f.rings) for f in sim.frames[c.fps * 5:]]
    assert min(rings) >= 3                         # it never runs out of layers
    breaks = [e for e in sim.events if e.kind == "break"]
    assert len(breaks) >= 10
    gaps = [b.t - a.t for a, b in zip(breaks, breaks[1:])]
    assert max(gaps) < 12                          # something breaks regularly
    if style == "escape":
        assert sum(e.kind == "bounce" for e in sim.events) > len(breaks)


def test_simulation_is_deterministic_per_seed():
    a, b = simulate(SceneCfg(), 5, "layers", seed=3), simulate(SceneCfg(), 5, "layers", seed=3)
    assert a.frames[-1].ball == b.frames[-1].ball and len(a.events) == len(b.events)
    c = simulate(SceneCfg(), 5, "layers", seed=4)
    assert c.frames[-1].ball != a.frames[-1].ball


def test_pick_style():
    rng = random.Random(0)
    assert pick_style(SceneCfg(), rng, "escape") == "escape"
    assert {pick_style(SceneCfg(style="random"), rng) for _ in range(40)} == set(STYLES)
    with pytest.raises(ValueError):
        simulate(SceneCfg(), 1, "nope")


def test_hit_sound_track(tmp_path):
    sim = simulate(SceneCfg(), 6, "escape", seed=1)
    out = track(sim.events, 6, 1080, tmp_path / "s.wav", seed=1)
    import soundfile as sf
    audio, sr = sf.read(str(out))
    assert sr == 48000 and audio.shape[1] == 2 and len(audio) / sr >= 6
    assert np.isfinite(audio).all() and 0.3 < np.abs(audio).max() <= 0.51 + 0.18


# ---------------------------------------------------------------- captions
def test_caption_groups_are_short_and_stay_in_one_sentence():
    words = voice.align([{"text": "My neighbor mows his lawn at exactly 3:12 AM.", "t0": 0, "t1": 3},
                         {"text": "Every night.", "t0": 3.2, "t1": 4}], [])
    groups = group_words(words, CaptionCfg())
    assert all(len(g) <= 3 for g in groups)
    assert all(len({w["sentence"] for w in g}) == 1 for g in groups)
    ass = build_ass(words, STORY["title"], 5.0, CaptionCfg())
    assert "Style: Title" in ass and "MY NEIGHBOR" in ass and "\\c&H35E1FF&" in ass
    assert ass.count("Dialogue: 1,") == len(words)


# ---------------------------------------------------------------- telegram wording
def test_parse_request():
    assert parse_request("story") == {"count": None, "scene": None, "theme": None}
    assert parse_request("story n3 scene2") == {"count": 3, "scene": "escape", "theme": None}
    assert parse_request("/story scene1") == {"count": None, "scene": "layers", "theme": None}
    assert parse_request("a babysitter finds a second staircase n2") == \
        {"count": 2, "scene": None, "theme": "a babysitter finds a second staircase"}
    assert parse_request("n11 heist")["count"] is None
    assert parse_request("scene3 haunted")["scene"] is None


# ---------------------------------------------------------------- end to end (offline)
def test_make_story_end_to_end_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "_kokoro", lambda m, v: FakeKokoro())
    cfg = kokoro_cfg(work_dir=str(tmp_path / "work"), output_dir=str(tmp_path / "out"))
    cfg.voice.align = False
    short = {**STORY, "script": "My neighbor mows at night.\n\nThe mower was off."}
    meta = pipeline.make_story(cfg, story=short, style="layers", seed=11)
    from storygen.utils import media_info
    info = media_info(meta["video"])
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert info["duration"] == pytest.approx(meta["duration"], abs=0.15)
    assert Path(meta["thumbnail"]).is_file()
    saved = json.loads((Path(meta["video"]).parent / "story.json").read_text())
    assert saved["style"] == "layers" and saved["title"] == STORY["title"] and saved["rings_broken"] > 0
