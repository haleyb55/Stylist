"""
scrape_one.py — Scrape one TRR product URL into our item JSON shape.

Loads the page with Playwright (a real Chromium browser), pulls structured data
from the JSON-LD that TRR publishes for SEO, falls back to the rendered text +
HTML for the fields JSON-LD doesn't cover (size, condition, color, material),
and collects every product image URL it can find.

Usage:
    # Print to stdout
    uv run scripts/scrape_one.py <trr-url>

    # Write to a JSON file (same shape as samples/*.json — feed straight to score_cli.py)
    uv run scripts/scrape_one.py <trr-url> --out samples/my_item.json

    # Watch the browser navigate (useful if scraping seems to fail)
    uv run scripts/scrape_one.py <trr-url> --headed
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# patchright is a drop-in Playwright fork with anti-detection patches built in.
# Same API; just import from patchright instead of playwright. Required for
# Akamai-protected sites like TRR — vanilla Playwright gets blocked even with
# manual stealth tweaks.
from patchright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Persists cookies + localStorage so subsequent runs skip Akamai's CAPTCHA.
# First headed run: user solves the press-and-hold once; we save state here.
# Following runs (including headless): load state, breeze past the challenge.
# Lives under .scratch (gitignored) — this is per-user session state.
STORAGE_STATE_PATH = PROJECT_ROOT / ".scratch" / "trr_storage_state.json"

# Close-to-real user agent. TRR runs Akamai bot protection; we pair this with
# launch flags + an init script that erase the most common Playwright/Chromium
# "I'm automation" tells before the page loads.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Patches the JS runtime before TRR's bot-detection scripts run.
# - navigator.webdriver: Playwright sets this to true; real browsers leave it undefined.
# - navigator.plugins/languages: vanilla Chromium has weird defaults that fingerprint as automation.
# - window.chrome: missing in headless Chromium; present in real Chrome.
# - permissions.query: a common detection probe expects browser-shape responses.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
if (window.navigator.permissions && window.navigator.permissions.query) {
  const originalQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({ state: 'granted' })
      : originalQuery(parameters)
  );
}
"""


def derive_ids(url: str) -> tuple[str, str]:
    """Pull the URL slug as the source-side id and build our composite id."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return f"trr_{slug}", slug


def extract_product_ld(json_ld_texts: list[str]) -> dict:
    """Find the Product entry across all JSON-LD blocks on the page."""
    for text in json_ld_texts:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if isinstance(entry, dict) and entry.get("@type") == "Product":
                return entry
    return {}


def extract_field(text: str, label: str) -> str | None:
    """Find a 'Label: value' line in visible page text."""
    pattern = rf"^{re.escape(label)}\s*:\s*(.+?)$"
    m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else None


async def wait_for_product_page(page, timeout_seconds: int = 90) -> bool:
    """Poll until the page has Product JSON-LD (meaning we're past any CAPTCHA).

    Returns True if found, False if timed out. CAPTCHA pages don't include
    Product schema; only the real product page does.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        try:
            texts = await page.locator(
                'script[type="application/ld+json"]'
            ).all_inner_texts()
            if any('"@type"' in t and "Product" in t for t in texts):
                return True
        except Exception:
            pass
        await asyncio.sleep(1.5)
    return False


async def scrape_item(url: str, headed: bool = False, debug: bool = False) -> dict:
    async with async_playwright() as p:
        # Prefer the real Chrome app installed on the system — its TLS and
        # browser fingerprint pass Akamai where bundled Chromium fails. Fall
        # back to bundled Chromium if Chrome isn't installed.
        launch_args = [
            "--disable-blink-features=AutomationControlled",
        ]
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=not headed,
                args=launch_args,
            )
        except Exception as e:
            print(
                f"[warning] Couldn't launch system Chrome ({e!s}); "
                "falling back to bundled Chromium.",
                file=sys.stderr,
            )
            browser = await p.chromium.launch(
                headless=not headed,
                args=launch_args,
            )
        # Load saved cookies if we have them — this is what lets subsequent
        # runs skip the CAPTCHA after a one-time manual solve.
        storage_state = (
            str(STORAGE_STATE_PATH) if STORAGE_STATE_PATH.exists() else None
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            storage_state=storage_state,
        )
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

        # If a CAPTCHA appears, wait up to 90 seconds for the user to solve it.
        # We're done once the Product JSON-LD shows up on the page.
        if storage_state is None:
            print(
                "\n>>> If a 'press and hold' or other CAPTCHA appears in the browser,\n"
                ">>> solve it now. The script will wait up to 90 seconds.\n",
                file=sys.stderr,
            )
        product_ready = await wait_for_product_page(page, timeout_seconds=90)
        if not product_ready:
            print(
                "[warning] Product JSON-LD not detected within 90s — either the "
                "CAPTCHA wasn't solved or TRR changed their page structure.",
                file=sys.stderr,
            )

        # networkidle is best-effort — TRR has long-polling assets that can
        # prevent it from ever firing. Continue if it times out.
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # Save cookies/localStorage so the next run skips the CAPTCHA.
        if product_ready:
            STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(STORAGE_STATE_PATH))

        json_ld_texts = await page.locator(
            'script[type="application/ld+json"]'
        ).all_inner_texts()
        product = extract_product_ld(json_ld_texts)

        visible_text = await page.locator("body").inner_text()
        html = await page.content()

        if debug:
            page_title = await page.title()
            page_url = page.url
            print(f"[debug] page title: {page_title}", file=sys.stderr)
            print(f"[debug] final url:  {page_url}", file=sys.stderr)
            print(f"[debug] json-ld scripts found: {len(json_ld_texts)}", file=sys.stderr)
            for i, text in enumerate(json_ld_texts):
                try:
                    data = json.loads(text)
                    if isinstance(data, list):
                        types = [d.get("@type") for d in data if isinstance(d, dict)]
                    elif isinstance(data, dict):
                        types = [data.get("@type")]
                    else:
                        types = ["(non-object)"]
                    print(f"[debug]   [{i}] @type values: {types}", file=sys.stderr)
                except json.JSONDecodeError as e:
                    print(f"[debug]   [{i}] parse error: {e}", file=sys.stderr)
            print(f"[debug] body text length: {len(visible_text)}", file=sys.stderr)
            print(f"[debug] body text (first 600 chars):", file=sys.stderr)
            print(visible_text[:600], file=sys.stderr)
            debug_path = PROJECT_ROOT / ".scratch" / "last_scrape.html"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(html)
            print(f"[debug] full HTML written to {debug_path}", file=sys.stderr)

        await browser.close()

    # ---- Normalise JSON-LD fields ----
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price")
    if isinstance(price, str):
        try:
            price = float(price)
        except ValueError:
            price = None

    brand = product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")

    # ---- Images: JSON-LD first, then any product-images.therealreal.com URL in HTML ----
    images: list[str] = []
    ld_image = product.get("image")
    if isinstance(ld_image, list):
        images.extend(img for img in ld_image if isinstance(img, str))
    elif isinstance(ld_image, str):
        images.append(ld_image)

    seen = set(images)
    for match in re.finditer(
        r"https://product-images\.therealreal\.com/[^\s\"'<>)]+", html
    ):
        # Trim trailing punctuation that sometimes sneaks in from the surrounding markup.
        cleaned = match.group(0).rstrip("&;,.)\"'")
        if cleaned not in seen:
            seen.add(cleaned)
            images.append(cleaned)

    item_id, source_item_id = derive_ids(url)
    return {
        "id": item_id,
        "source": "trr",
        "source_item_id": source_item_id,
        "url": url,
        "brand": brand,
        # We'll derive category/subcategory later from the URL path or breadcrumbs.
        # Leaving null for now keeps this scraper simple and stable.
        "category": None,
        "subcategory": None,
        "title": product.get("name"),
        "description": product.get("description"),
        "price": price,
        "size": extract_field(visible_text, "Size"),
        "condition": extract_field(visible_text, "Condition"),
        "color": extract_field(visible_text, "Color"),
        "material": extract_field(visible_text, "Material"),
        "image_urls": images[:10],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="TRR product URL")
    parser.add_argument("--out", help="Write JSON to this file path (else stdout)")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (useful for debugging bot detection issues)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print diagnostic info about what the scraper saw on the page",
    )
    args = parser.parse_args()

    item = await scrape_item(args.url, headed=args.headed, debug=args.debug)
    text = json.dumps(item, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n")
        print(f"Wrote {out_path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
