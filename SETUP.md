# Setup & Run

Clean, step-by-step. Total time: ~45 minutes plus waiting on app approvals.

---

## Part 1 — Create the free accounts

| # | Service | Link | What you get |
|---|---|---|---|
| 1 | **Groq** | console.groq.com | free Whisper + Llama keys |
| 2 | **Cloudflare** | dash.cloudflare.com | R2 storage (10 GB free) |
| 3 | **Whop / Content Rewards** | contentrewards.com | clipping campaigns that pay you |
| 4 | **Meta developer** | developers.facebook.com | Instagram + Facebook publishing |
| 5 | **TikTok developer** | developers.tiktok.com | TikTok publishing |

Start the **TikTok app audit** and **Meta app review** today — they take days
to weeks. Everything else works while you wait.

---

## Part 2 — Push the repo to GitHub

The repo must be **public** (that's what makes Actions minutes free).

```bash
git remote add origin https://github.com/<YOU>/clip-factory.git
git branch -M main
git push -u origin main
```

---

## Part 3 — Cloudflare R2 (clip storage)

1. **R2 → Create bucket** → name it `clips`
2. **R2 → Manage R2 API Tokens → Create token**
   - Permission: **Object Read & Write**
   - Copy: `Access Key ID`, `Secret Access Key`, and your **Account ID** (top of the R2 page)
3. **Bucket → Settings → Public access** → enable the **r2.dev** URL
   - Copy that URL — it looks like `https://pub-abc123.r2.dev`

You now have: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET=clips`, `R2_PUBLIC_BASE`.

---

## Part 4 — Instagram (easiest platform, start here)

1. Instagram app → **Settings → Account type** → switch to **Creator** (or Business)
2. Link it to a **Facebook Page** (create a blank one if needed)
3. developers.facebook.com → **My Apps → Create App → Business**
4. Add product **Instagram Graph API**
5. **Graph API Explorer**:
   - select your app
   - add permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`
   - Generate token → exchange for a **long-lived** token
6. Get your IG user id: `GET /me/accounts` → page id → `GET /{page-id}?fields=instagram_business_account`

You now have: `IG_USER_ID`, `IG_ACCESS_TOKEN`, `IG_APP_ID`, `IG_APP_SECRET`.

---

## Part 5 — Facebook Reels (optional, same app)

1. In the same Meta app, make sure the Page is connected
2. Graph API Explorer → **Get Page Access Token** for your Page
   (must be a **Page** token, not your user token)
3. You now have `FB_PAGE_ID`, `FB_PAGE_TOKEN`

---

## Part 6 — TikTok (do this early, it's slow)

1. developers.tiktok.com → **Manage apps → Connect an app**
2. Add scopes: `user.info.basic`, `video.publish`, `video.upload`
3. Add redirect URI: `https://localhost/callback`
4. Copy `Client key` and `Client secret`
5. Run the one-time helper **on your laptop**:

```bash
pip install -r requirements.txt
set TT_CLIENT_KEY=your_key
set TT_CLIENT_SECRET=your_secret
python scripts/tiktok_oauth.py
```

It prints a JSON blob. Save that as the `STATE_TOKENS` secret (next step).

> TikTok's **Direct Post** needs an app audit. Until it's approved, keep
> `draft_mode: true` in `config/accounts.yaml` — clips post as private drafts.

---

## Part 7 — Add all secrets to GitHub

Repo → **Settings → Secrets and variables → Actions → New repository secret**

### Minimum to start (Instagram only)

| Secret | From |
|---|---|
| `GROQ_API_KEY` | Part 1 |
| `R2_ACCOUNT_ID` | Part 3 |
| `R2_ACCESS_KEY_ID` | Part 3 |
| `R2_SECRET_ACCESS_KEY` | Part 3 |
| `R2_BUCKET` | `clips` |
| `R2_PUBLIC_BASE` | Part 3 (the r2.dev URL) |
| `IG_USER_ID` | Part 4 |
| `IG_ACCESS_TOKEN` | Part 4 |

### Add when ready

| Secret | For |
|---|---|
| `IG_APP_ID`, `IG_APP_SECRET` | token auto-renewal |
| `FB_PAGE_ID`, `FB_PAGE_TOKEN` | Facebook Reels |
| `TT_CLIENT_KEY`, `TT_CLIENT_SECRET` | TikTok |
| `STATE_TOKENS` | TikTok tokens (JSON from Part 6) |
| `SECRETS_ADMIN_PAT` | lets CI save rotated tokens (see Part 8) |
| `GEMINI_API_KEY` | LLM fallback (optional) |

### Part 8 — `SECRETS_ADMIN_PAT`

Because the repo is public, rotated tokens can't be committed. Create a
**fine-grained PAT** → repo access → permission **Secrets: Read and write**.
Save it as `SECRETS_ADMIN_PAT`. Without it, tokens still work but you may need
to re-run Part 6 occasionally.

---

## Part 9 — Enable the dashboard (GitHub Pages)

Repo → **Settings → Pages** → **Source: GitHub Actions**.

Your dashboard will be at `https://<YOU>.github.io/<repo>/` and refreshes every
60 seconds.

---

## Part 10 — Add a source video

Open `config/campaigns.yaml` and enable a campaign you've joined:

```yaml
campaigns:
  - id: my-first-campaign
    enabled: true                     # ← change from false
    source_type: yt-dlp
    sources:
      - "https://the-url-the-rights-holder-gave-you"
    watermark: "@yourhandle"
    required_hashtags: ["#their-tag"]
    layout: crop                      # crop | split_v | gameplay_bg
```

> For a first test you can use **any public video you own the rights to**
> (your own unlisted YouTube link, or a CC0 clip).

---

## Part 11 — Run it

**Actions → clip-pipeline → Run workflow**

### First test (recommended)

| Input | Value |
|---|---|
| `limit` | `1` |
| `auto_approve` | **unchecked** |

This renders and uploads but does **not** post. Check the clips in R2 and the
dashboard, then re-run with `auto_approve` **checked** to publish.

### Normal runs

Nothing to do — it runs automatically at **05:00 and 17:00 UTC**.

---

## Part 12 — Check it worked

1. **Dashboard** → `https://<YOU>.github.io/<repo>/`
   - Pipeline shows every stage green
   - Accounts cards show OK
   - Published table fills in after posting
2. **Actions tab** → open the latest run → red steps show the exact error
3. **R2 bucket** → your clips should be there

---

## Daily routine (2 minutes)

- Glance at the dashboard: any red errors?
- Once a week: look at the **performance** table — which hooks got views?
  Put those patterns into the prompt in `pipeline/select.py`.

---

## Money

1. Join campaigns on **Content Rewards** (18+, free)
2. Connect the **same** accounts you post from
3. Post volume → get paid per 1,000 approved views (~$1 median, 10% fee,
   3-day hold, withdraw from $1)

Month 1 is typically **$0–300**. Volume comes from the pipeline; income comes
from picking good campaigns and good hooks.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no enabled campaigns` | set `enabled: true` in `config/campaigns.yaml` |
| `R2 credentials not set` | add the 3 R2 secrets |
| `Instagram not configured` | `IG_USER_ID` + `IG_ACCESS_TOKEN` |
| `TikTok not authorised` | run Part 6, save `STATE_TOKENS` |
| `ffmpeg failed` | check the Actions log; usually a bad source URL |
| TikTok posts fail | audit not approved → keep `draft_mode: true` |
| Nothing published | `auto_approve: true` in `config/accounts.yaml` |
