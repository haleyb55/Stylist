"""
scrape_depop.py — Scrape one Depop product URL into our item JSON shape.

Modelled on scrape_one.py (TRR). Depop runs on Cloudflare, which is much
more tractable than TRR's Akamai — patchright + the same stealth shims
usually clear it without a CAPTCHA. First run uses --headed in case
Cloudflare throws a verification page; cookies are then cached for future
headless runs.

Usage:
    uv run scripts/scrape_depop.py <depop-url> [--out samples/foo.json] [--headed] [--debug]
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Separate storage state file from TRR — different domain, different cookies.
STORAGE_STATE_PATH = PROJECT_ROOT / ".scratch" / "depop_storage_state.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

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
    """Depop URLs look like /products/<seller>-<itemname>/ — use the slug as the source id."""
    slug = urlparse(url).path.strip("/").split("/")[-1]
    return f"depop_{slug}", slug


def extract_product_ld(json_ld_texts: list[str]) -> dict:
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
    pattern = rf"^{re.escape(label)}\s*:?\s*(.+?)$"
    m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else None


def extract_depop_condition(text: str) -> str | None:
    """Depop writes condition inline like 'Excellent condition' — no colon prefix.
    Matches any word(s) preceding 'condition'."""
    m = re.search(
        r"\b(Brand new|Like new|Used – like new|Used – good|Used – fair|"
        r"Excellent|Very good|Good|Fair)\s+condition\b",
        text,
        re.IGNORECASE,
    )
    return m.group(1).strip().title() if m else None


async def wait_for_product_page(page, timeout_seconds: int = 60) -> bool:
    """Poll for Product JSON-LD — that's our signal we're past any Cloudflare check."""
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
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            browser = await p.chromium.launch(
                channel="chrome", headless=not headed, args=launch_args
            )
        except Exception:
            browser = await p.chromium.launch(headless=not headed, args=launch_args)

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

        if storage_state is None:
            print(
                "\n>>> If Cloudflare shows a verification page, solve it now.\n"
                ">>> Otherwise just wait — script will continue once the product loads.\n",
                file=sys.stderr,
            )
        product_ready = await wait_for_product_page(page, timeout_seconds=60)
        if not product_ready:
            await browser.close()
            raise SystemExit(
                "ERROR: Product JSON-LD not detected within 60s. Depop may have "
                "served a Cloudflare check page (try --headed) or the URL is invalid. "
                "Not writing output to avoid producing garbage data."
            )

        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        json_ld_texts = await page.locator(
            'script[type="application/ld+json"]'
        ).all_inner_texts()
        product = extract_product_ld(json_ld_texts)

        visible_text = await page.locator("body").inner_text()
        html = await page.content()

        if debug:
            print(f"[debug] page title: {await page.title()}", file=sys.stderr)
            print(f"[debug] final url:  {page.url}", file=sys.stderr)
            print(f"[debug] json-ld scripts: {len(json_ld_texts)}", file=sys.stderr)
            print(f"[debug] body text length: {len(visible_text)}", file=sys.stderr)
            print("[debug] body text (first 600 chars):", file=sys.stderr)
            print(visible_text[:600], file=sys.stderr)
            debug_path = PROJECT_ROOT / ".scratch" / "last_depop_scrape.html"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(html)
            print(f"[debug] HTML written to {debug_path}", file=sys.stderr)

        if product_ready:
            STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(STORAGE_STATE_PATH))

        await browser.close()

    # ---- Normalise JSON-LD ----
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

    # ---- Images: JSON-LD first, then any Depop CDN URL on the page ----
    images: list[str] = []
    ld_image = product.get("image")
    if isinstance(ld_image, list):
        images.extend(img for img in ld_image if isinstance(img, str))
    elif isinstance(ld_image, str):
        images.append(ld_image)

    seen = set(images)
    # Depop image CDNs: media.depop.com, cdn.depop.com, photos.depop.com
    for match in re.finditer(
        r"https://(?:media|cdn|photos)\.depop\.com/[^\s\"'<>)]+", html
    ):
        cleaned = match.group(0).rstrip("&;,.)\"'")
        if cleaned not in seen:
            seen.add(cleaned)
            images.append(cleaned)

    item_id, source_item_id = derive_ids(url)
    return {
        "id": item_id,
        "source": "depop",
        "source_item_id": source_item_id,
        "url": url,
        "brand": brand,
        # We could parse breadcrumbs for these but Depop's category structure
        # is messy. Leave null for v1 — the model gets enough from title/desc.
        "category": None,
        "subcategory": None,
        "title": product.get("name"),
        "description": product.get("description"),
        "price": price,
        "size": extract_field(visible_text, "Size"),
        "condition": extract_depop_condition(visible_text),
        "color": extract_field(visible_text, "Color"),
        "material": extract_field(visible_text, "Material"),
        "image_urls": images[:10],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Depop product URL")
    parser.add_argument("--out", help="Write JSON to this file (else stdout)")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--debug", action="store_true")
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
