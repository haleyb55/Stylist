"""
seed_profile.py — Load profile.md into profile_versions and mark it active.

Idempotent: if the current active profile already matches profile.md, exits with
no changes. Otherwise deactivates the current active row and inserts a new
active version with the markdown text and a changelog note.

Usage:
    uv run scripts/seed_profile.py
    uv run scripts/seed_profile.py --notes "v4 — refined sleeve preferences"
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "profile.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notes",
        default="Bootstrap v3 — initial profile",
        help="Changelog note stored on profile_versions.notes",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print(
            "Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env",
            file=sys.stderr,
        )
        return 1

    profile_text = PROFILE_PATH.read_text()
    if not profile_text.strip():
        print(f"Error: {PROFILE_PATH} is empty", file=sys.stderr)
        return 1

    supabase = create_client(url, key)

    # Idempotency: if the active row already matches profile.md verbatim, no-op.
    # If it differs, deactivate it before inserting the new one — the partial
    # unique index on (is_active = true) would reject a second active row.
    existing = (
        supabase.table("profile_versions")
        .select("id, profile_text")
        .eq("is_active", True)
        .execute()
    )
    if existing.data:
        current = existing.data[0]
        if current["profile_text"] == profile_text:
            print(
                f"No change — active profile {current['id']} already matches profile.md"
            )
            return 0
        supabase.table("profile_versions").update({"is_active": False}).eq(
            "id", current["id"]
        ).execute()
        print(f"Deactivated previous active profile {current['id']}")

    inserted = (
        supabase.table("profile_versions")
        .insert(
            {
                "profile_text": profile_text,
                "notes": args.notes,
                "is_active": True,
            }
        )
        .execute()
    )
    new_id = inserted.data[0]["id"]
    print(f"Inserted new active profile {new_id} ({len(profile_text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
