"""Stage 5 - word-by-word captions and the title box, as an ASS file burned in by ffmpeg/libass.

Captions sit below the ring arena, 1-3 words at a time, the spoken word in yellow and popped.
The story title stays at the top in a white box (the "story time" look) for the whole clip.
"""
from __future__ import annotations

from .config import CaptionCfg


def ass_color(rgb_hex: str, alpha: int = 0) -> str:
    h = rgb_hex.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_inline(rgb_hex: str) -> str:
    h = rgb_hex.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def wrap(text: str, max_chars: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def group_words(words: list[dict], c: CaptionCfg) -> list[list[dict]]:
    """Short groups; a group never spans two sentences or a pause, and ends at , . ! ?"""
    groups, cur = [], []
    for w in words:
        n = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
        if cur and (len(cur) >= c.words_per_group or n > c.max_chars or w["sentence"] != cur[-1]["sentence"]
                    or w["t0"] - cur[-1]["t1"] > 0.35):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["w"].endswith((",", ".", "!", "?", ";", ":", "…")):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], title: str, duration: float, c: CaptionCfg,
              W: int = 1080, H: int = 1920) -> str:
    hi = ass_inline(c.highlight)
    base = ass_inline(c.color)
    y = int(H * c.position_y)
    out = [f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{c.font},{c.size},{ass_color(c.color)},&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,{c.outline},3,5,60,60,0,1
Style: Title,{c.font},{c.title_size},&H00111111,&H000000FF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,3,18,0,8,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""]
    if c.title_box and title:
        lines = wrap(esc(title), 26)[:3]
        out.append(f"Dialogue: 2,{ass_time(0)},{ass_time(duration)},Title,,0,0,0,,"
                   f"{{\\an8\\pos({W // 2},{int(H * 0.06)})\\fad(250,0)}}" + r"\N".join(lines))
    groups = group_words(words, c)
    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0]["t0"] if gi + 1 < len(groups) else g[-1]["t1"] + 0.6
        g_end = min(g_end, g[-1]["t1"] + 0.6)
        for k, w in enumerate(g):                    # one event per active word
            t0 = w["t0"] if k else g[0]["t0"]
            t1 = g[k + 1]["t0"] if k + 1 < len(g) else g_end
            if t1 - t0 < 0.02:
                continue
            parts = []
            for j, x in enumerate(g):
                txt = esc(x["w"].upper())
                if j == k:
                    parts.append(f"{{\\c{hi}\\fscx112\\fscy112}}{txt}{{\\c{base}\\fscx100\\fscy100}}")
                else:
                    parts.append(txt)
            pop = r"\fscx80\fscy80\t(0,90,\fscx100\fscy100)" if k == 0 else ""
            out.append(f"Dialogue: 1,{ass_time(t0)},{ass_time(t1)},Cap,,0,0,0,,"
                       f"{{\\an5\\pos({W // 2},{y}){pop}}}" + " ".join(parts))
    return "\n".join(out) + "\n"
