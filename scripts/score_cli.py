"""
score_cli.py — Score one item against the active taste profile.

This is the manual scoring CLI for step 3: it lets us iterate on the prompt
without depending on the scraper. Reads an item from a JSON file (see
samples/example_item.json for the shape), loads the active profile from
Supabase, calls Haiku, prints the parsed result.

Does NOT write to scoring_runs — this is just for prompt iteration. The
real scrape+score pipeline (step 5) will use the same scoring function and
persist results.

Usage:
    uv run scripts/score_cli.py samples/example_item.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from supabase import create_client


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Per the project spec: Haiku for per-item scoring.
MODEL = "claude-haiku-4-5-20251001"

# Fields we surface to the scorer. We deliberately exclude `price` — per
# profile.md ("keep taste scoring and price posture separate"), the price
# multiplier and surfacing threshold are applied in code, not the prompt.
ITEM_FIELDS_FOR_PROMPT = [
    "brand",
    "category",
    "subcategory",
    "title",
    "description",
    "size",
    "condition",
    "color",
    "material",
]

# Tool-use schema: forces Haiku to return a validated JSON object instead of
# free text we'd have to parse. Mirrors the "Scoring guidance for Claude"
# section of profile.md.
SCORE_TOOL = {
    "name": "submit_taste_score",
    "description": "Submit the taste score for this item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "taste_score": {
                "type": "number",
                "minimum": 0,
                "maximum": 10,
                "description": (
                    "0-10 score for fit against the profile, ignoring price. "
                    "Bias toward 'interesting over flattering' — lean higher on "
                    "conceptually strong but unconventional pieces."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "2-3 sentences referencing specific profile elements "
                    "(priority features, sub-aesthetics, brand affinity, etc.). "
                    "Be honest about uncertainty."
                ),
            },
            "features_hit": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["1", "2", "3", "4", "4b", "5", "6"],
                },
                "description": (
                    "Which priority features apply. 1=surface intervention, "
                    "2=interesting waist, 3=mixed registers, 4=balloon/puff "
                    "sleeves, 4b=asymmetric one-shoulder draping, "
                    "5=interesting over flattering, 6=vintage/archival."
                ),
            },
            "sub_aesthetics": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Which sub-aesthetics from the profile fit, e.g. "
                    "'romantic prairie / 70s', 'workwear / utility', "
                    "'90s / Y2K archival designer'."
                ),
            },
            "flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Concerns about the item, e.g. 'wrong size', "
                    "'condition Fair', 'plain piece with no surface intervention', "
                    "'too flattering-coded'. Empty array if no concerns."
                ),
            },
        },
        "required": ["taste_score", "reasoning", "features_hit", "sub_aesthetics", "flags"],
    },
}

# ---- Price + surfacing logic (lives in code, not in the scoring prompt) ----
# Mirrors the "Price posture" section of profile.md. Buy-zone (<$100) gets
# full taste_score credit; aspirational tiers get progressively discounted.
def price_multiplier(price: float | None) -> float:
    if price is None:
        # Unknown price → conservative; surface if the item is strong enough.
        return 0.85
    if price < 150:
        return 1.0
    if price < 300:
        return 0.85
    return 0.7


# Default surfacing threshold (after price adjustment). Jewellery has a higher
# bar per profile.md ("Only surface if exceptional (score 9+)").
def surfacing_threshold(category: str | None) -> float:
    if category and category.strip().lower() == "jewellery":
        return 9.0
    return 6.0


# Condition multiplier: how much to discount the score based on TRR's
# condition grade. Fair signals significant wear and meaningfully lowers
# buy-intent even on items that match the profile. None falls between Good
# and Very Good — conservative when condition wasn't captured.
def condition_multiplier(condition: str | None) -> float:
    if condition is None:
        return 0.92
    normalized = condition.strip().lower()
    return {
        # TRR labels
        "pristine": 1.0,
        "excellent": 1.0,
        "very good": 0.95,
        "good": 0.9,
        "fair": 0.8,
        # Depop labels (and freeform variants)
        "brand new": 1.0,
        "like new": 0.97,
        "used – like new": 0.97,
        "used – good": 0.9,
        "used – fair": 0.8,
    }.get(normalized, 0.92)


SYSTEM_INSTRUCTIONS = (
    "You score fashion items against a personal taste profile. The full profile "
    "is provided below — read it carefully before scoring. Always call the "
    "submit_taste_score tool with your verdict; do not respond in plain text. "
    "Do NOT factor price into the score — price is handled separately in code. "
    "When product images are provided, give them substantial weight — they "
    "reveal silhouette, hardware, surface detail, drape, and proportion that "
    "text descriptions often miss. "
    "IMPORTANT: taste_score is PURE PROFILE FIT, ignoring price, condition, "
    "and size. If condition is Fair, or the size is outside the user's range, "
    "or any other concern applies, put it in the flags array — do NOT deduct "
    "from taste_score. Downstream code uses flags to decide whether to surface "
    "the item; the score itself should reflect only how well the piece matches "
    "the design profile."
)


def load_active_profile(supabase) -> tuple[str, str]:
    """Fetch the currently active profile text and id from Supabase."""
    result = (
        supabase.table("profile_versions")
        .select("id, profile_text")
        .eq("is_active", True)
        .execute()
    )
    if not result.data:
        print("Error: no active profile in profile_versions. Run seed_profile.py first.", file=sys.stderr)
        sys.exit(1)
    row = result.data[0]
    return row["id"], row["profile_text"]


def format_item_for_prompt(item: dict) -> str:
    """Render the subset of item fields the scorer sees."""
    lines = []
    for field in ITEM_FIELDS_FOR_PROMPT:
        value = item.get(field)
        if value in (None, "", []):
            continue
        label = field.replace("_", " ").title()
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def score_item(client: Anthropic, profile_text: str, item: dict) -> dict:
    """Call Haiku with the profile + item and return the parsed score dict."""
    # Prompt-caching marker on the profile block: the profile is ~10KB and
    # identical across every item in a scoring run, so caching it cuts cost
    # and latency dramatically once we're scoring batches.
    system = [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"<taste_profile>\n{profile_text}\n</taste_profile>",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Build the user message: text first, then any product images. Capping at
    # 6 images keeps cost/latency bounded — TRR listings sometimes have 10+,
    # but the first few usually cover front/back/detail.
    user_blocks: list[dict] = [
        {"type": "text", "text": f"Score this item:\n\n{format_item_for_prompt(item)}"}
    ]
    for image_url in (item.get("image_urls") or [])[:6]:
        user_blocks.append(
            {"type": "image", "source": {"type": "url", "url": image_url}}
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        # temperature=0 makes scoring deterministic: same item + same profile
        # always returns the same verdict. Sub-aesthetics and flags drifted
        # between runs at the default temperature.
        temperature=0,
        system=system,
        tools=[SCORE_TOOL],
        tool_choice={"type": "tool", "name": "submit_taste_score"},
        messages=[{"role": "user", "content": user_blocks}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_taste_score":
            return block.input

    raise RuntimeError(
        f"Model did not call submit_taste_score. Raw response: {response.content!r}"
    )


def persist(supabase, item: dict, profile_id: str, result: dict) -> tuple[float, float, float, bool]:
    """Upsert the item and insert a scoring_runs row. Returns (price_mult, cond_mult, adjusted_score, surfaced)."""
    p_mult = price_multiplier(item.get("price"))
    c_mult = condition_multiplier(item.get("condition"))
    adjusted_score = float(result["taste_score"]) * p_mult * c_mult
    threshold = surfacing_threshold(item.get("category"))
    surfaced = adjusted_score >= threshold

    # Upsert items — re-scoring an existing item shouldn't fail; the raw_payload
    # is the full JSON we used, kept for forward compatibility.
    supabase.table("items").upsert(
        {
            "id": item["id"],
            "source": item.get("source", "trr"),
            "source_item_id": item["source_item_id"],
            "url": item.get("url"),
            "brand": item.get("brand"),
            "category": item.get("category"),
            "subcategory": item.get("subcategory"),
            "title": item.get("title"),
            "description": item.get("description"),
            "price": item.get("price"),
            "size": item.get("size"),
            "condition": item.get("condition"),
            "color": item.get("color"),
            "material": item.get("material"),
            "image_urls": item.get("image_urls") or [],
            "raw_payload": item,
        }
    ).execute()

    # Insert a scoring_runs row — we keep history (no upsert) so we can see how
    # scores evolve across profile versions.
    supabase.table("scoring_runs").insert(
        {
            "item_id": item["id"],
            "profile_version_id": profile_id,
            "taste_score": result["taste_score"],
            "price_adjusted_score": adjusted_score,
            "reasoning": result["reasoning"],
            "features_hit": result["features_hit"],
            "sub_aesthetics": result["sub_aesthetics"],
            "flags": result["flags"],
            "surfaced": surfaced,
        }
    ).execute()

    return p_mult, c_mult, adjusted_score, surfaced


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "item_json",
        help="Path to a JSON file describing one item (see samples/example_item.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score but don't write the item or scoring_runs row to Supabase.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    missing = [
        var
        for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY")
        if not os.environ.get(var)
    ]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    item_path = Path(args.item_json)
    if not item_path.exists():
        print(f"Error: {item_path} not found", file=sys.stderr)
        return 1
    item = json.loads(item_path.read_text())

    # Refuse to score garbage. If both title and brand are missing the scraper
    # likely failed (e.g. headless mode hit a Cloudflare check) and we'd just
    # get hallucinated nonsense from the model.
    if not item.get("title") and not item.get("brand"):
        print(
            f"Error: {item_path} has no title or brand. The scraper likely "
            "failed — refusing to score empty data. Re-run scrape with --headed.",
            file=sys.stderr,
        )
        return 1

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    profile_id, profile_text = load_active_profile(supabase)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    result = score_item(client, profile_text, item)

    print(f"Active profile: {profile_id}")
    print(f"Item: {item.get('brand', '?')} — {item.get('title', '?')}")
    print(f"Price: ${item.get('price', '?')}")
    print()
    print(json.dumps(result, indent=2))
    print()

    if args.dry_run:
        print("[dry-run] not writing to Supabase.")
    else:
        p_mult, c_mult, adjusted, surfaced = persist(supabase, item, profile_id, result)
        verdict = "✓ SURFACED" if surfaced else "✗ filtered"
        print(
            f"{verdict} — taste_score {result['taste_score']} "
            f"× price_mult {p_mult} × cond_mult {c_mult} "
            f"= adjusted {adjusted:.2f}"
        )
        print(f"Saved to items + scoring_runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
