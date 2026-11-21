# Bellhaven daily schedule

The GitHub Actions workflow `daily-bellhaven-sync.yml` runs every day at **14:00 UTC** (7 AM Mountain Standard Time or 8 AM Mountain Daylight Time). It can also be started manually in GitHub Actions.

Every run does these steps, in order:

1. `python3 scraper.py` — visits the Bellhaven website and refreshes `bellhaven_locations.csv`.
2. `python3 fetch_crm.py` — downloads CRM accounts to `crm_accounts.json`. GitHub passes the API token from a repository secret called `BELLHAVEN_TOKEN`; the token is **not stored in the workflow file**.
3. `python3 match.py` — compares the two files and writes proposed changes to `proposals.json`.

The updated `match.py` reads `decisions.json` and skips proposals already approved or rejected. **For this to work across GitHub Actions runs, you must make the latest `decisions.json` available in the repository before each run.** GitHub's runner is temporary and does not remember files created or updated on your Mac; if you only keep `decisions.json` locally, decided items could appear again in GitHub's proposals.

**Nothing is ever auto-approved.** The daily workflow only creates `proposals.json` and uploads it as a downloadable GitHub Actions artifact. New proposals must wait until a person reviews and approves or rejects them in the local review app (`review_app.py`). This scheduled workflow does **not** run the review app or send CRM write requests.

## Alternative: local Mac cron

The cron schedule is:

```cron
0 7 * * *
```

That means **7:00 AM every day in your Mac's local time zone**. To run the complete pipeline, a cron entry must include the commands and use the correct folder, for example:

```cron
0 7 * * * cd /Users/YOUR_USERNAME/Downloads && /usr/bin/env python3 scraper.py && /usr/bin/env python3 fetch_crm.py && /usr/bin/env python3 match.py
```

**Important:** `fetch_crm.py` currently prompts for a token. A cron job has no interactive terminal, so the example above **will not complete unattended as-is**. The GitHub Actions workflow solves this by passing the secret securely through standard input. On a Mac, you would also need a secure, noninteractive way to supply the token before using cron; do not put the token directly in your crontab.

## Setup reminders

- Put `daily-bellhaven-sync.yml` at `.github/workflows/daily-bellhaven-sync.yml` in your GitHub repository.
- Keep `scraper.py`, `fetch_crm.py`, and the updated `match.py` at the repository root, as written in the workflow.
- Add `BELLHAVEN_TOKEN` under GitHub repository **Settings > Secrets and variables > Actions**.
- Keep the latest `decisions.json` synchronized to the repository if you want daily runs to respect decisions made in the Mac review app. Do not store the API token in `decisions.json`.
- Review new results by downloading the `bellhaven-proposals` artifact from a completed workflow run. The app itself runs locally, not on GitHub Actions.
