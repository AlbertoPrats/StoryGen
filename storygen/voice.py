"""Stage 2 - narrate the script offline and time every word for the captions.

Engines ([voice].engine):
  chatterbox - Chatterbox Turbo (MIT), expressive; runs in its own venv through tts_worker.py, one call
               per beat (paragraph) so the delivery flows; ~4x slower than real time on this CPU. It can
               copy a voice from a short reference clip, which is made once with a Kokoro voice, so there
               are male and female narrators. No speed control: a light time-stretch fits long takes.
  kokoro     - Kokoro-82M (Apache-2.0) through kokoro-onnx, one call per sentence, ~2x faster than real
               time, flatter delivery. Also the fallback when Chatterbox is not installed or fails.

Each unit is trimmed to its voiced part and joined with short pauses, so unit boundaries in the track are
known exactly. Inside a unit the word timings come from faster-whisper on the finished track, aligned to the
script words (the script's spelling is kept); unmatched words are spread by letter count.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import Config
from .utils import log

SR = 24000
WORD_RE = re.compile(r"\S+")


def split_sentences(script: str) -> list[tuple[str, bool]]:
    """[(sentence, ends_paragraph)] in reading order."""
    out = []
    for para in re.split(r"\n\s*\n", script.strip()):
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])[\"”’)]*\s+(?=[\"“‘(]?[A-Z0-9])", para.strip()) if p.strip()]
        for k, p in enumerate(parts):
            out.append((p, k == len(parts) - 1))
    return out


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower().replace("’", "'"))


@lru_cache(maxsize=2)
def _kokoro(model: str, voices: str):
    from kokoro_onnx import Kokoro
    return Kokoro(model, voices)


def split_beats(script: str, max_words: int = 40) -> list[tuple[str, bool]]:
    """[(beat, True)]: one unit per paragraph; long paragraphs are cut at sentence ends (max_words)."""
    out = []
    for para in re.split(r"\n\s*\n", script.strip()):
        sents = [p for p, _ in split_sentences(para)]
        cur: list[str] = []
        for sent in sents:
            if cur and len(" ".join(cur + [sent]).split()) > max_words:
                out.append((" ".join(cur), False))
                cur = []
            cur.append(sent)
        if cur:
            out.append((" ".join(cur), True))
    return out


def engine_of(cfg: Config) -> str:
    """The engine that will really run: chatterbox only when its environment exists."""
    if cfg.voice.engine == "chatterbox" and cfg.path(cfg.voice.chatterbox_python).exists():
        return "chatterbox"
    if cfg.voice.engine == "chatterbox":
        log.warning("Chatterbox environment %s missing; using Kokoro", cfg.voice.chatterbox_python)
    return "kokoro"


def pick_voice(cfg: Config, narrator: str, rng, engine: str | None = None) -> str:
    v = cfg.voice
    if (engine or engine_of(cfg)) == "chatterbox":
        pool = v.chatterbox_female if narrator == "female" else v.chatterbox_male
    else:
        pool = v.female if narrator == "female" else v.male
    return rng.choice(pool)


def reference_clip(cfg: Config, voice: str) -> Path | None:
    """Reference recording for Chatterbox to copy: made once with the Kokoro voice of that name.
    "builtin" = Chatterbox's own voice (no reference)."""
    if voice == "builtin":
        return None
    import soundfile as sf

    ref = cfg.path(cfg.work_dir) / "voices" / f"ref_{voice}.wav"
    if not ref.exists():
        k = _kokoro(str(cfg.path(cfg.voice.model)), str(cfg.path(cfg.voice.voices_file)))
        text = ("I never believed in any of it. Not ghosts, not curses, not the stories my grandmother told. "
                "Then one night, the phone rang, and the voice on the other end knew my name.")
        samples, sr = k.create(text, voice=voice, speed=1.0, lang="en-us")
        ref.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(ref), samples, sr)
    return ref


def chatterbox_units(cfg: Config, texts: list[str], voice: str, cache: Path, progress=None) -> list[tuple]:
    """[(samples, sr)] per text. Results are cached by (voice, text), so re-fitting never re-records."""
    import soundfile as sf

    cache.mkdir(parents=True, exist_ok=True)
    keys = [hashlib.sha1(f"{voice}|{t}".encode()).hexdigest()[:16] for t in texts]
    todo = [i for i, k in enumerate(keys) if not (cache / f"{k}.wav").exists()]
    if todo:
        job_dir = cache / "job"
        job_dir.mkdir(exist_ok=True)
        for f in job_dir.glob("*.wav"):
            f.unlink()
        ref = reference_clip(cfg, voice)
        job = {"texts": [texts[i] for i in todo], "ref": str(ref) if ref else None, "out_dir": str(job_dir),
               "threads": cfg.voice.threads}
        (cache / "job.json").write_text(json.dumps(job), encoding="utf-8")
        worker = Path(__file__).with_name("tts_worker.py")
        # stderr (progress bars, warnings) goes to a file: a pipe nobody reads fills up and freezes the worker
        with open(cache / "worker.log", "w") as errlog:
            proc = subprocess.Popen([str(cfg.path(cfg.voice.chatterbox_python)), str(worker), str(cache / "job.json")],
                                    stdout=subprocess.PIPE, stderr=errlog, text=True)
            try:
                for line in proc.stdout:
                    if line.startswith("done ") and progress:
                        _, i, n, _sr = line.split()
                        progress(f"Recording the voice-over ({i}/{n})")
            except BaseException:
                proc.kill()                   # /stop or an error: do not leave the model running
                raise
            code = proc.wait()
        if code != 0:
            err = (cache / "worker.log").read_text(errors="replace")
            raise RuntimeError(f"Chatterbox failed: {err[-1500:]}")
        for j, i in enumerate(todo):
            (job_dir / f"{j:03d}.wav").replace(cache / f"{keys[i]}.wav")
    out = []
    for k in keys:
        samples, sr = sf.read(str(cache / f"{k}.wav"), dtype="float32")
        out.append((samples if samples.ndim == 1 else samples.mean(axis=1), sr))
    return out


def voiced_span(audio: np.ndarray, sr: int = SR, thresh_db: float = -42.0) -> tuple[int, int]:
    """First and last sample of the voiced part (10 ms RMS above the threshold, relative to the peak)."""
    hop = sr // 100
    n = len(audio) // hop
    if n == 0:
        return 0, len(audio)
    frames = audio[: n * hop].reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms / (rms.max() + 1e-12))
    on = np.nonzero(db > thresh_db)[0]
    if len(on) == 0:
        return 0, len(audio)
    a = max(0, (on[0] - 2) * hop)
    b = min(len(audio), (on[-1] + 4) * hop)
    return a, b


def stretch(audio: np.ndarray, factor: float) -> np.ndarray:
    """Speed up by `factor` without changing pitch (ffmpeg atempo)."""
    if abs(factor - 1.0) < 1e-3:
        return audio
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(SR),
                           "-ac", "1", "-i", "-", "-af", f"atempo={factor:.4f}", "-f", "f32le", "-"],
                          input=audio.astype(np.float32).tobytes(), capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def synthesize(cfg: Config, script: str, voice: str, speed: float, out_wav: Path,
               tts=None, engine: str | None = None, progress=None) -> dict:
    """Writes the narration WAV. Returns {duration, sentences: [{text, t0, t1}], voice, speed, engine}.
    `speed` is the Kokoro speed, or the time-stretch factor for Chatterbox.
    `tts(text, voice, speed) -> (samples, sr)` can be injected (tests; Kokoro-style)."""
    import soundfile as sf

    engine = "kokoro" if tts is not None else (engine or engine_of(cfg))
    if engine == "chatterbox":
        units = split_beats(script)
        made = chatterbox_units(cfg, [u for u, _ in units], voice, out_wav.parent / "tts_cache", progress)
    else:
        if tts is None:
            k = _kokoro(str(cfg.path(cfg.voice.model)), str(cfg.path(cfg.voice.voices_file)))
            tts = lambda text, v, s: k.create(text, voice=v, speed=s, lang="en-us")  # noqa: E731
        units = split_sentences(script)
        made = [tts(text, voice, speed) for text, _ in units]
    pieces, sentences, t = [], [], 0.0
    lead = np.zeros(int(0.15 * SR), dtype=np.float32)       # a breath before the first word
    pieces.append(lead)
    t += len(lead) / SR
    for (text, para_end), (samples, sr) in zip(units, made):
        samples = np.asarray(samples, dtype=np.float32)
        if sr != SR:                                          # work at 24 kHz; resample anything else
            idx = np.linspace(0, len(samples) - 1, int(len(samples) * SR / sr))
            samples = np.interp(idx, np.arange(len(samples)), samples).astype(np.float32)
        a, b = voiced_span(samples)
        samples = samples[a:b]
        sentences.append({"text": text, "t0": t, "t1": t + len(samples) / SR})
        pieces.append(samples)
        t += len(samples) / SR
        pause = cfg.voice.paragraph_pause if para_end else cfg.voice.sentence_pause
        pieces.append(np.zeros(int(pause * SR), dtype=np.float32))
        t += pause
    audio = np.concatenate(pieces)
    if engine == "chatterbox" and abs(speed - 1.0) > 1e-3:
        audio = stretch(audio, speed)
        for s_ in sentences:
            s_["t0"], s_["t1"] = s_["t0"] / speed, s_["t1"] / speed
    peak = float(np.abs(audio).max() or 1.0)
    audio = (audio / peak * 0.89).astype(np.float32)          # -1 dBFS peak; loudness is set at the mix
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), audio, SR)
    return {"duration": len(audio) / SR, "sentences": sentences, "voice": voice, "speed": speed, "engine": engine}


def spread(words: list[str], t0: float, t1: float) -> list[tuple[float, float]]:
    """Times for words spoken between t0 and t1, proportional to their letter count."""
    weights = np.array([max(len(norm(w)), 1) + 1.5 for w in words], dtype=float)
    edges = t0 + (t1 - t0) * np.concatenate([[0], np.cumsum(weights)]) / weights.sum()
    return [(float(edges[i]), float(edges[i + 1])) for i in range(len(words))]


def align(sentences: list[dict], heard: list[dict]) -> list[dict]:
    """Script words with times. `heard` = whisper words [{w, t0, t1}] (may be empty)."""
    script = []                                   # [(word, sentence index)]
    for si, s in enumerate(sentences):
        script += [(w, si) for w in WORD_RE.findall(s["text"])]
    a = [norm(w) for w, _ in script]
    b = [norm(h["w"]) for h in heard]
    times: list[tuple[float, float] | None] = [None] * len(script)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            i, j = blk.a + k, blk.b + k
            s = sentences[script[i][1]]
            h = heard[j]
            if s["t0"] - 0.3 <= h["t0"] <= s["t1"] + 0.3:      # a match in the wrong sentence is noise
                times[i] = (max(h["t0"], s["t0"]), min(max(h["t1"], h["t0"] + 0.05), s["t1"]))
    out = []
    for si, s in enumerate(sentences):
        idx = [i for i, (_, k) in enumerate(script) if k == si]
        i = 0
        while i < len(idx):
            if times[idx[i]] is not None:
                i += 1
                continue
            j = i
            while j < len(idx) and times[idx[j]] is None:     # a run of unmatched words
                j += 1
            lo = times[idx[i - 1]][1] if i > 0 else s["t0"]
            hi = times[idx[j]][0] if j < len(idx) else s["t1"]
            if hi - lo < 0.06 * (j - i):                      # no room: borrow from the neighbours
                lo = times[idx[i - 1]][0] if i > 0 else s["t0"]
                hi = times[idx[j]][1] if j < len(idx) else s["t1"]
            for k, tt in zip(range(i, j), spread([script[x][0] for x in idx[i:j]], lo, hi)):
                times[idx[k]] = tt
            i = j
        prev = s["t0"]
        for i in idx:                                         # monotonic, inside the sentence
            t0 = min(max(times[i][0], prev), s["t1"])
            t1 = min(max(times[i][1], t0 + 0.04), s["t1"])
            out.append({"w": script[i][0], "t0": round(t0, 3), "t1": round(t1, 3), "sentence": si})
            prev = t0
    return out


def transcribe_words(cfg: Config, wav: Path) -> list[dict]:
    try:
        import soundfile as sf
        from faster_whisper import WhisperModel

        audio, sr = sf.read(str(wav), dtype="float32")
        if sr != 16000:                                      # whisper wants 16 kHz (array input avoids PyAV)
            idx = np.linspace(0, len(audio) - 1, int(len(audio) * 16000 / sr))
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        model = WhisperModel(cfg.voice.whisper_model, device="cpu", compute_type="int8")
        segs, _ = model.transcribe(audio, language="en", word_timestamps=True, beam_size=5)
        return [{"w": w.word.strip(), "t0": float(w.start), "t1": float(w.end)}
                for s in segs for w in (s.words or [])]
    except Exception as e:  # noqa: BLE001 - captions still work from the letter-count spread
        log.warning("Word alignment unavailable (%s); spreading words by letters", e)
        return []


def word_timings(cfg: Config, wav: Path, sentences: list[dict]) -> list[dict]:
    heard = transcribe_words(cfg, wav) if cfg.voice.align else []
    words = align(sentences, heard)
    matched = sum(1 for _ in heard)
    log.info("  %d script words timed (%d heard by whisper)", len(words), matched)
    return words
