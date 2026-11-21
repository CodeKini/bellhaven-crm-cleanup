#!/usr/bin/env python3
"""Reset only the Grove City and Kettering demo decisions.

Run from any directory: python3 reset_demo.py
Only decisions.json and decisions_backup.json are touched.
"""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECISIONS = HERE / "decisions.json"
BACKUP = HERE / "decisions_backup.json"
TARGETS = ("grove city", "kettering")


def main():
    if not DECISIONS.is_file():
        raise SystemExit("decisions.json was not found next to reset_demo.py. Nothing changed.")
    if BACKUP.exists():
        raise SystemExit(
            "decisions_backup.json already exists. Nothing changed. "
            "Move or rename that backup first so it won't be overwritten."
        )

    # Always make the backup before loading or changing decisions.json.
    shutil.copy2(DECISIONS, BACKUP)
    with DECISIONS.open("r", encoding="utf-8") as file:
        decisions = json.load(file)
    if not isinstance(decisions, dict):
        raise SystemExit("Expected decisions.json to contain a JSON object. Original and backup are safe.")

    # Search the full text of each decision, including its saved key.
    to_remove = [
        key for key, value in decisions.items()
        if any(term in json.dumps({key: value}, ensure_ascii=False, default=str).casefold()
               for term in TARGETS)
    ]

    # This demo expects exactly the two named decisions. Avoid an unintended reset.
    if len(to_remove) != 2:
        raise SystemExit(
            f"Found {len(to_remove)} matching decisions, not the expected 2. "
            "No decisions were removed; the backup was saved."
        )

    for key in to_remove:
        del decisions[key]

    with DECISIONS.open("w", encoding="utf-8") as file:
        json.dump(decisions, file, indent=2, ensure_ascii=False)
        file.write("\n")

    print(f"Decisions removed: {len(to_remove)}")
    print(f"Decisions remaining: {len(decisions)}")
    print("Backup saved: decisions_backup.json")
    print("Restart review_app.py to see those proposals pending again.")


if __name__ == "__main__":
    main()
