"""storygen command line.

  storygen make [-n 2] [--theme "a haunted elevator"] [--style layers|escape]
  storygen make --story work/stories/<id>/story.json     # re-voice and re-render an existing story
  storygen bot                                           # Telegram bot (its own token)
  storygen doctor
"""
from __future__ import annotations

import argparse
import shutil
import sys

from .config import load_config
from .utils import log, read_json, setup_logging


def cmd_make(args, cfg) -> int:
    from .pipeline import make_story

    story = read_json(args.story) if args.story else None
    n = args.count or cfg.count
    ok = 0
    for i in range(n):
        try:
            meta = make_story(cfg, theme=args.theme, style=args.style, story=story,
                              progress=lambda m: log.info("· %s", m))
            print(f"{meta['video']}  ({meta['duration']} s, {meta['style']}, {meta['voice']})  {meta['title']}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.error("Story %d/%d failed: %s", i + 1, n, e)
            if args.verbose:
                raise
    return 0 if ok else 1


def cmd_bot(args, cfg) -> int:
    from .telegram import Bot, resolve_token
    Bot(cfg, resolve_token(cfg)).run()
    return 0


def cmd_doctor(args, cfg) -> int:
    good = True

    def check(name: str, ok: bool, hint: str = "") -> None:
        nonlocal good
        good &= ok
        print(f"  [{'ok' if ok else 'MISSING'}] {name}" + ("" if ok else f"  -> {hint}"))

    check("ffmpeg", bool(shutil.which("ffmpeg")), "sudo apt install ffmpeg")
    import subprocess
    if shutil.which("ffmpeg"):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True).stdout.decode()
        check("ffmpeg libass (ass filter)", " ass " in filters, "ffmpeg built with --enable-libass")
    check("claude CLI", bool(shutil.which("claude")), "curl -fsSL https://claude.ai/install.sh | bash; then log in")
    check("Kokoro model", cfg.path(cfg.voice.model).is_file(), "./install.sh downloads it")
    check("Kokoro voices", cfg.path(cfg.voice.voices_file).is_file(), "./install.sh downloads them")
    check("Anton font", cfg.path("assets/fonts/Anton-Regular.ttf").is_file(), "./install.sh downloads it")
    for mod in ("kokoro_onnx", "cv2", "faster_whisper", "soundfile"):
        try:
            __import__(mod)
            check(f"python: {mod}", True)
        except Exception as e:  # noqa: BLE001
            check(f"python: {mod}", False, str(e))
    if not args.skip_llm and shutil.which("claude"):
        from .llm import ask_json
        try:
            r = ask_json("Reply with ok=true.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                 "required": ["ok"]}, "Answer in JSON.", cfg.llm, what="doctor")
            check("Claude login (headless)", bool(r.get("ok")), "run `claude` once and log in")
        except Exception as e:  # noqa: BLE001
            check("Claude login (headless)", False, str(e)[:200])
    print("All good." if good else "Something is missing (see above).")
    return 0 if good else 1


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="storygen", description="Story-time shorts made from scratch.")
    p.add_argument("-c", "--config", help="path to storygen.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="write, voice and render story clips")
    m.add_argument("-n", "--count", type=int, help="number of clips (default [count])")
    m.add_argument("--theme", help="what the story should be about (default: a random genre)")
    m.add_argument("--style", choices=["random", "layers", "escape"], help="background animation")
    m.add_argument("--story", help="story.json to reuse instead of asking Claude")
    sub.add_parser("bot", help="run the Telegram bot")
    d = sub.add_parser("doctor", help="check dependencies")
    d.add_argument("--skip-llm", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    sys.exit({"make": cmd_make, "bot": cmd_bot, "doctor": cmd_doctor}[args.cmd](args, cfg))
