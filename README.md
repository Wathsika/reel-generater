# Clip Factory

> **New here? → [SETUP.md](SETUP.md)** step-by-step setup and run guide.

Fully automated, **rights-cleared** clipping pipeline. Runs 100% on GitHub's free
hosted runners (public repo) — nothing downloads to your machine.

It ingests source videos you have permission to use (clipping campaigns supply
these), finds the viral moments with an LLM, renders 9:16 clips with burned-in
captions, and publishes to Instagram Reels, Facebook Reels and TikTok through
their **official APIs**. A web dashboard shows account status, ongoing work and
performance.

```
discover → transcribe → select → render → qa → upload → publish → analytics → dashboard
   │           │          │        │       │      │         │          │          │
   └───────────┴──────────┴────────┴───────┴──────┴─────────┴──────────┴──────────┘
                              all on GitHub Actions
```

## Why this shape

| Decision | Reason |
|---|---|
| **All cloud, no local runs** | Zero bandwidth cost to you; public repos get unlimited free Actions minutes (4 vCPU / 16 GB / 6 h per job) |
| **Campaign sources only** | The rights holder supplies the video. Reposting scraped content gets you strikes **and** pays nothing — Meta/TikTok demonetise "unoriginal" content |
| **Official APIs only** | Browser automation gets accounts banned. IG Graph, FB Graph, TikTok Content Posting API |
| **State in JSON, committed** | The dashboard reads the same files the pipeline writes. No database, no server |
| **Tokens in Actions secrets, never committed** | Repo is public. Rotated tokens sync to a `STATE_TOKENS` secret via `gh secret set` |

## Setup

### 1. Repo + secrets

Push this to a **public** GitHub repo, then add these under
*Settings → Secrets and variables → Actions*:

**LLM / transcription**
- `GROQ_API_KEY` — [console.groq.com](https://console.groq.com) (free; Whisper + Llama)
- `GEMINI_API_KEY` — optional fallback

**Storage (pick one backend)**
- **MinIO / any S3** (recommended for a VPS): `STORAGE_BACKEND=s3`,
  `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`,
  `S3_REGION`, `S3_PATH_STYLE`, `S3_PUBLIC_BASE`
- **Nextcloud**: `STORAGE_BACKEND=nextcloud`, `NC_URL`, `NC_USER`,
  `NC_APP_PASSWORD`, `NC_FOLDER` (optional `NC_DIRECT_TEMPLATE`)
- **Cloudflare R2**: `STORAGE_BACKEND=r2`, `R2_ACCOUNT_ID`,
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE`
- The URL must serve the raw `video/mp4` over HTTPS with no redirect; see
  [SETUP.md Part 3](SETUP.md) for the VPS walkthrough

**Instagram**
- `IG_USER_ID`, `IG_ACCESS_TOKEN`, `IG_APP_ID`, `IG_APP_SECRET`
- Account must be **Business or Creator** and linked to a Facebook Page

**Facebook Reels**
- `FB_PAGE_ID`, `FB_PAGE_TOKEN`
- `FB_PAGE_TOKEN` must be a **Page access token** (Graph API Explorer → your
  Page → *Get Page Access Token*), not your personal user token
- The Page must be linked to the same Meta app as Instagram

**TikTok**
- `TT_CLIENT_KEY`, `TT_CLIENT_SECRET`
- [developers.tiktok.com](https://developers.tiktok.com) → Content Posting API
- Direct post needs an **app audit** (days–weeks). Until it passes, leave
  `draft_mode: true` in `config/accounts.yaml`

**Token sync (required — public repo)**
- `SECRETS_ADMIN_PAT` — a PAT with **repo → Secrets: Read and write**. Lets a
  run push rotated tokens back into `STATE_TOKENS` so the next run works.

### 2. Enable GitHub Pages

*Settings → Pages → Source: **GitHub Actions***. The pipeline deploys
`docs/` automatically after each run.

### 3. Configure campaigns

Edit `config/campaigns.yaml`. Set `enabled: true` on campaigns you've joined
and paste **their** supplied source URLs:

```yaml
campaigns:
  - id: my-campaign
    enabled: true
    source_type: yt-dlp
    sources: ["https://...supplied-by-the-rights-holder..."]
    watermark: "@yourhandle"     # most campaigns require this
    required_hashtags: ["#their-tag"]
    layout: crop                 # crop | split_v | gameplay_bg
    gameplay_source: null        # set if they supply gameplay for split view
```

### 4. Configure accounts

`config/accounts.yaml` — enable platforms, set posting limits and
`auto_approve` (keep `false` until you trust the output).

### 5. Join a payout marketplace

**Content Rewards (Whop / contentrewards.com)** is the established one — it's
run by Whop, uses a real payment processor, and holds ~700 live campaigns.
Median rate ≈ **$1.09 per 1,000 approved views** (range $0.20–$6), 10% fee,
3-day hold, withdraw from $1 to bank/PayPal.

Requirements: 18+, free, and you connect the **same** social accounts you post
from (campaign attribution follows the connected account, not a linked one).

Beware anything that asks you to pay to start, promises fixed daily income, or
wants your password — those are the scams.

### 6. Trigger it

*Actions → clip-pipeline → Run workflow*. First run: `limit=1`,
`auto_approve=false`. Review the output, then open it up.

## Dashboard

Deployed automatically to GitHub Pages after every run. Shows:

- **KPIs** — clips today, rendered, published, views, in-progress, errors
- **Pipeline flow** — all 9 stages with live status and last-run time
- **Accounts & connections** — IG / FB / TikTok / storage / LLM health at a glance
- **Ongoing work** — what's running right now, elapsed time
- **Recent errors** — with stage and reason
- **Today's queue** — every source and its stage
- **Published & performance** — top clips by views + 14-day view sparkline
- **Activity log** — latest 18 events

The page auto-refreshes every 60 s by fetching state straight from
`raw.githubusercontent.com`, so it updates even between pipeline runs.
Locally: `python -m pipeline.dashboard` then open `docs/index.html`.

## The stages

| # | Module | What it does |
|---|---|---|
| 1 | `pipeline/discover.py` | Downloads enabled campaign sources via yt-dlp |
| 2 | `pipeline/transcribe.py` | Groq `whisper-large-v3`, word-level timestamps, 10-min chunks |
| 3 | `pipeline/select.py` | LLM picks clip windows + hook/title/caption/hashtags; snaps to word boundaries |
| 4 | `pipeline/render.py` | ffmpeg → 1080×1920, layout, loudness normalise, burned ASS captions |
| 5 | `pipeline/qa.py` | Duration/resolution/audio/profanity/duplicate checks |
| 6 | `pipeline/storage.py` | Uploads to S3/MinIO, Nextcloud or R2; records public URL |
| 7 | `pipeline/publish.py` | IG Reels → FB Reels → TikTok, each independent |
| 8 | `pipeline/analytics.py` | Pulls views/likes back, builds daily rollup |
| 9 | `pipeline/cleanup.py` | Deletes sources + uploaded clips, prunes old state |
| 10 | `pipeline/dashboard.py` | Renders `docs/index.html` |

## Publishing targets

All three are wired and independent — one failing never blocks the others.

| Platform | Endpoint | Notes |
|---|---|---|
| **Instagram Reels** | IG Graph `/media` → `/media_publish` | Needs Business/Creator account + `instagram_content_publish` |
| **Facebook Reels** | FB Graph `/video_reels` **3-phase** (`start` → upload → `finish`) | Needs a **Page** access token, not a user token |
| **TikTok** | Content Posting API | Direct Post needs app audit; `draft_mode: true` until then |

Facebook Reels is **enabled by default** in `config/accounts.yaml`. It uploads the
local file to Meta (the public URL is only a fallback), which is why cleanup runs
*after* publishing.

## Cleanup & retention

`pipeline/cleanup.py` runs as stage 9 on every pipeline run:

- **Long-form source deleted** once all its clips are cut — media, `.audio.mp3`
  and `.words.json` all go. Sources with *unrendered* clips are kept
  (`--force` overrides).
- **Clip `.mp4` deleted** as soon as it is uploaded to storage (publishing uses the
  URL, so the local copy is dead weight).
- **Remote clips pruned** after 7 days (`--remote-days`), which matters when
  storage is your own VPS. Only `.mp4` files under the configured folder/bucket
  are touched, and only after platforms have had time to download them.
- **Stale captions** (`work/captions/*.ass` whose clip is gone) removed.
- **Per-day state pruned** after `--prune-days` (default 30). Only `queue/`,
  `rendered/` and `logs/` are pruned; `posted.json`, `performance.json`,
  `jobs.json` and `accounts.json` are cumulative and never touched.
- **Safety net** in the workflow still does `rm -rf work out` at job end.

**Why your repo won't grow:** videos live in `work/` and `out/`, both
gitignored — they never enter git. Hosted runners also wipe their VM after each
job. The only committed growth is small JSON, which the retention rule bounds.
The dashboard's Storage card shows local MB used and MB freed last run.

## Money — realistic expectations

| Stream | When it starts | Typical |
|---|---|---|
| CPM campaign payouts | week 1–2 | $100–500/mo → $1–5K/mo at 2–3 accounts |
| Per-post campaigns | week 1 | $5–50 per approved clip (predictable floor) |
| Retainers | month 2–3 | $500–2K/mo per page |
| Affiliate / product links | month 3+ | upside |
| Page flip | month 6+ | 20–40× monthly revenue |

**Honest math:** $300/day needs ~200K views/day at $1.50 CPM. Month 1 is
usually **$0–300**. Volume (the pipeline) buys you attempts; hook quality and
niche selection buy you money. Check the dashboard's performance table weekly
and feed winning hook patterns back into `select.py`'s prompt.

**Running cost: $0.** Public-repo Actions are free, clip storage runs on your
own VPS (MinIO/Nextcloud) or R2's free tier, Groq and Gemini have free tiers,
platform APIs are free.

## Limits & risks

| Risk | Mitigation |
|---|---|
| 6 h job cap | `timeout-minutes: 330`; `limit` input caps daily sources |
| TikTok audit not approved yet | `draft_mode: true` posts privately until audited |
| Token expiry kills a run | `tokens.py` refreshes + mirrors to `STATE_TOKENS` |
| Same clip posted by other clippers | SHA-256 dedupe in `qa.py` |
| "Unoriginal content" flag | Captions + hook text + watermark + campaign rules |
| Actions ToS (must relate to the software project) | This repo *is* the open-source project |

## Not included (on purpose)

- Scraping creators you have no permission for — infringement, and platforms
  pay nothing for it anyway
- Content-ID evasion / layout "loopholes" — detection fingerprints audio and
  visuals, not layout; it gets accounts banned
- Browser automation for posting — official APIs only
