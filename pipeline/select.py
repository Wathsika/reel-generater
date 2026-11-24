"""Stage 3: pick the viral moments with an LLM.

Two things matter for accuracy, and both are handled here:

1. **Coverage.** A 75-minute stream does not fit in one prompt, so the
   transcript is split into time windows and the model is asked once per
   window. Previously only a 12k-character sample was sent, which is why a long
   stream produced one or two clips (and usually not the best ones).

2. **Trust.** A model reading timestamped text guesses where things happened.
   Every clip must now come with a verbatim `quote`; the quote is searched in
   the transcript and the clip is moved to where those words are actually
   spoken. Clips whose quote cannot be found are thrown away, so a clip always
   contains the moment it claims to.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from . import config, jobs, state

SYSTEM_PROMPT = """You are a short-form video editor who has cut thousands of viral clips
for TikTok, Reels and Shorts. Given a timestamped transcript, choose every
moment that deserves its own clip. Quality over quantity: return only moments
you would actually post, and never pad the list to reach a number.

What makes a clip work:
- A stranger scrolling past understands it in the first second.
- Emotion on screen: surprise, disbelief, conflict, a strong opinion, laughter,
  money, a win, a reaction, a hot take, a mistake, a reveal.
- It stands alone: no setup needed, the payoff is inside the window, and the
  last line makes people want to rewatch or comment.
- Start on the reaction or the claim, not on "so anyway". End right after the
  payoff. Never start mid-sentence or cut mid-word.
- Do not include filler, admin talk, "in this video", reading chat, or the
  streamer thinking out loud with nothing happening.

For EVERY clip you MUST return "quote": the exact words spoken inside that
window, copied word for word from the transcript (8-20 words, no paraphrasing,
no translation, no joining lines that are far apart). The quote is checked
against the transcript: clips whose quote cannot be found are discarded, so a
wrong quote destroys the clip.

"score" must be honest: 9-10 = I would bet this gets 100k views, 7-8 = clearly
good, 5-6 = average, 1-4 = do not return it at all.

Return ONLY JSON with this exact shape:
{"clips":[{"start":<seconds>,"end":<seconds>,"score":<1-10>,
"quote":"<verbatim words from the transcript>",
"hook":"<max 60 chars, the text shown on screen>",
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

# A quote must match at least this share of its words to be believed.
QUOTE_MATCH = 0.6
# Below this, a moment is not worth posting.
MIN_SCORE = 5
# How far the model's timestamp may be from where its quote really is.
QUOTE_DRIFT = 60
# Transcript characters per LLM call, and the most calls per video.
WINDOW_CHARS = 9000
MAX_WINDOWS = 6
WINDOW_SPAN = 900


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
    focus: list[int] | None = None, part: str = "",
    must_mention: bool = False,
) -> dict:
    extra = ""
    if part:
        extra += (
            f"\nThis is {part}. Only return moments that are inside this part; "
            "timestamps are absolute seconds of the whole recording.\n"
        )
    if rules:
        extra += f"\nCampaign brief (must be respected): {rules}\n"
    if keywords and must_mention:
        # The campaign pays per view, so a clip with no product mention earns
        # nothing however good the moment is. Say "must", not "prefer".
        extra += (
            "EVERY moment you return must contain one of these words spoken "
            "out loud, inside the clip: " + ", ".join(keywords) + ".\n"
            "If this part has no such moment, return an empty clips list.\n"
        )
    elif keywords:
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
            f"{min_dur} and {max_dur} seconds. Use as many as this part "
            f"genuinely supports; do not pad."
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
                body = {"model": model, "temperature": 0.6, "messages": messages}
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
                "generationConfig": {"response_mime_type": "application/json", "temperature": 0.6},
            },
            timeout=180,
        )
        resp.raise_for_status()
        return _json_from(resp.json()["candidates"][0]["content"]["parts"][0]["text"])

    raise RuntimeError("No LLM key set - provide GROQ_API_KEY or GEMINI_API_KEY")


# --------------------------------------------------------------------------
# transcript -> readable lines -> windows that fit one prompt
# --------------------------------------------------------------------------
def _lines(words: list[dict], bucket_seconds: int = 15) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    cur: list[str] = []
    last = -1
    for w in words:
        b = int(w["start"] // bucket_seconds)
        if b != last:
            if cur:
                lines.append((last * bucket_seconds, " ".join(cur)))
                cur = []
            last = b
        cur.append(str(w.get("word", "")).strip())
    if cur:
        lines.append((last * bucket_seconds, " ".join(cur)))
    return [(t, text) for t, text in lines if text.strip()]


def _fmt_transcript(words: list[dict], chunk_chars: int = 12000,
                    focus: list[int] | None = None) -> str:
    """One compact string of the whole transcript (kept for callers/tests)."""
    text = "\n".join(f"[{t}] {s}" for t, s in _lines(words))
    if len(text) <= chunk_chars:
        return text
    part = chunk_chars // 3
    mid = (len(text) - part) // 2
    return text[:part] + "\n...\n" + text[mid:mid + part] + "\n...\n" + text[-part:]


def _windows(
    words: list[dict], max_chars: int | None = None,
    max_windows: int | None = None, peaks: list[int] | None = None,
    span: int | None = None,
) -> list[dict]:
    """Split the transcript into prompt-sized time windows.

    Every window is self-contained (its lines carry absolute timestamps), so
    the model sees real text instead of a truncation. When there are more
    windows than calls we are willing to make, the ones with the most chat
    spikes win - that is where the audience actually reacted.
    """
    max_chars = max_chars or WINDOW_CHARS
    max_windows = max_windows or MAX_WINDOWS
    span = span or WINDOW_SPAN
    lines = _lines(words)
    if not lines:
        return []

    out: list[dict] = []
    cur: list[str] = []
    start = None
    size = 0
    for t, text in lines:
        line = f"[{t}] {text}"
        if cur and (size + len(line) > max_chars or t - start > span):
            out.append({"start": start, "end": t, "text": "\n".join(cur)})
            cur, size = [], 0
        if not cur:
            start = t
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append({"start": start, "end": words[-1]["end"], "text": "\n".join(cur)})

    if len(out) <= max_windows:
        return out

    if peaks:
        def heat(w: dict) -> int:
            return sum(1 for p in peaks if w["start"] - 60 <= p < w["end"])
        chosen = sorted(out, key=lambda w: -heat(w))[:max_windows]
    else:
        # no chat signal: spread the picks across the recording
        step = (len(out) - 1) / (max_windows - 1)
        chosen = [out[round(i * step)] for i in range(max_windows)]
    return sorted(chosen, key=lambda w: w["start"])


# --------------------------------------------------------------------------
# quote verification
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(str(text or "").lower())


def _find_quote(words: list[dict], quote: str,
                index: tuple[list[str], list[float]] | None = None) -> dict | None:
    """Locate the spoken words in the transcript.

    Returns {"ratio": 0..1, "start": seconds} for the best match. A ratio below
    QUOTE_MATCH means the model made the quote up (or copied another line).
    """
    q = _tokens(quote)
    if len(q) < 3:
        return None

    if index is None:
        index = _token_index(words)
    toks, times = index
    if len(toks) < len(q):
        return None

    anchors = set(q[:3])
    best_score, best_at = 0, -1
    for i in range(len(toks) - len(q) + 1):
        if toks[i] not in anchors:
            continue
        score = sum(1 for j, tok in enumerate(q) if toks[i + j] == tok)
        if score > best_score:
            best_score, best_at = score, i
            if score == len(q):
                break
    if best_at < 0:
        return None
    return {"ratio": round(best_score / len(q), 3), "start": times[best_at]}


def _token_index(words: list[dict]) -> tuple[list[str], list[float]]:
    """Flatten the transcript into tokens once (a 75 min VOD has ~13k words,
    and every clip would otherwise re-tokenise the whole thing)."""
    toks: list[str] = []
    times: list[float] = []
    for w in words:
        for tok in _tokens(w.get("word", "")):
            toks.append(tok)
            times.append(float(w["start"]))
    return toks, times

    anchors = set(q[:3])
    best_score, best_at = 0, -1
    for i in range(len(toks) - len(q) + 1):
        if toks[i] not in anchors:
            continue
        score = sum(1 for j, tok in enumerate(q) if toks[i + j] == tok)
        if score > best_score:
            best_score, best_at = score, i
            if score == len(q):
                break
    if best_at < 0:
        return None
    return {"ratio": round(best_score / len(q), 3), "start": times[best_at]}


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


def _mentions(words: list[dict], start: float, end: float,
              keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = " ".join(
        str(w.get("word", "")) for w in words
        if start - 1 <= float(w["start"]) <= end + 1
    ).lower()
    return any(k.lower() in text for k in keywords if k)


def _build_pick(
    raw: dict, words: list[dict], duration: float, min_dur: int, max_dur: int,
    index: tuple[list[str], list[float]] | None = None,
) -> dict | None:
    """Turn one model answer into a clip we can actually cut, or None."""
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except (KeyError, TypeError, ValueError):
        return None

    span = max(min_dur, min(end - start, max_dur))
    quote = str(raw.get("quote") or "").strip()
    hit = _find_quote(words, quote, index) if quote else None
    verified = False
    if hit and hit["ratio"] >= QUOTE_MATCH:
        if abs(hit["start"] - start) > QUOTE_DRIFT:
            state.info("select", "moved a clip to where its quote really is",
                       from_s=int(start), to_s=int(hit["start"]))
        start = hit["start"] - 0.25
        verified = True

    start = max(0.0, _snap(words, start, "start"))
    end = min(duration or (start + span), _snap(words, start + span, "end"))
    if end - start < min_dur:
        end = min(duration or (start + max_dur), start + max_dur)
    if end - start > max_dur:
        end = start + max_dur
    if end - start < 8:
        return None

    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "score": int(raw.get("score", 5)),
        "quote": quote[:300],
        "verify": "quote" if verified else "none",
        "hook": str(raw.get("hook", ""))[:120],
        "title": str(raw.get("title", ""))[:160],
        "caption": str(raw.get("caption", ""))[:400],
        "hashtags": [str(h) for h in (raw.get("hashtags") or [])][:6],
        "reason": str(raw.get("reason", ""))[:300],
        # render only picks up clips in this state - without it a selected clip
        # is silently never rendered
        "stage": "selected",
    }


def _keep(picks: list[dict], want: int) -> list[dict]:
    """Score, verify and overlap filter, then trim to `want`."""
    weakest = min((p["score"] for p in picks), default=0)
    # Only apply the score floor when there is something to choose between, so
    # a genuinely weak video still produces its best (few) moments.
    floor = MIN_SCORE if len(picks) > 3 and weakest < MIN_SCORE else 0

    kept: list[dict] = []
    for p in sorted(picks, key=lambda p: (-p["score"], p["verify"] != "quote")):
        if p["score"] < floor:
            continue
        if any(not (p["end"] <= k["start"] or p["start"] >= k["end"]) for k in kept):
            continue
        kept.append(p)

    trusted = [p for p in kept if p["verify"] == "quote"]
    if len(trusted) >= 3:
        # Enough verified moments: unverified ones are probably inventions.
        kept = trusted
    if len(kept) > want:
        kept = kept[:want]
    return sorted(kept, key=lambda p: p["start"])


def run() -> int:
    job = jobs.start("select")
    queue = state.load_queue()
    made = 0

    for item in queue.get("clips", []):
        if item.get("stage") != "transcribed":
            continue
        settings = item.get("settings", {})
        # The model decides how many clips the transcript can yield; this is
        # only a safety cap. clips_per_video is honoured for older configs.
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

        duration = float(data.get("duration", 0)) or (
            float(words[-1]["end"]) if words else 0.0
        )
        focus = [int(t) for t in (settings.get("focus_seconds") or [])
                 if str(t).lstrip("-").isdigit()]
        keywords = [str(k) for k in (settings.get("keywords") or [])]
        rules = str(settings.get("rules") or "")

        windows = _windows(words, peaks=focus)
        if not windows:
            item["stage"] = "failed"
            item["error"] = "no transcript windows"
            continue

        per_window = max(2, min(5, -(-want // len(windows)) + 1))
        want_mention = bool(settings.get("must_mention")) and bool(keywords)
        index = _token_index(words)
        picks: list[dict] = []
        errors: list[str] = []
        for n, win in enumerate(windows, 1):
            local_focus = [p for p in focus if win["start"] - 60 <= p < win["end"]]
            part = f"part {n} of {len(windows)} of the recording"
            try:
                raw = _llm_json(
                    win["text"], per_window, max_dur, min_dur,
                    rules=rules, keywords=keywords, focus=local_focus,
                    part=part, must_mention=want_mention,
                )
            except Exception as exc:
                errors.append(str(exc)[:200])
                state.warn("select", f"window {n}/{len(windows)} failed",
                           reason=str(exc)[:200])
                continue
            # A "must mention" part can legitimately come back empty. Ask again
            # without the restriction so the other 15 minutes are not lost -
            # the keyword filter below still throws out anything off-brief.
            if want_mention and not raw.get("clips"):
                try:
                    raw = _llm_json(
                        win["text"], per_window, max_dur, min_dur,
                        rules=rules, keywords=keywords, focus=local_focus,
                        part=part, must_mention=False,
                    )
                except Exception:
                    pass
            for c in raw.get("clips", []):
                pick = _build_pick(c, words, duration, min_dur, max_dur, index)
                if pick:
                    picks.append(pick)

        if not picks:
            item["stage"] = "failed"
            item["error"] = f"llm: {'; '.join(errors)[:300]}" if errors else "no usable moments"
            state.error("select", f"nothing selected for {item.get('source_url')}",
                        reason=item["error"][:300])
            continue

        # Campaigns that need the product mentioned: keep only windows that
        # actually say it, but never throw away everything.
        if settings.get("must_mention") and keywords:
            hits = [p for p in picks if _mentions(words, p["start"], p["end"], keywords)]
            if hits:
                dropped = len(picks) - len(hits)
                if dropped:
                    state.info("select", "dropped moments that never mention the product",
                               dropped=dropped, kept=len(hits))
                picks = hits
            else:
                state.warn("select", "no moment mentions the product - keeping the "
                           "best ones anyway", keywords=", ".join(keywords)[:120])

        picked = _keep(picks, want)
        if not picked:
            # The score floor removed everything. A video with nothing strong
            # still should not be wasted: keep its best moments and be honest
            # about it in the log.
            picked = sorted(picks, key=lambda p: -p["score"])[:min(3, want)]
            picked.sort(key=lambda p: p["start"])
            state.warn("select", "every moment scored below the quality floor - "
                       "keeping the best few anyway", clips=len(picked))
        unverified = sum(1 for p in picked if p["verify"] != "quote")
        if unverified:
            state.warn("select", "some clips could not be checked against the "
                       "transcript", clips=unverified, total=len(picked))

        item["clips"] = picked
        item["stage"] = "selected"
        item["windows"] = len(windows)
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
