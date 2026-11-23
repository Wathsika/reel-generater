"""ASS subtitle generation: viral-style burned-in captions.

Timestamps come from the word-level transcript, so captions are perfectly
synced to the original audio. Clips are rendered relative to clip start,
so we offset absolute transcript times by the clip's start.
"""
from __future__ import annotations

import pathlib
from typing import Any

# ASS colours are &HAABBGGRR&
WHITE = "&H00FFFFFF"
YELLOW = "&H0000FFFF"   # active word / karaoke fill
BLACK = "&H00000000"
HOOK_COLOR = "&H0000D7FF"  # orange-ish hook
WATERMARK = "&H60FFFFFF"   # 60% transparent white


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


def _chunks(words: list[dict], clip_start: float, clip_end: float, max_words: int = 4) -> list[dict]:
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
            or w["start"] - cur[-1]["end"] > 0.6
            or w["start"] - cur[0]["start"] > 3.2
        ):
            out.append({"words": cur, "start": cur[0]["start"], "end": cur[-1]["end"]})
            cur = []
        cur.append(w)
    if cur:
        out.append({"words": cur, "start": cur[0]["start"], "end": cur[-1]["end"]})
    return out


def build(
    words: list[dict],
    clip_start: float,
    clip_end: float,
    out_path: pathlib.Path,
    style: str = "word",
    hook: str = "",
    watermark: str = "",
    font: str = "DejaVu Sans",
    font_size: int = 76,
) -> pathlib.Path:
    play_x, play_y = 1080, 1920
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_x}
PlayResY: {play_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{font_size},{YELLOW},{YELLOW},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,5,0,2,60,60,560,1
Style: CaptionChunk,{font},{font_size},{WHITE},{YELLOW},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,5,0,2,60,60,560,1
Style: Hook,{font},64,{HOOK_COLOR},{HOOK_COLOR},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,4,0,8,80,80,150,1
Style: Mark,{font},34,{WATERMARK},{WATERMARK},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,2,0,3,40,40,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    blocks = _chunks(words, clip_start, clip_end)
    if style == "chunk":
        for b in blocks:
            parts = []
            for w in b["words"]:
                cs = max(1, int(round((w["end"] - w["start"]) * 100)))
                parts.append(f"{{\\k{cs}}}{_clean(w['word'])}")
            text = " ".join(parts)
            events.append(
                f"Dialogue: 0,{_ts(b['start'])},{_ts(b['end'])},CaptionChunk,,0,0,0,,{text}"
            )
    else:  # word-by-word
        for b in blocks:
            for w in b["words"]:
                events.append(
                    f"Dialogue: 0,{_ts(w['start'])},{_ts(w['end'])},Caption,,0,0,0,,"
                    f"{_clean(w['word'])}"
                )

    if hook:
        events.append(
            f"Dialogue: 0,{_ts(0)},{_ts(min(3.0, clip_end - clip_start))},Hook,,0,0,0,,"
            f"{_clean(hook)}"
        )

    if watermark:
        events.append(
            f"Dialogue: 0,{_ts(0)},{_ts(clip_end - clip_start)},Mark,,0,0,0,,{_clean(watermark)}"
        )

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def from_clip(clip: dict, words: list[dict], out_dir: pathlib.Path, settings: dict[str, Any], clip_id: str) -> pathlib.Path:
    return build(
        words=words,
        clip_start=float(clip["start"]),
        clip_end=float(clip["end"]),
        out_path=out_dir / f"{clip_id}.ass",
        style=str(settings.get("caption_style", "word")),
        hook=str(clip.get("hook", "")),
        watermark=str(settings.get("watermark", "")),
        font=str(settings.get("caption_font", "DejaVu Sans")),
        font_size=int(settings.get("caption_font_size", 76)),
    )
