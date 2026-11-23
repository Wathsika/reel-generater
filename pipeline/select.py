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
Given a timestamped transcript, choose every moment that deserves its own
clip for TikTok / Reels / Shorts. Quality over quantity: return only moments
you would actually post, and never pad the list to reach a number.

Rules:
- Each clip must stand alone: no setup needed, punchline or payoff inside the window.
- Start on motion or a bold claim, end immediately after the payoff (loopable).
- Prefer emotional peaks: surprise, conflict, strong opinion, humor, money, transformation.
- Never pick a segment that is mid-sentence at the start or cut mid-word at the end.
- Do not include the words "in this video" or meta references.
- Clips must not overlap each other.
- If the transcript only supports two good moments, return two. Zero is
  acceptable only when nothing at all would work.

Return ONLY JSON with this exact shape:
{"clips":[{"start":<seconds>,"end":<seconds>,"score":<1-10>,"hook":"<max 60 chars>",
"title":"<max 80 chars>","caption":"<1-2 sentences, first-person or question>",
"hashtags":["<tag>","<tag>","<tag>"],"reason":"<why this moment works>"}]}
"""


GROQ_PREFERRED = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct",
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
]
GROQ_SKIP = ("whisper", "tts", "guard", "-vision")


def _groq_models(key: str) -> list[str]:
    """Model ids the key can actually use, preferred first.

    Groq retires models regularly (a retired id answers 404 and every run
    fails). Asking the API what exists means a deprecation can never take the
    pipeline down again.
    """
    import requests

    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        resp.raise_for_status()
        ids = [str(m.get("id")) for m in resp.json().get("data", []) if m.get("id")]
    except Exception:
        return []
    ids = [m for m in ids if not any(s in m for s in GROQ_SKIP)]
    order = {m: i for i, m in enumerate(GROQ_PREFERRED)}
    return sorted(ids, key=lambda m: (order.get(m, len(GROQ_PREFERRED)), m))


def _json_from(text: str) -> dict:
    """Parse JSON even if the model wrapped it in prose or a code fence."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def _llm_json(
    transcript_text: str, want: int, max_dur: int, min_dur: int,
    rules: str = "", keywords: list[str] | None = None,
    focus: list[int] | None = None,
) -> dict:
    extra = ""
    if rules:
        extra += f"\nCampaign brief (must be respected): {rules}\n"
    if keywords:
        extra += (
            "Prefer moments that mention any of these brand/product keywords: "
            + ", ".join(keywords) + ".\n"
        )
    if focus:
        stamps = ", ".join(f"{int(t)}s" for t in focus[:12])
        extra += (
            "\nAudience chat spiked at these timestamps: " + stamps + ".\n"
            "Chat spikes are the strongest available signal that something "
            "worth clipping happened. Prefer clips that begin at or shortly "
            "before a spike; only depart from them for a clearly better moment.\n"
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Transcript (seconds):\n{transcript_text}\n\n"
            f"Return between 1 and {want} clips, each between "
            f"{min_dur} and {max_dur} seconds. Use as many as the "
            f"transcript genuinely supports; do not pad."
            f"{extra}"
        )},
    ]

    key = config.env("GROQ_API_KEY")
    if key:
        import requests

        errors: list[str] = []
        candidates: list[str] = []
        explicit = config.env("GROQ_MODEL")
        if explicit:
            candidates.append(explicit)
        for m in _groq_models(key):
            if m not in candidates:
                candidates.append(m)
        if not candidates:
            candidates = list(GROQ_PREFERRED)

        # A 404 means the model was retired, a 429 means its quota is spent:
        # both are solved by moving to the next model in the list.
        for model in candidates[:4]:
            for json_mode in (True, False):
                body = {"model": model, "temperature": 0.7, "messages": messages}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                try:
                    resp = requests.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json=body,
                        timeout=180,
                    )
                except Exception as exc:
                    errors.append(f"{model}: {str(exc)[:120]}")
                    break

                if resp.status_code in (404, 429):
                    errors.append(f"{model}: HTTP {resp.status_code}")
                    break
                if resp.status_code == 400 and json_mode:
                    continue          # model may not support JSON mode
                if resp.status_code >= 400:
                    errors.append(f"{model}: HTTP {resp.status_code} {resp.text[:120]}")
                    break

                try:
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = _json_from(content)
                except Exception as exc:
                    errors.append(f"{model}: unusable answer ({str(exc)[:80]})")
                    continue

                state.info("select", f"groq model in use: {model}")
                return parsed

        raise RuntimeError("groq: " + ("; ".join(errors) or "no usable model"))

    gemini = config.env("GEMINI_API_KEY")
    if gemini:
        import requests

        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.env('GEMINI_MODEL', 'gemini-2.0-flash')}:generateContent",
            params={"key": gemini},
            json={
                "contents": [{"parts": [{"text": SYSTEM_PROMPT + "\n\n" + transcript_text + extra}]}],
                "generationConfig": {"response_mime_type": "application/json", "temperature": 0.7},
            },
            timeout=180,
        )
        resp.raise_for_status()
        return _json_from(resp.json()["candidates"][0]["content"]["parts"][0]["text"])

    raise RuntimeError("No LLM key set - provide GROQ_API_KEY or GEMINI_API_KEY")


def _fmt_transcript(
    words: list[dict], chunk_chars: int = 12000, focus: list[int] | None = None,
) -> str:
    """Compact the word list into readable lines with start timestamps.

    Long sources (multi-hour stream VODs) are sampled from the head, middle and
    tail instead of being cut to the first couple of minutes, so the model can
    still find moments anywhere in the recording.

    When `focus` timestamps are known (Twitch chat spikes), those windows are
    kept instead - the audience already told us where something happened.
    """
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
    if len(text) <= chunk_chars:
        return text

    if focus:
        blocks: list[str] = []
        for t in focus:
            lo, hi = int(t) - 90, int(t) + 150
            block = [
                ln for ln in lines
                if lo <= int(ln[1:ln.index("]")]) < hi
            ]
            if block:
                blocks.append("\n".join(block))
        joined = "\n...\n".join(blocks)
        if len(joined) > chunk_chars:
            cut = joined.rfind("\n", 0, chunk_chars)
            joined = joined[:cut if cut > 0 else chunk_chars]
        if joined.strip():
            return joined

    part = chunk_chars // 3
    mid = (len(text) - part) // 2
    text = (
        text[:part]
        + "\n...\n"
        + text[mid:mid + part]
        + "\n...\n"
        + text[-part:]
    )
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
        # The model decides how many clips the transcript can yield; this is
        # only a safety cap (campaigns rarely want more than a handful from
        # one window). clips_per_video is honoured for older configs.
        cap = settings.get("max_clips_per_video") or settings.get("clips_per_video") or 10
        want = max(1, min(int(cap), 20))
        min_dur = int(settings.get("min_duration", 18))
        max_dur = int(settings.get("max_duration", 60))

        tpath = pathlib.Path(item["transcript"])
        data = json.loads(tpath.read_text(encoding="utf-8"))
        words = data.get("words", [])
        if len(words) < 30:
            item["stage"] = "failed"
            item["error"] = "transcript too short"
            continue

        focus = [int(t) for t in (settings.get("focus_seconds") or []) if str(t).lstrip("-").isdigit()]
        try:
            raw = _llm_json(
                _fmt_transcript(words, focus=focus), want, max_dur, min_dur,
                rules=str(settings.get("rules") or ""),
                keywords=[str(k) for k in (settings.get("keywords") or [])],
                focus=focus,
            )
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
                    # render only picks up clips in this state - without it a
                    # selected clip is silently never rendered
                    "stage": "selected",
                }
            )

        if not picked:
            item["stage"] = "failed"
            item["error"] = "no valid clip windows selected"
            continue

        if len(picked) > want:
            # keep the highest-scoring ones, then restore timeline order
            picked = sorted(picked, key=lambda p: -p["score"])[:want]
            picked.sort(key=lambda p: p["start"])

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
