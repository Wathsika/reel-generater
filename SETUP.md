# Setup & Run

Clean, step-by-step. Total time: ~45 minutes plus waiting on app approvals.

---

## Part 1 — Create the free accounts

| # | Service | Link | What you get |
|---|---|---|---|
| 1 | **Groq** | console.groq.com | free Whisper + Llama keys |
| 2 | **Storage** | your existing **Oracle VPS** (MinIO or Nextcloud) — or dash.cloudflare.com | public HTTPS URL for clips |
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
git push -u origin HEAD          # pushes whatever branch you're on
```

> Your branch may be `master` or `main` — either works. Scheduled workflows
> run from the repo's **default branch**, so check that under
> **Settings → General → Default branch** if runs don't start.

---

## Part 3 — Clip storage (pick ONE)

Meta and TikTok download each clip **themselves**, so the URL must be:

- public **HTTPS** with a valid certificate
- the **raw file** — no login, no HTML page, no redirect
- `Content-Type: video/mp4`

Three backends are supported. Set `STORAGE_BACKEND` (or leave it blank and let
the pipeline auto-detect from which variables are present).

---

### Option A — MinIO on your Oracle VPS (recommended)

MinIO speaks the S3 API, so it plugs in directly and is far more reliable than
a Nextcloud share link. It runs comfortably on the free-tier ARM instance.

```bash
# 1) install minio + mc on the VPS
curl -O https://dl.min.io/server/minio/release/linux-arm64/minio
curl -O https://dl.min.io/client/mc/release/linux-arm64/mc
sudo install -m755 minio /usr/local/bin/minio
sudo install -m755 mc    /usr/local/bin/mc

# 2) data dir + service user
sudo mkdir -p /srv/minio/data
sudo useradd -r minio-user 2>/dev/null || true
sudo chown -R minio-user:minio-user /srv/minio

# 3) systemd unit  /etc/systemd/system/minio.service
```

```ini
[Unit]
Description=MinIO
After=network-online.target

[Service]
User=minio-user
Environment="MINIO_ROOT_USER=clipadmin"
Environment="MINIO_ROOT_PASSWORD=CHANGE-THIS-LONG-PASSWORD"
ExecStart=/usr/local/bin/minio server /srv/minio/data --address 127.0.0.1:9000 --console-address 127.0.0.1:9001

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now minio
sudo systemctl status minio --no-pager

# 4) create the bucket and allow anonymous GET (public read)
mc alias set local http://127.0.0.1:9000 clipadmin CHANGE-THIS-LONG-PASSWORD
mc mb --ignore-existing local/clips
mc anonymous set download local/clips
```

5. **Publish it over HTTPS.** MinIO stays on `127.0.0.1:9000`; put [Caddy](https://caddyserver.com)
   in front so it gets a free certificate (point an `A` record for
   `clips.yourdomain.com` at the VPS, open ports 80 + 443 in the Oracle
   security list, then `/etc/caddy/Caddyfile`):

```
clips.yourdomain.com {
    reverse_proxy 127.0.0.1:9000
}
```

6. **Verify before adding secrets:**

```bash
mc cp some.mp4 local/clips/probe.mp4
curl -sI https://clips.yourdomain.com/clips/probe.mp4 | grep -Ei "HTTP/|content-type|content-length"
# expect: HTTP/2 200, content-type: video/mp4, no "location:" header
```

Secrets for this option:

```
STORAGE_BACKEND          = s3
S3_ENDPOINT              = https://clips.yourdomain.com
S3_ACCESS_KEY_ID         = clipadmin
S3_SECRET_ACCESS_KEY     = CHANGE-THIS-LONG-PASSWORD
S3_BUCKET                = clips
S3_REGION                = us-east-1
S3_PATH_STYLE            = true
S3_PUBLIC_BASE           = https://clips.yourdomain.com/clips
```

> Oracle free-tier egress is 10 TB/month. Uploads from GitHub are *ingress*
> (free); only the platform fetching a clip counts, and a 10 MB clip has to be
> viewed ~1 million times to matter.

---

### Option B — Your existing Nextcloud

How it works:

1. the clip is uploaded over **WebDAV** to `NC_FOLDER/<date>/<clip>.mp4`
2. one **public read-only link share of the whole folder** is created and then
   reused for every clip (not one share per video — that would hit Nextcloud's
   share limits)
3. the public URL given to Meta/TikTok is the folder share's **direct DAV**
   path: `https://cloud.example.com/public.php/dav/files/<TOKEN>/<date>/<clip>.mp4`

That last path returns the raw bytes with `HTTP 200` and `Content-Type:
video/mp4` — no HTML wrapper, no redirect. The normal `/s/TOKEN` and
`/s/TOKEN/download` links do **not** work for this, which is why the pipeline
never uses them.

#### One-time Nextcloud setup

1. **Settings → Files → Sharing** — allow users to share via link.
2. Create a folder for clips (default name `Clips`) in the account you'll use.
3. **Settings → Security → Create app password** — use *this*, not your login
   password (2FA breaks plain-password auth).
4. Make sure `Clips` is **not** already shared with a password or an expiry
   date. A password-protected or expiring share cannot be fetched by Meta. If
   it is, either remove the password/expiry or point `NC_FOLDER` at a fresh
   folder.

#### Secrets

```
STORAGE_BACKEND = nextcloud
NC_URL          = https://cloud.yourdomain.com
NC_USER         = your-nextcloud-user
NC_APP_PASSWORD = <the app password from step 3>
NC_FOLDER       = Clips
```

Optional — only if your server differs:

```
# NC_DIRECT_TEMPLATE = {url}/public.php/dav/files/{token}/{key}
#   placeholders: {url} {token} {share} {key} {name}
```

#### Verify before running the pipeline

Run this **on your laptop** (it reads `.env` or your environment). It uploads a
tiny probe clip, then fetches the URL the same way Meta does — no redirects
followed, no cookies — and tells you pass/fail:

```bash
pip install -r requirements.txt
python scripts/check_storage.py
```

Expected output ends with:

```
status        : 200
content_type  : video/mp4
location      :
PASS - this URL is directly fetchable and correctly typed.
probe deleted from storage
```

Add `--keep` to keep the probe so you can paste its URL into Meta's Sharing
Debugger. Exit code `0` means storage is good; anything else, fix it before
running the workflow.

#### Disk stays bounded

Nextcloud runs on your VPS, so the pipeline prunes remote clips too:

- every run, `cleanup` deletes stored clips **older than 7 days**
  (`--remote-days 7`; set `0` to disable)
- it only ever removes `.mp4` files under `NC_FOLDER`
- platforms have already downloaded the file at publish time, so this is safe

The dashboard's **Storage** card shows `Remote pruned` and the retentions.

#### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Nextcloud could not share /Clips (403)` | link sharing disabled, or the folder is already shared with a password/expiry — remove it or use a new folder |
| URL returns `302` | you are using a `/s/...` link; the pipeline should use `/public.php/dav/files/...` — check `NC_DIRECT_TEMPLATE` |
| URL returns `401`/`403` | share has a password, or app password is wrong |
| `content-type: application/octet-stream` | the object was not stored as `.mp4`; keep keys ending in `.mp4` |
| `PROPFIND` fails during cleanup | Nextcloud's DAV is behind a proxy that blocks `PROPFIND`; allow it, or set `--remote-days 0` and prune manually |

> If Nextcloud keeps fighting you, Option A on the same VPS is ~15 minutes and
> removes this whole class of failure. The backend is a single secret switch.

---

### Option C — Cloudflare R2

1. **R2 → Create bucket** → name it `clips`
2. **R2 → Manage R2 API Tokens → Create token** → *Object Read & Write*
3. **Bucket → Settings → Public access** → enable the **r2.dev** URL

```
STORAGE_BACKEND        = r2
R2_ACCOUNT_ID          = ...
R2_ACCESS_KEY_ID       = ...
R2_SECRET_ACCESS_KEY   = ...
R2_BUCKET              = clips
R2_PUBLIC_BASE         = https://pub-abc123.r2.dev
```

R2 has a free tier but requires a card on file — that is why Option A/B exist.

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
| **one storage set** | Part 3 — Option A (`S3_*`), B (`NC_*`) or C (`R2_*`) |
| `IG_USER_ID` | Part 4 |
| `IG_ACCESS_TOKEN` | Part 4 |

Storage example (Option B / Nextcloud):
`STORAGE_BACKEND=nextcloud`, `NC_URL`, `NC_USER`, `NC_APP_PASSWORD`, `NC_FOLDER`.

Run `python scripts/check_storage.py` once before the first workflow run — it
proves Meta/TikTok can fetch from your storage.

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
| `source_url` | a video URL (or leave empty to use `campaigns.yaml`) |
| `auto_approve` | **unchecked** |

This renders and uploads but does **not** post. Check the clips in your storage
bucket/folder and the dashboard, then re-run with `auto_approve` **checked** to publish.

### Normal runs

Paste one or more links into `source_url`, comma separated, and run. Each link
is handled on its own run: when one video finishes, the next run starts
automatically for the next link. Nothing overlaps.

Leave `source_url` empty and it works through whatever is under `sources:` in
`config/campaigns.yaml`. The schedule also fires at **05:00 and 17:00 UTC**.

---

## Part 12 — Check it worked

1. **Dashboard** → `https://<YOU>.github.io/<repo>/`
   - Pipeline shows every stage green
   - Accounts cards show OK
   - Published table fills in after posting
2. **Actions tab** → open the latest run → red steps show the exact error
3. **Storage bucket/folder** → your clips should be there

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
| `R2 credentials not set` / `S3 backend not configured` | add the storage secrets (Part 3) |
| `Instagram not configured` | `IG_USER_ID` + `IG_ACCESS_TOKEN` |
| `TikTok not authorised` | run Part 6, save `STATE_TOKENS` |
| `ffmpeg failed` | check the Actions log; usually a bad source URL |
| TikTok posts fail | audit not approved → keep `draft_mode: true` |
| Nothing published | `auto_approve: true` in `config/accounts.yaml` |
