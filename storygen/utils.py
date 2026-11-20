"""Shared helpers: logging, subprocess wrappers for ffmpeg/ffprobe, JSON I/O."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("storygen")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "huggingface_hub", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Kokoro's phonemizer warns "words count mismatch" on every sentence with punctuation; harmless
    logging.getLogger("phonemizer").setLevel(logging.ERROR)


class ToolError(RuntimeError):
    pass


def require(binary: str) -> str:
    p = shutil.which(binary)
    if not p:
        raise ToolError(f"'{binary}' not found on PATH. Install it first (see README).")
    return p


def run(cmd: list[str], *, input: bytes | None = None, check: bool = True,
        capture: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(map(str, cmd)))
    proc = subprocess.run(list(map(str, cmd)), input=input, capture_output=capture, timeout=timeout)
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace")[-3000:]
        raise ToolError(f"Command failed ({proc.returncode}): {' '.join(map(str, cmd[:6]))} ...\n{err}")
    return proc


def ffmpeg(args: list[str], **kw) -> subprocess.CompletedProcess:
    return run([require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], **kw)


def ffprobe(path: Path | str) -> dict:
    out = run([require("ffprobe"), "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]).stdout
    return json.loads(out)


def media_info(path: Path | str) -> dict:
    """Return duration, width, height, fps, has_audio."""
    info = ffprobe(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") not in ("0/0", "0/1"):
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else 30.0
    dur = float(info["format"].get("duration") or (v or {}).get("duration") or 0)
    return {
        "duration": dur,
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": fps,
        "has_video": v is not None,
        "has_audio": a is not None,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fmt_ts(t: float) -> str:
    m, s = divmod(max(t, 0), 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


def slugify(text: str, maxlen: int = 40) -> str:
    import re
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:maxlen].strip("-") or "clip"
