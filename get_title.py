"""
Utility for Supjav:

1) Default mode: given URL of a video page, get title/code/cast, call dodnld.py
   and wait for it to finish, then save to download/{CODE}/{CODE}.txt and POSTER.jpg.
   Returns same exit code as dodnld.py: 0 on success, 1 on failure.

2) Cast-list mode (--cast-list): given URL like
   https://supjav.com/category/cast/kijima-airi, walk all pages for this actress
   and save download/{CAST_SLUG}/LIST.TXT where each line is:
   movie_page_url,CODE,upload_date,labels_without_brackets

3) Process-list mode (--process-list CAST_SLUG): read download/{CAST_SLUG}/LIST.TXT
   and for each line that has 'Reducing Mosaic' in labels, call dodnld.py to
   download the movie into download/{CAST_SLUG}/{CODE}/...
"""

import argparse
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# SQLite DB in project root for tracking downloaded films (slug + type + upload_date)
def _db_path() -> Path:
    return Path(__file__).resolve().parent / "downloads.db"


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS downloads (
            slug TEXT NOT NULL,
            type TEXT NOT NULL,
            upload_date TEXT NOT NULL,
            url TEXT,
            labels TEXT,
            PRIMARY KEY (slug, type, upload_date)
        )"""
    )
    conn.commit()


def _already_downloaded(conn: sqlite3.Connection, slug: str, type_str: str, upload_date: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM downloads WHERE slug = ? AND type = ? AND upload_date = ?",
        (slug, type_str, upload_date),
    )
    return cur.fetchone() is not None


def _save_download(conn: sqlite3.Connection, slug: str, type_str: str, upload_date: str, url: str, labels: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO downloads (slug, type, upload_date, url, labels) VALUES (?, ?, ?, ?, ?)",
        (slug, type_str, upload_date, url, labels),
    )
    conn.commit()

from playwright.sync_api import sync_playwright

# Reuse browser setup from main script
from dodnld import (
    DOWNLOAD_DIR,
    launch_browser,
    new_stealth_context,
    wait_for_cloudflare_pass,
)

PAGE_TIMEOUT_MS = 60_000
# JAV code pattern: 2–5 letters, hyphen, digits (e.g. IPZ-590, IPZZ-621, ABP-123)
CODE_PATTERN = re.compile(r"[A-Z]{2,5}-\d+", re.IGNORECASE)


def extract_code_from_title(title: str) -> str | None:
    """Extract code like IPZ-590 or IPZZ-621 from title. Returns first match (hyphen required)."""
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


def collect_cast_list(page_url: str) -> list[tuple[str, str, str, str]]:
    """Walk all pages of a cast listing and collect (url, code_slug, upload_date, labels).

    Logs each processed item and pagination jump to stdout/stderr so progress is visible in terminal.
    """
    page_url = page_url.strip()
    if not page_url.startswith(("http://", "https://")):
        return []
    results: list[tuple[str, str, str]] = []
    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            context = new_stealth_context(browser)
            page = context.new_page()
            current_url = page_url
            seen_urls: set[str] = set()
            seen_items: set[str] = set()  # movie URLs we've already recorded
            while current_url and current_url not in seen_urls:
                seen_urls.add(current_url)
                print(f"[CAST] Page: {current_url}", file=sys.stderr)
                try:
                    page.goto(current_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                except Exception as nav_err:
                    # Network / DNS / Cloudflare error on this page: log and stop pagination gracefully
                    print(f"[CAST] Page navigation error, stopping: {nav_err!r}", file=sys.stderr)
                    break
                wait_for_cloudflare_pass(page)
                page.wait_for_timeout(2000)
                items = page.evaluate(
                    """() => {
                    const out = [];
                    function extractDate(text) {
                        if (!text) return '';
                        // Match 2026-03-14 or 2026/03/14
                        const m = text.match(/\\b(\\d{4}[\\/-]\\d{2}[\\/-]\\d{2})\\b/);
                        return m ? m[1] : '';
                    }
                    // Supjav: cast pages are typically a grid of links to /NNNNNN.html
                    const all = Array.from(document.querySelectorAll('a[href*=\".html\"]'));
                    all.forEach(a => {
                        const href = a.href || '';
                        if (!href.includes('supjav.com')) return;
                        // Only detail pages like /411204.html
                        const path = new URL(href, document.location.href).pathname;
                        if (!/\\/\\d+\\.html(?:[#?].*)?$/.test(path)) return;
                        let title = (a.getAttribute('title') || a.getAttribute('data-title') || a.innerText || '').trim();
                        if (!title) {
                            const t = a.querySelector('.video-title, h3, h4');
                            if (t && t.innerText.trim()) title = t.innerText.trim();
                        }
                        let date = '';
                        // Try container text (card) to pick up date line like 2026/03/14
                        let card = a.closest('.movie-box, .item, .thumb, li, .grid-item, .video-item, .col, .entry');
                        if (!card) card = a.parentElement;
                        if (card) {
                            const t = (card.innerText || '').slice(0, 400);
                            date = extractDate(t);
                        }
                        if (!date) {
                            const text = (a.innerText || '').slice(0, 400);
                            date = extractDate(text);
                        }
                        out.push({ url: href, title, date });
                    });
                    if (out.length) return out;
                    // Fallback: generic cards if structure changes
                    const cards = document.querySelectorAll(
                        '.video-item, .item, .grid-item, .entry, .col, .thumb-block'
                    );
                    cards.forEach(card => {
                        let a = card.querySelector('a[href*=\".html\"]');
                        if (!a) return;
                        const href = a.href;
                        let title = (a.getAttribute('title') || a.getAttribute('data-title') || a.innerText || '').trim();
                        if (!title && card.querySelector('h3, h4')) {
                            title = (card.querySelector('h3, h4').innerText || '').trim();
                        }
                        let date = '';
                        const text = (card.innerText || '').slice(0, 400);
                        date = extractDate(text);
                        out.push({ url: href, title, date });
                    });
                    return out;
                }"""
                )
                if isinstance(items, list):
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        url = str(it.get("url") or "").strip()
                        raw_title = str(it.get("title") or "").strip()
                        # Extract label prefixes in [] like [Reducing Mosaic], [Chinese Subtitles]
                        labels: list[str] = []
                        title_source = raw_title
                        import re  # use global re with simple raw patterns
                        for m in re.finditer(r"\[([^\]]+)\]", title_source):
                            label = m.group(1).strip()
                            if label:
                                labels.append(label)
                        # remove all [..] blocks before extracting slug
                        title_source = re.sub(r"\[[^\]]+\]", "", title_source)
                        # Keep only code-like slug, e.g. NHDTC-108, T38-043
                        slug_match = re.search(r"[A-Z0-9]{2,}-\d+", title_source.upper())
                        title = slug_match.group(0) if slug_match else ""
                        labels_str = " ".join(labels)
                        date = str(it.get("date") or "").strip().replace("/", ".")
                        if not url or url in seen_items:
                            continue
                        results.append((url, title, date, labels_str))
                        seen_items.add(url)
                        print(f"[CAST] Item: {url} | {title} | {date} | {labels_str}", file=sys.stderr)
                # Find next-page link
                next_url = page.evaluate(
                    """() => {
                    function abs(href) {
                        try { return new URL(href, document.location.href).href; } catch (e) { return null; }
                    }
                    const containers = document.querySelectorAll(
                        '.pagination, .wp-pagenavi, .nav-links, .page-navi, .paging'
                    );
                    const links = [];
                    containers.forEach(c => c.querySelectorAll('a').forEach(a => links.push(a)));
                    if (!links.length) {
                        document.querySelectorAll('a').forEach(a => links.push(a));
                    }
                    let candidate = null;
                    links.forEach(a => {
                        if (candidate) return;
                        const t = (a.textContent || '').trim().toLowerCase();
                        if (t === '»' || t === 'next' || t === '>' || t === '>>') {
                            candidate = abs(a.getAttribute('href'));
                        }
                    });
                    return candidate || null;
                }"""
                )
                next_url = next_url if isinstance(next_url, str) else None
                if not next_url or next_url in seen_urls:
                    break
                print(f"[CAST] Next page: {next_url}", file=sys.stderr)
                current_url = next_url
        finally:
            browser.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Supjav helper: default — get title/code/cast for a single video and call dodnld.py; "
            "--cast-list — build LIST.TXT for actress cast page."
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://supjav.com/411204.html",
        help="Page URL: video page by default, or cast page with --cast-list.",
    )
    parser.add_argument(
        "--cast-list",
        action="store_true",
        help="Treat URL as actress cast page (https://supjav.com/category/cast/...), build LIST.TXT instead of calling dodnld.py.",
    )
    parser.add_argument(
        "--process-list",
        metavar="CAST_SLUG",
        help="Process existing download/{CAST_SLUG}/LIST.TXT: for each 'Reducing Mosaic' entry call dodnld.py to download into that actress folder.",
    )
    args = parser.parse_args()

    if args.process_list:
        # Process-list mode: run downloads for entries in LIST.TXT under given actress slug
        cast_slug = args.process_list.strip()
        if not cast_slug:
            print("Invalid CAST_SLUG for --process-list.", file=sys.stderr)
            return 1
        cast_dir = DOWNLOAD_DIR / cast_slug
        list_path = cast_dir / "LIST.TXT"
        if not list_path.exists():
            print(f"LIST.TXT not found: {list_path}", file=sys.stderr)
            return 1
        lines = list_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            print(f"LIST.TXT is empty: {list_path}", file=sys.stderr)
            return 1
        script_dir = Path(__file__).resolve().parent
        dodnld_py = script_dir / "dodnld.py"
        db_path = _db_path()
        conn = sqlite3.connect(str(db_path))
        try:
            _init_db(conn)
        finally:
            pass
        total = 0
        skipped = 0
        for idx, line in enumerate(lines, start=1):
            parts = [p.strip() for p in line.split(",", 3)]
            if len(parts) < 4:
                continue
            url, code, date, labels = parts
            if not url or not code:
                continue
            if "reducing mosaic" not in labels.lower():
                continue
            type_str = "Reducing Mosaic"
            if _already_downloaded(conn, code, type_str, date):
                print(f"[PROCESS] {idx}: skip (already in DB) {code} {date}", file=sys.stderr)
                skipped += 1
                continue
            total += 1
            # Folder name: CODE UNC [DATE]
            date_str = date or ""
            folder_name = f"{code} UNC [{date_str}]" if date_str else f"{code} UNC"
            filename = f"{code}_UNCENSORED.m4v"
            output_path_arg = f"{cast_slug}/{folder_name}/{filename}"
            print(f"[PROCESS] {idx}: {url} -> {output_path_arg}", file=sys.stderr)
            proc = subprocess.run(
                [sys.executable, str(dodnld_py), url, "--visual", "-o", output_path_arg],
                cwd=str(script_dir),
            )
            if proc.returncode == 0:
                _save_download(conn, code, type_str, date, url, labels)
            else:
                print(f"[PROCESS] Failed (exit {proc.returncode}) for {url}", file=sys.stderr)
        conn.close()
        print(f"[PROCESS] Completed. Started {total} downloads, skipped {skipped} (already in DB). List: {list_path}", file=sys.stderr)
        return 0

    if args.cast_list:
        # Cast-list mode: build LIST.TXT under download/{CAST_SLUG}/
        cast_items = collect_cast_list(args.url)
        if not cast_items:
            print("No items found on cast page or failed to parse.", file=sys.stderr)
            return 1
        slug = Path(urlparse(args.url).path).name or "cast"
        cast_dir = DOWNLOAD_DIR / slug
        cast_dir.mkdir(parents=True, exist_ok=True)
        out_file = cast_dir / "LIST.TXT"
        lines = [f"{url},{title},{date},{labels}\n" for (url, title, date, labels) in cast_items]
        out_file.write_text("".join(lines), encoding="utf-8")
        print(f"Saved cast list: {out_file}", file=sys.stderr)
        return 0
    title, code, cast, cover_url = get_video_title(args.url)
    if not title:
        print("Could not extract title.", file=sys.stderr)
        return 1
    code = code or "unknown"
    is_reducing_mosaic = "reducing mosaic" in (title or "").lower()
    output_name = f"{code}_UNCENSORED.m4v" if is_reducing_mosaic else f"{code}.m4v"
    script_dir = Path(__file__).resolve().parent
    dodnld_py = script_dir / "dodnld.py"
    # Save video to download/{CODE}/{filename}; wait for dodnld to finish
    output_path_arg = f"{code}/{output_name}"
    proc = subprocess.run(
        [sys.executable, str(dodnld_py), args.url, "--visual", "-o", output_path_arg],
        cwd=str(script_dir),
    )
    code_dir = DOWNLOAD_DIR / code
    code_dir.mkdir(parents=True, exist_ok=True)
    out_file = code_dir / f"{code}.txt"
    out_file.write_text(f"{title}\n{code}\n{cast}\n", encoding="utf-8")
    print(title)
    print(f"Saved: {out_file}", file=sys.stderr)
    # Save cover image as download/{CODE}/POSTER.jpg
    cover_path = code_dir / "POSTER.jpg"
    if not cover_url and code != "unknown":
        # Fallback: URL like https://img.supjav.com/images/2025/12/rbd812pl.jpg
        code_plain = code.replace("-", "").lower()
        cover_url = f"https://img.supjav.com/images/2025/12/{code_plain}pl.jpg"
    if cover_url:
        if save_cover_image(cover_url, cover_path):
            print(f"Saved cover: {cover_path}", file=sys.stderr)
        else:
            print(f"Could not download cover: {cover_url}", file=sys.stderr)
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
