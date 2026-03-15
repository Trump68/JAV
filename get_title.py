"""
Get title/code/cast, call dodnld.py, wait 10 sec, then save to download/{CODE}.txt.
Usage: python get_title.py [URL]
File: line 1 = title, line 2 = code (e.g. IPZ-590), line 3 = cast.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Reuse browser setup from main script
from dodnld import (
    DOWNLOAD_DIR,
    launch_browser,
    new_stealth_context,
    wait_for_cloudflare_pass,
)

PAGE_TIMEOUT_MS = 30_000
# JAV code pattern: 2–5 letters, hyphen, digits (e.g. IPZ-590, ABP-123)
CODE_PATTERN = re.compile(r"[A-Z]{2,5}-\d+", re.IGNORECASE)


def extract_code_from_title(title: str) -> str | None:
    """Extract code like IPZ-590 from title. Returns first match or None."""
    m = CODE_PATTERN.search(title)
    return m.group(0).upper() if m else None


def get_video_title(page_url: str) -> tuple[str | None, str | None, str]:
    """Load page with Playwright; return (title, code, cast)."""
    page_url = page_url.strip()
    if not page_url.startswith(("http://", "https://")):
        return None, None, ""
    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            context = new_stealth_context(browser)
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            wait_for_cloudflare_pass(page)
            page.wait_for_timeout(2000)
            title = page.evaluate(
                """() => {
                const og = document.querySelector('meta[property="og:title"]');
                if (og && og.content && og.content.trim()) return og.content.trim();
                const h1 = document.querySelector('h1');
                if (h1 && h1.innerText) return h1.innerText.trim();
                return document.title ? document.title.trim() : null;
            }"""
            )
            title = title if isinstance(title, str) and title else None
            code = extract_code_from_title(title) if title else None
            cast = page.evaluate(
                """() => {
                var text = (document.body && document.body.innerText) || document.documentElement.innerText || '';
                var m = text.match(/Cast\\s*:\\s*([^\\n]+)/i);
                if (m && m[1]) return m[1].trim();
                var el = Array.from(document.querySelectorAll('*')).find(function(e) { return (e.textContent || '').trim().startsWith('Cast:'); });
                if (!el) return '';
                var t = (el.textContent || '').trim();
                m = t.match(/^Cast:\\s*([^\\n]+)/m);
                if (m && m[1]) return m[1].trim();
                var next = el.nextElementSibling;
                if (next && next.textContent) return next.textContent.trim();
                return t.replace(/^Cast:\\s*/i, '').split(/[\\n,]/)[0].trim() || '';
            }"""
            )
            cast = (cast or "").strip() if isinstance(cast, str) else ""
            return title, code, cast
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Get title/code/cast, call dodnld.py, wait 10 sec, save to download/{CODE}.txt."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://supjav.com/411204.html",
        help="Page URL (default: https://supjav.com/411204.html)",
    )
    args = parser.parse_args()
    title, code, cast = get_video_title(args.url)
    if not title:
        print("Could not extract title.", file=sys.stderr)
        return 1
    code = code or "unknown"
    is_reducing_mosaic = "reducing mosaic" in (title or "").lower()
    output_name = f"{code}_UNCENSORED.m4v" if is_reducing_mosaic else f"{code}.m4v"
    script_dir = Path(__file__).resolve().parent
    dodnld_py = script_dir / "dodnld.py"
    subprocess.Popen(
        [sys.executable, str(dodnld_py), args.url, "--visual", "-o", output_name],
        cwd=str(script_dir),
    )
    print("dodnld.py started, waiting 10 sec...", file=sys.stderr)
    time.sleep(10)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DOWNLOAD_DIR / f"{code}.txt"
    out_file.write_text(f"{title}\n{code}\n{cast}\n", encoding="utf-8")
    print(title)
    print(f"Saved: {out_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
