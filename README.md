# Bellhaven CRM Cleanup

A small system that compares Bellhaven Senior Living's public website directory with a messy CRM account list and prepares safe, reviewable corrections. Nothing changes in the CRM until a person reviews the evidence and approves it.

## How it works

1. **Scraper** (`scraper.py`): collects all 34 communities from the website directory into `bellhaven_locations.csv`. It also surfaced a data gap: a 35th community featured on the homepage, Findlay, is missing from the directory.
2. **CRM download** (`fetch_crm.py`): pulls all 121 CRM accounts through the API, verifying pagination across all pages, into `crm_accounts.json`. The API token is entered privately at runtime and never stored.
3. **Matcher** (`match.py`): compares normalized names with city and ZIP and writes `proposals.json`: 38 proposals (16 confident matches, 14 needing a fix, 4 creates, 1 duplicate, 3 no longer listed), each with its evidence.
4. **Review app** (`review_app.py`): a local web app that shows one proposal at a time with its evidence. Approve or reject; decisions save to `decisions.json` and re-runs skip decided items. Full review result: 37 approved, 1 rejected, and the CRM grew from 121 to 127 records.
5. **Daily sync** (`.github/workflows/daily-bellhaven-sync.yml`): runs the scraper, CRM fetch, and matcher daily and saves proposals for human review. No auto-approval. See `SCHEDULE.md`.

## Safety rules

- **Billed-account protection (CHOW rule):** an account with lifetime revenue and open accounts receivable is never moved to a new parent. The system creates a new account under the correct parent and links the old one with `chow_current_account`. This protected the Tiffin and Marietta accounts automatically.
- Duplicates are marked Inactive and linked with `duplicate_of_account`, because the API has no delete.
- Ambiguous cases are flagged for a human, never guessed. Kettering had three CRM accounts at one address under different owners, so its proposal was rejected and documented instead of picking one blindly.

## Demo video

Link coming soon.

## Tech

Python (requests, BeautifulSoup), GitHub Actions. Built with AI tools, with every output checked against the source data, page counts, account totals, and safety rules before being accepted.
