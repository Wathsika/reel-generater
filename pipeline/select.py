"""Stage 3: pick the viral moments with an LLM.

Reads word-level transcripts, asks the model for timestamped clip windows
with a hook, title, caption and hashtags. Validates and dedupes the result.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import config, jobs, state

SYSTEM_PROMPT = """You are a short-form video editor who has cut thousands of viral clips.
Given a timestamped transcript, choose the single best clip for TikTok / Reels / Shorts.

Rules:
- The clip must stand alone: no setup needed, punchline or payoff inside the window.
- Start on motion or a bold claim, end immediately after the payoff (loopable).
- Prefer emotional peaks: surprise, conflict, strong opinion, humor, money, transformation.
- Never pick a segment that is mid-sentence at the start or cut mid-word at the end.
- Do not include the words "in this video" or meta references.

Return ONLY JSON with this exact shape:
{"clips":[{"start":<seconds>,"end":<seconds>,"score":<1-10>,"hook":"<max 60 chars>",
"title":"<max 80 chars>","caption":"<1-2 sentences, first-person or question>",
"hashtags":["<tag>","<tag>","<tag>"],"reason":"<why this moment works>"}]}
"""


def _llm_json(transcript_text: str, want: int, max_dur: int, min_dur: int) -> dict:
    key = config.env("GROQ_API_KEY")
    model = config.env("GROQ_MODEL", "llama-3.3-70b-versatile")
    if key:
        import requests

        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"Transcript (seconds):\n{transcript_text}\n\n"
                        f"Return exactly {want} clips, each between {min_dur} and {max_dur} seconds."
                    )},
                ],
            },
            timeout=180,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    gemini = config.env("GEMINI_API_KEY")
    if gemini:
        import requests

        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.env('GEMINI_MODEL', 'gemini-2.0-flash')}:generateContent",
            params={"key": gemini},
            json={
                "contents": [{"parts": [{"text": SYSTEM_PROMPT + "\n\n" + transcript_text}]}],
                "generationConfig": {"response_mime_type": "application/json", "temperature": 0.7},
            },
            timeout=180,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])

    raise RuntimeError("No LLM key set - provide GROQ_API_KEY or GEMINI_API_KEY")


def _fmt_transcript(words: list[dict], chunk_chars: int = 3000) -> str:
    """Compact the word list into readable lines with start timestamps."""
    lines: list[str] = []
    cur: list[str] = []
    last_bucket = -1
    for w in words:
        bucket = int(w["start"] // 15)
        if bucket != last_bucket:
            if cur:
                lines.append(f"[{last_bucket * 15}] " + " ".join(cur))
                cur = []
            last_bucket = bucket
        cur.append(w["word"])
    if cur:
        lines.append(f"[{last_bucket * 15}] " + " ".join(cur))
    text = "\n".join(lines)
    if len(text) > chunk_chars:
        # keep head and tail - openings and payoffs matter most
        half = chunk_chars // 2
        text = text[:half] + "\n...\n" + text[-half:]
    return text


def _snap(words: list[dict], t: float, mode: str) -> float:
    """Snap a timestamp to a word boundary so we never cut mid-word."""
    if not words:
        return t
    best, best_d = t, 1e9
    for w in words:
        target = w["start"] if mode == "start" else w["end"]
        d = abs(target - t)
        if d < best_d:
            best, best_d = target, d
    return round(best, 2)


def run() -> int:
    job = jobs.start("select")
    queue = state.load_queue()
    made = 0

    for item in queue.get("clips", []):
        if item.get("stage") != "transcribed":
            continue
        settings = item.get("settings", {})
        want = int(settings.get("clips_per_video", 3))
        min_dur = int(settings.get("min_duration", 18))
        max_dur = int(settings.get("max_duration", 60))

        tpath = pathlib.Path(item["transcript"])
        data = json.loads(tpath.read_text(encoding="utf-8"))
        words = data.get("words", [])
        if len(words) < 30:
            item["stage"] = "failed"
            item["error"] = "transcript too short"
            continue

        try:
            raw = _llm_json(_fmt_transcript(words), want, max_dur, min_dur)
        except Exception as exc:
            item["stage"] = "failed"
            item["error"] = f"llm: {str(exc)[:300]}"
            state.error("select", f"llm failed for {item.get('source_url')}", reason=str(exc)[:300])
            continue

        duration = float(data.get("duration", 0))
        picked: list[dict] = []
        for c in raw.get("clips", []):
            try:
                start = _snap(words, float(c["start"]), "start")
                end = _snap(words, float(c["end"]), "end")
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            if end - start < min_dur:
                end = min(duration, start + max_dur)
            if end - start > max_dur:
                end = start + max_dur
            if end > duration:
                end = duration
            if end - start < 8:
                continue
            # reject overlapping picks
            if any(not (end <= p["start"] or start >= p["end"]) for p in picked):
                continue
            picked.append(
                {
                    "start": start,
                    "end": round(end, 2),
                    "score": int(c.get("score", 5)),
                    "hook": str(c.get("hook", ""))[:120],
                    "title": str(c.get("title", ""))[:160],
                    "caption": str(c.get("caption", ""))[:400],
                    "hashtags": [str(h) for h in c.get("hashtags", [])][:6],
                    "reason": str(c.get("reason", ""))[:300],
                }
            )

        if not picked:
            item["stage"] = "failed"
            item["error"] = "no valid clip windows selected"
            continue

        item["clips"] = picked
        item["stage"] = "selected"
        made += len(picked)
        state.save_queue(queue)

    jobs.finish(job, status="ok", items=made)
    return made


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Select viral moments").parse_args(argv)
    n = run()
    print(f"selected {n} clip candidate(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
