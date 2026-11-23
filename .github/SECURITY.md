# Security

This repo is **public**, which is what makes GitHub Actions minutes free. That
is safe for the code, but it raises the bar on how credentials are handled.

## What is never in the repo

| Item | Where it lives |
|---|---|
| API keys, tokens, app passwords | GitHub **repository secrets** (`Settings -> Secrets and variables -> Actions`) |
| TikTok access/refresh tokens | `STATE_TOKENS` secret; hydrated at job start, never committed |
| `.env` | `.gitignore` (local only) |
| Video files, captions, transcripts | `work/` and `out/`, gitignored |

Only small JSON state and the generated dashboard are committed. State files
contain booleans and IDs, never credentials — and every run re-checks this.

## Automatic guard

`scripts/scan_secrets.py` runs in CI **after staging and before committing**:

- searches every staged file for the verbatim value of each secret this run can
  see (including leaves of the `STATE_TOKENS` JSON)
- also catches credential shapes: `ghp_`, `github_pat_`, `gsk_`, `sk-`,
  `AKIA...`, `AIza...`, private keys
- on a hit the job fails and **nothing is pushed**

Run it locally before a push:

```bash
python scripts/scan_secrets.py --all
```

## Do this once: a dedicated Nextcloud user

An app password inherits the **whole account's** permissions. Do not use your
personal account.

1. Nextcloud -> **Users -> New user**
   - username: `clip-factory`
   - add it to no groups, give it a normal quota
2. Log in as `clip-factory` -> create a folder **`Clips`**
3. **Settings -> Files -> Sharing** -> allow link sharing
4. **Settings -> Security -> Create app password** -> name it `clip-factory`
5. Copy that password into the `NC_APP_PASSWORD` secret; set
   `NC_USER=clip-factory`
6. Verify locally:

```bash
python scripts/check_storage.py
```

Worst case, a leak now exposes public clip files only — not your documents.

## Rotate a credential

If a secret is ever exposed (committed, pasted in an issue, shared in a
screenshot):

1. **Revoke at the source** — Groq dashboard, Meta app, TikTok app, Nextcloud
   app passwords, or GitHub PAT settings. Rotating in GitHub alone is not
   enough; the old value stays valid at the provider.
2. Create a new value and update the **repository secret**.
3. If it reached git history: treat it as public. Revocation is the fix; a
   history rewrite alone does not undo the leak.
4. Check **Actions -> latest run** to confirm the pipeline is healthy again.

TikTok tokens refresh automatically and are mirrored back into `STATE_TOKENS`
by the workflow using `SECRETS_ADMIN_PAT`.

## Hardening checklist

- [ ] **Push protection** enabled: `Settings -> Code security` (free for public
      repos) — blocks a commit containing a recognizable credential
- [ ] **2FA** on the GitHub account
- [ ] **No collaborators** unless trusted — anyone with write access can edit
      workflows and read secrets at run time
- [ ] `SECRETS_ADMIN_PAT` is a **fine-grained** PAT, scoped to **this repo
      only**, permission **Secrets: Read and write**, with an expiry date
- [ ] Never run `scripts/tiktok_oauth.py` in CI — it prints tokens by design and
      is a laptop-only, one-time helper
- [ ] Campaign source URLs: `config/campaigns.yaml` is public. Never commit a
      **signed** URL (one containing `?token=` or `?signature=`)

## Environment secrets

Not used. A job only sees environment secrets if it declares
`environment: <name>`, and blocking a scheduled run on manual approval would
stop the automation. All secrets are **repository** secrets.

## Reporting

Found a problem in this repo? Open a private security advisory
(`Security -> Advisories -> Report a vulnerability`) rather than a public issue.
