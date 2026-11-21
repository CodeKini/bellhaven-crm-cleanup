"""Download all Bellhaven CRM accounts to crm_accounts.json.

Run: python3 fetch_crm.py
The API token is requested privately at runtime and never saved in this file.
"""

import getpass
import json
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
ACCOUNTS_URL = f"{BASE_URL}/accounts"
OUTPUT_FILE = Path(__file__).resolve().parent / "crm_accounts.json"
PAGE_SIZE = 50
MAX_PAGES = 100


def extract_accounts_and_pagination(payload):
    """Accept common API response shapes without dropping account fields."""
    if isinstance(payload, list):
        return payload, {}, None
    if not isinstance(payload, dict):
        raise ValueError("Unexpected API response: expected a list or object.")

    accounts = None
    for key in ("accounts", "data", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            accounts = value
            break
        if isinstance(value, dict):
            for nested in ("accounts", "items", "results", "data"):
                if isinstance(value.get(nested), list):
                    accounts = value[nested]
                    break
            if accounts is not None:
                break

    if accounts is None:
        raise ValueError(
            "Could not locate the account list in the API response. "
            f"Top-level fields: {list(payload)}"
        )

    meta = payload.get("pagination") or payload.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    next_link = payload.get("next") or payload.get("next_page_url")
    if isinstance(payload.get("links"), dict):
        next_link = next_link or payload["links"].get("next")
    next_link = next_link or meta.get("next") or meta.get("next_page_url")
    return accounts, meta, next_link


def int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def main():
    token = getpass.getpass("Paste your Bellhaven API token (input hidden): ").strip()
    if not token:
        raise SystemExit("No token entered. Nothing downloaded.")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    del token

    all_accounts = []
    seen_pages = set()
    page = 1
    next_url = ACCOUNTS_URL
    params = {"page": page, "limit": PAGE_SIZE}

    for _ in range(MAX_PAGES):
        print(f"Fetching page {page} ...")
        response = session.get(next_url, params=params, timeout=30)
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise SystemExit(
                f"API request failed: HTTP {response.status_code}. "
                "Check your token and the endpoint/pagination options in the API docs."
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise SystemExit("API returned something other than JSON.") from error

        accounts, meta, next_link = extract_accounts_and_pagination(payload)
        if not accounts:
            print("No more accounts on this page.")
            break

        # If the server ignores the page parameter, avoid silently saving duplicates.
        signature = json.dumps(accounts, sort_keys=True, default=str)
        if signature in seen_pages:
            raise SystemExit(
                "The API returned an identical page twice. "
                "Pagination may use different parameter names. "
                "No output was written; check the Swagger docs."
            )
        seen_pages.add(signature)
        all_accounts.extend(accounts)
        print(f"  Received {len(accounts)} accounts; total so far: {len(all_accounts)}")

        total = int_or_none(meta.get("total") or meta.get("total_count"))
        if total is None and isinstance(payload, dict):
            total = int_or_none(payload.get("total") or payload.get("total_count"))
        total_pages = int_or_none(meta.get("total_pages") or meta.get("pages"))
        has_next = meta.get("has_next")
        if has_next is None and isinstance(payload, dict):
            has_next = payload.get("has_next")

        if total is not None and len(all_accounts) >= total:
            break
        if total_pages is not None and page >= total_pages:
            break
        if has_next is False:
            break

        page += 1
        if isinstance(next_link, str) and next_link.strip():
            next_url = urljoin(response.url, next_link)
            params = None
        elif isinstance(next_link, int) and not isinstance(next_link, bool):
            next_url = ACCOUNTS_URL
            params = {"page": next_link, "limit": PAGE_SIZE}
            page = next_link
        else:
            # Page-number pagination. Continue until an empty page or declared end.
            next_url = ACCOUNTS_URL
            params = {"page": page, "limit": PAGE_SIZE}
    else:
        raise SystemExit("Stopped after 100 pages to prevent an endless loop; no output written.")

    # Preserve each original API account object unchanged.
    OUTPUT_FILE.write_text(json.dumps(all_accounts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved {len(all_accounts)} accounts to {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(f"Unable to finish download: {exc}") from exc
