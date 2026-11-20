"""Stage 6 - one ffmpeg pass: animation frames piped in, captions burned, voice + hit sounds mixed.

The hit sounds are ducked under the voice (sidechain), then the mix is normalised to -14 LUFS.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import ASSETS, Config
from .scenes import Sim, render_frames
from .utils import ffmpeg, log, require


def _filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render(cfg: Config, sim: Sim, voice_wav: Path, sfx_wav: Path, ass: Path, duration: float,
           out: Path, seed: int = 0, progress=None) -> Path:
    sc = cfg.scene
    W, H, fps = sc.width, sc.height, sc.fps
    fc = (
        f"[0:v]ass='{_filter_path(ass)}':fontsdir='{_filter_path(ASSETS / 'fonts')}',format=yuv420p[v];"
        f"[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0,apad,asplit=2[vo][key];"
        f"[2:a]aresample=48000,volume={sc.sfx_volume}[fx];"
        f"[fx][key]sidechaincompress=threshold=0.04:ratio=5:attack=15:release=350[duck];"
        f"[vo][duck]amix=inputs=2:normalize=0:duration=longest,"
        f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    )
    cmd = [require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-i", str(voice_wav), "-i", str(sfx_wav),
           "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", sc.preset, "-crf", str(sc.crf), "-r", str(fps),
           "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    log.debug("$ %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    total = len(sim.frames)
    try:
        for i, frame in enumerate(render_frames(sim, sc, seed=seed)):
            proc.stdin.write(frame.tobytes())
            if progress and i % (fps * 5) == 0:
                progress(f"Rendering {100 * i // max(total, 1)}%")
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg render failed: {err[-2000:]}")
    return out


def thumbnail(video: Path, out: Path, at: float = 1.5) -> Path:
    ffmpeg(["-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)])
    return out
