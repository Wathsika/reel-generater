"""ASS subtitle generation: viral-style burned-in captions.

Timestamps come from the word-level transcript, so captions are perfectly
synced to the original audio. Clips are rendered relative to clip start,
so we offset absolute transcript times by the clip's start.

Design rules that make captions look "edited" instead of default-ffmpeg:

  * a heavy condensed display font (Anton, bundled in assets/fonts) instead of
    DejaVu Sans, so text reads at a glance on a phone,
  * three words on screen, uppercase, white until spoken and then filled in
    the accent colour (karaoke sweep),
  * a thick dark outline plus a drop shadow, so captions survive a bright
    gameplay background,
  * the hook sits in a solid colour bar at the top, wrapped over two lines,
  * the handle is a small translucent mark, never in the way of TikTok's UI.

Everything is driven by `settings`, so a campaign can restyle its clips from
config/campaigns.yaml without touching code.
"""
from __future__ import annotations

import pathlib
from typing import Any

PLAY_X, PLAY_Y = 1080, 1920
FONT_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Sensible starting point for a "hype" clip. Campaigns override what they need.
DEFAULTS = {
    "font": "Anton",
    "font_size": 92,
    "accent": "#FACC15",        # words fill with this as they are spoken
    "text_color": "#FFFFFF",    # unspoken words
    "outline": "#0A0A0F",       # caption outline + shadow base
    "uppercase": True,
    "hook_font": "Archivo Black",
    "hook_size": 54,
    "hook_bg": "#7C3AED",       # solid bar behind the hook
    "hook_color": "#FFFFFF",
    "hook_seconds": 3.4,
    "hook_y": 300,
    "watermark_color": "#FFFFFF",
}

STYLE_CAPTION = "Cap"
STYLE_HOOK = "Hook"
STYLE_MARK = "Mark"


# --------------------------------------------------------------------------
# colours
# --------------------------------------------------------------------------
def _ass_colour(value: str, default: str = "#FFFFFF") -> str:
    """`#RRGGBB` (or `&HBBGGRR&`) -> ASS `&H00BBGGRR&` (00 = fully opaque)."""
    text = str(value or default).strip()
    if text.startswith("&H"):
        body = text[2:].rstrip("&").upper()
        if len(body) == 6:
            return f"&H00{body}&"
        if len(body) == 8:
            return f"&H{body}&"
        return f"&H00FFFFFF&"
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return _ass_colour(default, "#FFFFFF")
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return _ass_colour(default, "#FFFFFF")
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _opt(settings: dict, key: str, fallback: Any) -> Any:
    value = settings.get(key)
    return fallback if value in (None, "") else value


def palette(settings: dict | None = None) -> dict:
    s = settings or {}
    out = dict(DEFAULTS)
    out.update(
        {
            "font": str(_opt(s, "caption_font", DEFAULTS["font"])),
            "font_size": int(_opt(s, "caption_font_size", DEFAULTS["font_size"])),
            "accent": _ass_colour(str(_opt(s, "caption_accent", DEFAULTS["accent"])), DEFAULTS["accent"]),
            "text_color": _ass_colour(str(_opt(s, "caption_color", DEFAULTS["text_color"])), DEFAULTS["text_color"]),
            "outline": _ass_colour(str(_opt(s, "caption_outline", DEFAULTS["outline"])), DEFAULTS["outline"]),
            "uppercase": bool(_opt(s, "caption_uppercase", DEFAULTS["uppercase"])),
            "hook_font": str(_opt(s, "hook_font", DEFAULTS["hook_font"])),
            "hook_size": int(_opt(s, "hook_size", DEFAULTS["hook_size"])),
            "hook_bg": _ass_colour(str(_opt(s, "hook_bg", DEFAULTS["hook_bg"])), DEFAULTS["hook_bg"]),
            "hook_color": _ass_colour(str(_opt(s, "hook_color", DEFAULTS["hook_color"])), DEFAULTS["hook_color"]),
            "hook_seconds": float(_opt(s, "hook_seconds", DEFAULTS["hook_seconds"])),
            "hook_y": int(_opt(s, "hook_y", DEFAULTS["hook_y"])),
            "watermark_color": _ass_colour(str(_opt(s, "watermark_color", DEFAULTS["watermark_color"])), DEFAULTS["watermark_color"]),
        }
    )
    return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return (
        str(text)
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def _wrap(text: str, width: int, max_lines: int = 3) -> list[str]:
    """Greedy wrap with a widow fix ("... GOES" / "CRAZY" looks broken)."""
    words = _clean(text).split()
    if not words:
        return []
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = f"{cur} {w}".strip()
        if cur and len(candidate) > width:
            lines.append(cur)
            cur = w
        else:
            cur = candidate
    lines.append(cur)
    # never leave a single word alone on the last line
    while len(lines) > 1 and len(lines[-1].split()) == 1:
        prev = lines[-2].split()
        if len(prev) < 2:
            break
        lines[-2] = " ".join(prev[:-1])
        lines[-1] = prev[-1] + " " + lines[-1]
    return lines[:max_lines]


def _hook_layout(text: str, size: int) -> tuple[list[str], int]:
    """Lines and font size for the hook, shrinking rather than cutting words."""
    while size > 36:
        lines = _wrap(text, max(16, int(PLAY_X / (size * 0.58))), max_lines=2)
        if len(lines) <= 2:
            return lines, size
        size -= 4
    return _wrap(text, max(16, int(PLAY_X / (size * 0.58))), max_lines=3), size


def _chunks(
    words: list[dict], clip_start: float, clip_end: float, max_words: int = 3,
    max_span: float = 2.8,
) -> list[dict]:
    """Group words into caption blocks, breaking on long pauses."""
    inside = [
        {**w, "start": w["start"] - clip_start, "end": w["end"] - clip_start}
        for w in words
        if w["start"] >= clip_start - 0.15 and w["end"] <= clip_end + 0.15
    ]
    out: list[dict] = []
    cur: list[dict] = []
    for w in inside:
        if cur and (
            len(cur) >= max_words
            or w["start"] - cur[-1]["end"] > 0.55
            or w["start"] - cur[0]["start"] > max_span
        ):
            out.append({"words": cur, "start": cur[0]["start"], "end": cur[-1]["end"]})
            cur = []
        cur.append(w)
    if cur:
        out.append({"words": cur, "start": cur[0]["start"], "end": cur[-1]["end"]})
    return out


def _case(text: str, upper: bool) -> str:
    return _clean(text).upper() if upper else _clean(text)


# --------------------------------------------------------------------------
# builder
# --------------------------------------------------------------------------
def build(
    words: list[dict],
    clip_start: float,
    clip_end: float,
    out_path: pathlib.Path,
    settings: dict | None = None,
    style: str = "chunk",
    hook: str = "",
    watermark: str = "",
) -> pathlib.Path:
    p = palette(settings)
    clip_len = max(0.5, float(clip_end) - float(clip_start))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Size the hook first: a long hook shrinks instead of being cut off, and
    # the style row has to know the final size.
    upper = bool(p["uppercase"])
    hook_lines: list[str] = []
    hook_size = int(p["hook_size"])
    if hook:
        hook_lines, hook_size = _hook_layout(_case(hook, upper), int(p["hook_size"]))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_X}
PlayResY: {PLAY_Y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {STYLE_CAPTION},{p['font']},{p['font_size']},{p['accent']},{p['text_color']},{p['outline']},&H78000000,0,0,0,0,100,100,1,0,1,7,4,2,70,70,430,1
Style: {STYLE_HOOK},{p['hook_font']},{hook_size},{p['hook_color']},{p['hook_color']},{p['hook_bg']},&H78000000,0,0,0,0,100,100,0,0,3,17,0,5,40,40,40,1
Style: {STYLE_MARK},{p['font']},32,{p['watermark_color']},{p['watermark_color']},{p['outline']},&H78000000,0,0,0,0,100,100,2,0,1,3,1,2,40,40,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    blocks = _chunks(words, clip_start, clip_end)
    if style == "word":
        # legacy look: one word at a time, no fill sweep
        for b in blocks:
            for w in b["words"]:
                text = _case(w["word"], upper)
                if not text:
                    continue
                pop = r"{\fscx112\fscy112\t(0,110,\fscx100\fscy100)}"
                events.append(
                    f"Dialogue: 0,{_ts(w['start'])},{_ts(w['end'])},{STYLE_CAPTION},,0,0,0,,{pop}{text}"
                )
    else:
        # karaoke sweep: white -> accent as each word is spoken
        for b in blocks:
            parts: list[str] = []
            for w in b["words"]:
                text = _case(w["word"], upper)
                if not text:
                    continue
                cs = max(1, int(round((w["end"] - w["start"]) * 100)))
                parts.append(rf"{{\kf{cs}}}{text}")
            if not parts:
                continue
            end = min(clip_len, b["words"][-1]["end"] + 0.12)
            pop = r"{\fscx105\fscy105\t(0,130,\fscx100\fscy100)}"
            events.append(
                f"Dialogue: 0,{_ts(b['start'])},{_ts(end)},{STYLE_CAPTION},,0,0,0,,{pop}{' '.join(parts)}"
            )

    if hook_lines:
        text = r"\N".join(hook_lines)
        y = max(120, min(int(p["hook_y"]), 600))
        show = min(float(p["hook_seconds"]), clip_len)
        anim = rf"{{\an5\pos({PLAY_X // 2},{y})\fad(150,260)\fscx88\fscy88\t(0,170,\fscx100\fscy100)}}"
        events.append(
            f"Dialogue: 0,{_ts(0)},{_ts(show)},{STYLE_HOOK},,0,0,0,,{anim}{text}"
        )

    if watermark:
        mark = _case(watermark, False)
        events.append(
            f"Dialogue: 0,{_ts(0)},{_ts(clip_len)},{STYLE_MARK},,0,0,0,,"
            rf"{{\an2\pos({PLAY_X // 2},1712)\alpha&H45&}}{mark}"
        )

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def from_clip(
    clip: dict,
    words: list[dict],
    out_dir: pathlib.Path,
    settings: dict[str, Any],
    clip_id: str,
) -> pathlib.Path:
    return build(
        words=words,
        clip_start=float(clip["start"]),
        clip_end=float(clip["end"]),
        out_path=out_dir / f"{clip_id}.ass",
        settings=settings,
        style=str(_opt(settings, "caption_style", "chunk")),
        hook=str(clip.get("hook", "")),
        watermark=str(settings.get("watermark") or ""),
    )


def fontsdir(from_dir: pathlib.Path) -> str:
    """`ass` filter option pointing libass at the bundled fonts.

    ffmpeg runs with cwd = the caption directory, so the path is relative to
    it. Forward slashes, and any colon escaped, or ffmpeg's filter parser would
    read the value as more options.
    """
    import os

    try:
        text = os.path.relpath(str(FONT_DIR), str(from_dir)).replace("\\", "/")
        if pathlib.Path(from_dir).joinpath(text).resolve() != FONT_DIR.resolve():
            raise ValueError("relative path does not resolve back to the fonts")
    except (ValueError, OSError):
        # different drive or an odd path: absolute, with the colon escaped
        text = str(FONT_DIR.resolve()).replace("\\", "/").replace(":", r"\:")
    return text
