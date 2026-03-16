"""
Get title/code/cast, call dodnld.py, wait 10 sec, then save to download/{CODE}/{CODE}.txt
and cover image as download/{CODE}/{CODE}.jpg; video saved to download/{CODE}/ by dodnld.
Usage: python get_title.py [URL]
File: line 1 = title, line 2 = code (e.g. IPZ-590), line 3 = cast.
"""

import argparse
import re
import subprocess
import sys
import time
import urllib.request
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


def get_video_title(page_url: str) -> tuple[str | None, str | None, str, str | None]:
    """Load page with Playwright; return (title, code, cast, cover_image_url)."""
    page_url = page_url.strip()
    if not page_url.startswith(("http://", "https://")):
        return None, None, "", None
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
            cover_url = page.evaluate(
                """() => {
                const og = document.querySelector('meta[property="og:image"]');
                if (og && og.content && og.content.trim()) return og.content.trim();
                const img = document.querySelector('img[src*="img.supjav.com"], img[src*="supjav.com/images"]');
                if (img && img.src) return img.src;
                return null;
            }"""
            )
            cover_url = (cover_url or "").strip() if isinstance(cover_url, str) else None
            if cover_url and not cover_url.startswith(("http://", "https://")):
                cover_url = None
            return title, code, cast, cover_url
        finally:
            browser.close()


def save_cover_image(url: str, path: Path) -> bool:
    """Download cover image from url and save to path. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            path.write_bytes(resp.read())
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Get title/code/cast, call dodnld.py, wait 10 sec, save to download/{CODE}/."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://supjav.com/411204.html",
        help="Page URL (default: https://supjav.com/411204.html)",
    )
    args = parser.parse_args()
    title, code, cast, cover_url = get_video_title(args.url)
    if not title:
        print("Could not extract title.", file=sys.stderr)
        return 1
    code = code or "unknown"
    is_reducing_mosaic = "reducing mosaic" in (title or "").lower()
    output_name = f"{code}_UNCENSORED.m4v" if is_reducing_mosaic else f"{code}.m4v"
    script_dir = Path(__file__).resolve().parent
    dodnld_py = script_dir / "dodnld.py"
    # Save video to download/{CODE}/{filename}
    output_path_arg = f"{code}/{output_name}"
    subprocess.Popen(
        [sys.executable, str(dodnld_py), args.url, "--visual", "-o", output_path_arg],
        cwd=str(script_dir),
    )
    print("dodnld.py started, waiting 10 sec...", file=sys.stderr)
    time.sleep(10)
    code_dir = DOWNLOAD_DIR / code
    code_dir.mkdir(parents=True, exist_ok=True)
    out_file = code_dir / f"{code}.txt"
    out_file.write_text(f"{title}\n{code}\n{cast}\n", encoding="utf-8")
    print(title)
    print(f"Saved: {out_file}", file=sys.stderr)
    # Save cover image as download/{CODE}/{CODE}.jpg
    cover_path = code_dir / f"{code}.jpg"
    if not cover_url and code != "unknown":
        # Fallback: URL like https://img.supjav.com/images/2025/12/rbd812pl.jpg
        code_plain = code.replace("-", "").lower()
        cover_url = f"https://img.supjav.com/images/2025/12/{code_plain}pl.jpg"
    if cover_url:
        if save_cover_image(cover_url, cover_path):
            print(f"Saved cover: {cover_path}", file=sys.stderr)
        else:
            print(f"Could not download cover: {cover_url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
