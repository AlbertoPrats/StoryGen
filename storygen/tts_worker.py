"""Chatterbox Turbo worker, run with the separate TTS environment's Python (.venv-tts).

Chatterbox pins numpy 1.x and torch 2.6, which clash with the main environment, so it lives in its own
venv and is driven through this script (standard library + chatterbox only; it must not import storygen):

  .venv-tts/bin/python storygen/tts_worker.py job.json

job.json: {"texts": [...], "ref": "voice.wav" | null, "out_dir": "...", "threads": 6}
Writes out_dir/000.wav, 001.wav, ... (mono, the model's sample rate) and prints "done i n" after each,
so the caller can show progress and stop it.

Chatterbox sometimes fails to stop on a line and rambles until its 1000-token limit (minutes on a CPU,
garbage audio). Each line therefore gets a token budget from its word count (speech tokens run at 25/s),
and a take that hits the budget or is far too long for its words is re-rolled with another seed.
"""
import functools
import json
import os
import sys

TOKENS_PER_S = 25          # S3 speech tokens per second of audio


def budget(text: str) -> int:
    """Generous token cap: ~0.75 s per word + 1.5 s, at 25 tokens/s."""
    return int((0.75 * len(text.split()) + 1.5) * TOKENS_PER_S)


def plausible(seconds: float, text: str) -> bool:
    return seconds <= 0.62 * len(text.split()) + 1.2


def main() -> None:
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    import torch
    import torchaudio
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    torch.set_num_threads(int(job.get("threads") or os.cpu_count() or 4))
    model = ChatterboxTurboTTS.from_pretrained(device="cpu")
    if job.get("ref"):
        model.prepare_conditionals(job["ref"])
    out_dir = job["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    n = len(job["texts"])
    plain = model.t3.inference_turbo
    for i, text in enumerate(job["texts"]):
        best = None
        for attempt in range(3):
            torch.manual_seed(1000 * i + attempt)
            model.t3.inference_turbo = functools.partial(plain, max_gen_len=budget(text))
            with torch.inference_mode():
                wav = model.generate(text, temperature=float(job.get("temperature", 0.8)))
            secs = wav.shape[-1] / model.sr
            if best is None or secs < best[1]:
                best = (wav, secs)
            if plausible(secs, text):
                break
            print(f"retry {i + 1} {secs:.1f}s for {len(text.split())} words", file=sys.stderr, flush=True)
        wav = best[0]
        torchaudio.save(os.path.join(out_dir, f"{i:03d}.wav"), wav.cpu(), model.sr)
        print(f"done {i + 1} {n} {model.sr}", flush=True)


if __name__ == "__main__":
    main()
