"""
Extract streaming video URLs from a Supjav video page.
Opens the page in headless browser, optionally switches server tab,
captures network requests and DOM (iframe/video/m3u8), outputs unique URLs.
Can download video from the VOE tab via yt-dlp.
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://supjav.com/403831.html"
PAGE_TIMEOUT_MS = 30_000
PLAYER_TIMEOUT_MS = 15_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Reduce Cloudflare/automation detection: launch and context options
STEALTH_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--disable-features=IsolateOrigins,site-per-process",
]
STEALTH_IGNORE_DEFAULT_ARGS = ["--enable-automation"]

STEALTH_INIT_SCRIPT = """
(function() {
  if (typeof Object.defineProperty === 'undefined') return;
  try {
    Object.defineProperty(navigator, 'webdriver', { get: function() { return undefined; }, configurable: true });
  } catch (e) {}
  try {
    if (navigator.__proto__) delete navigator.__proto__.webdriver;
  } catch (e) {}
  if (window.chrome === undefined) window.chrome = { runtime: {} };
  if (!navigator.plugins || navigator.plugins.length === 0) {
    try {
      Object.defineProperty(navigator, 'plugins', { get: function() { return [1, 2, 3, 4, 5]; }, configurable: true });
    } catch (e) {}
  }
  if (!navigator.languages || navigator.languages.length === 0) {
    try {
      Object.defineProperty(navigator, 'languages', { get: function() { return ['en-US', 'en']; }, configurable: true });
    } catch (e) {}
  }
})();
"""


def launch_browser(playwright, headless: bool = True):
    """Launch browser with stealth options; prefer installed Chrome if available."""
    try:
        return playwright.chromium.launch(
            headless=headless,
            channel="chrome",
            args=STEALTH_LAUNCH_ARGS,
            ignore_default_args=STEALTH_IGNORE_DEFAULT_ARGS,
        )
    except Exception:
        return playwright.chromium.launch(
            headless=headless,
            args=STEALTH_LAUNCH_ARGS,
            ignore_default_args=STEALTH_IGNORE_DEFAULT_ARGS,
        )


def new_stealth_context(browser, **kwargs):
    """Create context with realistic locale/timezone and anti-detection init script."""
    opts = {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1280, "height": 720},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "permissions": [],
        "java_script_enabled": True,
        "bypass_csp": False,
    }
    opts.update(kwargs)
    context = browser.new_context(**opts)
    context.add_init_script(STEALTH_INIT_SCRIPT)
    return context


def _chrome_available(playwright) -> bool:
    """True if installed Chrome can be used (better fingerprint than bundled Chromium)."""
    try:
        b = playwright.chromium.launch(channel="chrome", headless=True)
        b.close()
        return True
    except Exception:
        return False


# Patterns and domains to treat as stream-related
STREAM_PATTERNS = (
    r"\.m3u8",
    r"video.*\.mp4",
    r"\.mp4\b",
    r"stream",
    r"player",
    r"embed",
    r"hls",
)
# Domains to block (click hijack / redirect ads) — abort navigation so user stays on player
BLOCKED_REDIRECT_DOMAINS = (
    "dillingers.ie",
    "dillingers.com",
    "cactusheadroomscaling",
)

# Substrings in URL to skip (ads, analytics, tracking)
SKIP_SUBSTRINGS = (
    "google",
    "googlesyndication",
    "doubleclick",
    "analytics",
    "facebook",
    "twitter",
    "ads.",
    "adservice",
    "tracking",
    "pixel",
    "stat.",
    "bluetrafficstream",
    "growcdnssedge",
    "fh-dxy.com",
    "otakusphere",
    "mavrtracktor",
    "mnaspm",
    "xxxvjmp",
    "yandex",
    "flixcdn",
    "mc.yandex",
    "jwpcdn.com",
    "abc.gif",
    "lang/en.json",
    "lib-auto.js",
    "widgets/",
    "domain-checker",
    "api/models",
    "api/click",
    "api/users",
)


def is_stream_output(url: str) -> bool:
    """Keep only URLs that are clearly stream or player (for final output)."""
    lower = url.lower()
    if ".m3u8" in lower:
        return True
    if ".mp4" in lower and any(x in lower for x in ("growcdnssedge", "media-hls.growcdnssedge")):
        return False  # skip ad CDN segments
    if any(x in lower for x in ("turbovidhls.com/t/", "supremejav.com/supjav", "turboviplay.com", "turbosplayer.com", "doppiocdn.com")):
        return True  # player page or video CDN
    if lower.startswith("blob:"):
        return True  # blob URL can be the active video
    return False


def is_stream_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    lower = url.lower()
    for skip in SKIP_SUBSTRINGS:
        if skip in lower:
            return False
    for pattern in STREAM_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def is_likely_player_or_video(url: str) -> bool:
    """Accept iframe/video URLs that look like players or direct video."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    lower = url.lower()
    for skip in SKIP_SUBSTRINGS:
        if skip in lower:
            return False
    # iframe src often points to embed/player pages
    if any(x in lower for x in ("embed", "player", "video", "play", ".m3u8", ".mp4")):
        return True
    return False


# Content-Type values that indicate HLS or video (DownloadHelper-style detection)
MEDIA_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "video/mp4",
    "video/webm",
    "video/mp2t",
    "video/",
    "application/dash+xml",
)


def is_media_content_type(content_type: str) -> bool:
    """True if Content-Type header indicates HLS manifest or video/audio stream."""
    if not content_type:
        return False
    ct = content_type.lower().split(";")[0].strip()
    return any(m in ct for m in MEDIA_CONTENT_TYPES)


def url_not_skipped(url: str) -> bool:
    """True if URL is not from known ad/tracking domains."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    lower = url.lower()
    return not any(s in lower for s in SKIP_SUBSTRINGS)


# Unique marker in anti-debug script on the page; we click the element containing it to proceed
ANTIDEBUG_SCRIPT_MARKER = "c74a68dfbd15fcd6f23a6b26879bc82e"


def click_element_containing_antidebug_script(page) -> None:
    """Click the element that contains the anti-debug script (unlocks player/flow)."""
    try:
        found = page.evaluate(
            f"""() => {{
                const marker = "{ANTIDEBUG_SCRIPT_MARKER}";
                const scripts = document.querySelectorAll("script");
                for (const s of scripts) {{
                    if (s.textContent && s.textContent.includes(marker)) {{
                        const parent = s.parentElement;
                        if (parent) {{ parent.click(); return true; }}
                    }}
                }}
                return false;
            }}"""
        )
        if found:
            page.wait_for_timeout(500)
    except Exception:
        pass


def _remove_ad_overlay_js() -> str:
    """JS that finds overlay containing 'Close ad' / 'LIVE' / chat popups / game ads and removes it (no click)."""
    return r"""
    () => {
        let removed = false;
        const chatGameMarkers = [
            'New message from', 'I wanna chat', 'Click here!', 'wanna chat with you', 'Cristina',
            'Rated 18+ Game', 'Choose the sexiest', 'sexiest girl to fight'
        ];
        const isOverlayOrCard = (p) => {
            const rect = p.getBoundingClientRect();
            if (rect.width < 100 || rect.height < 60) return false;
            if (rect.width > 900 || rect.height > 700) return false;
            if (rect.top > (window.innerHeight || 9999) || rect.left > (window.innerWidth || 9999)) return false;
            const style = window.getComputedStyle(p);
            const pos = style.position;
            return pos === 'fixed' || pos === 'absolute' || pos === 'relative';
        };
        const removeParentOverlay = (el) => {
            let p = el;
            while (p && p !== document.body) {
                if (isOverlayOrCard(p)) { p.remove(); return true; }
                p = p.parentElement;
            }
            return false;
        };
        const walk = (root) => {
            const nodes = Array.from(root.querySelectorAll('*'));
            nodes.forEach(el => {
                const text = (el.innerText || '').slice(0, 500);
                const hasCloseAd = text.indexOf('Close ad') >= 0;
                const hasLive = text === 'LIVE' && el.closest && (el.closest('[class*="ad"]') || el.closest('[id*="ad"]'));
                const hasChatGame = chatGameMarkers.some(m => text.indexOf(m) >= 0);
                if (!hasCloseAd && !hasLive && !hasChatGame) return;
                if (removeParentOverlay(el)) removed = true;
            });
        };
        walk(document.body);
        return removed;
    }
    """


def try_close_ad_overlay(page) -> bool:
    """Remove 'Close ad' / LIVE overlay from DOM (no click, to avoid ad scripts triggering Cloudflare)."""
    try:
        # Main page: remove overlay by DOM
        if page.evaluate(_remove_ad_overlay_js()):
            return True
        # Same-origin iframes (e.g. player with ad overlay)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                if frame.evaluate(_remove_ad_overlay_js()):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def click_center_play_button(page) -> bool:
    """Click only the saved element from .player_center.json (coords then selector)."""
    def log_step(msg: str, err: Exception | None = None) -> None:
        line = f"click_play: {msg}"
        if err is not None:
            line += f" | {type(err).__name__}: {err!s}"
        _visual_log(line)

    if not PLAYER_CENTER_FILE.exists():
        log_step("no saved element")
        return False
    try:
        data = json.loads(PLAYER_CENTER_FILE.read_text())
    except Exception as e:
        log_step("read saved failed", e)
        return False
    x, y = data.get("x"), data.get("y")
    if x is not None and y is not None:
        try:
            log_step(f"trying saved coords x={x} y={y}")
            page.mouse.click(x, y)
            log_step("saved coords click ok")
            return True
        except Exception as e:
            log_step("saved coords failed", e)
    sel = data.get("selector")
    if sel:
        try:
            log_step(f"trying saved selector {sel!r}")
            page.locator(sel).first.scroll_into_view_if_needed(timeout=300)
            page.locator(sel).first.click(force=True, timeout=400)
            log_step("saved selector click ok")
            return True
        except Exception as e:
            log_step("saved selector main failed", e)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame.locator(sel).first.click(force=True, timeout=300)
                log_step("saved selector in frame ok")
                return True
            except Exception:
                pass
    log_step("saved element click failed")
    return False


def move_mouse_around(page) -> None:
    """Move mouse across the screen (a few points) after a click."""
    try:
        w = page.viewport_size.get("width", 1280) or 1280
        h = page.viewport_size.get("height", 720) or 720
        points = [
            (w // 4, h // 2),
            (w // 2, h // 4),
            (w * 3 // 4, h // 2),
            (w // 2, h // 2),
        ]
        for x, y in points:
            page.mouse.move(x, y)
            page.wait_for_timeout(150)
    except Exception:
        pass


def has_jw_video_with_blob_src(page) -> bool:
    """True if page or any frame has <video class=\"jw-video jw-reset\" src=\"blob:...\">."""
    try:
        for frame in page.frames:
            try:
                found = frame.evaluate("""() => {
                    const v = document.querySelector('video.jw-video.jw-reset[src^="blob:"]')
                        || document.querySelector('video.jw-video[src^="blob:"]');
                    return !!(v && v.src && v.src.startsWith('blob:'));
                }""")
                if found:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def get_jw_video_blob_info(page) -> dict | None:
    """If jw-video with blob src exists, return dict with frame_url, video_src, how it's fed (for log)."""
    try:
        for frame in page.frames:
            try:
                info = frame.evaluate("""() => {
                    const v = document.querySelector('video.jw-video.jw-reset[src^="blob:"]')
                        || document.querySelector('video.jw-video[src^="blob:"]');
                    if (!v || !v.src || !v.src.startsWith('blob:')) return null;
                    return {
                        frame_url: window.location.href,
                        video_src: v.src,
                        current_src: v.currentSrc || v.src,
                        ready_state: v.readyState,
                        network_state: v.networkState,
                        error: v.error ? v.error.message : null
                    };
                }""")
                if info and isinstance(info, dict):
                    return info
            except Exception:
                continue
        return None
    except Exception:
        return None


def try_click_player(page) -> bool:
    """Click video or play button to start playback (after ads are gone). Returns True if clicked."""
    try:
        # Main page: video element or Play button
        for loc in [
            page.locator("video").first,
            page.get_by_role("button", name=re.compile(r"Play", re.I)).first,
            page.locator("[class*='play'], [class*='jwplay'], [aria-label*='lay']").first,
        ]:
            try:
                if loc.is_visible(timeout=300):
                    loc.click(force=True, timeout=500)
                    return True
            except Exception:
                pass
        # Inside player iframe
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                for loc in [
                    frame.locator("video").first,
                    frame.locator("body").first,
                ]:
                    try:
                        if loc.is_visible(timeout=200):
                            loc.click(force=True, timeout=400)
                            return True
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    return False


def click_player_center(page) -> bool:
    """Click at saved center (.player_center.json) or at center of video/iframe. Returns True if clicked."""
    try:
        if PLAYER_CENTER_FILE.exists():
            try:
                data = json.loads(PLAYER_CENTER_FILE.read_text())
                x, y = data.get("x"), data.get("y")
                if x is not None and y is not None:
                    page.mouse.click(x, y)
                    return True
            except Exception:
                pass
        video = page.locator("video").first
        if video.is_visible(timeout=500):
            box = video.bounding_box()
            if box and box.get("width") and box.get("height"):
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.click(cx, cy)
                return True
        iframe = page.query_selector("iframe[src^='http']")
        if iframe and iframe.is_visible():
            box = iframe.bounding_box()
            if box and box.get("width") and box.get("height"):
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.click(cx, cy)
                return True
    except Exception:
        pass
    return False


def click_saved_player_target(page) -> bool:
    """Click saved target: remove overlays, then by selector (main + iframes) or (x,y). Returns True if clicked."""
    if not PLAYER_CENTER_FILE.exists():
        return False
    try:
        try_close_ad_overlay(page)
        data = json.loads(PLAYER_CENTER_FILE.read_text())
        sel = data.get("selector")
        if sel:
            for loc in [page.locator(sel).first] + [f.locator(sel).first for f in page.frames if f != page.main_frame]:
                try:
                    loc.scroll_into_view_if_needed(timeout=500)
                    loc.click(force=True, timeout=500)
                    return True
                except Exception:
                    pass
        x, y = data.get("x"), data.get("y")
        if x is not None and y is not None:
            page.mouse.click(x, y)
            return True
    except Exception:
        pass
    return False


def dismiss_ad_overlays(page) -> None:
    """Remove or close ad overlays so the player iframe is accessible."""
    try_close_ad_overlay(page)
    # Click common close buttons (X, Close, Skip)
    for selector in [
        '[class*="close"]', '[class*="dismiss"]', '[aria-label*="lose"]', '[title*="lose"]',
        '[class*="overlay"] button', '[class*="modal"] button', '.ad-close', '#close-ad',
        '[class*="skip"]', '[class*="popup"] [class*="close"]',
    ]:
        try:
            for el in page.locator(selector).all():
                try:
                    if el.is_visible(timeout=500):
                        el.click(force=True, timeout=500)
                        page.wait_for_timeout(300)
                except Exception:
                    pass
        except Exception:
            pass
    # Remove overlay elements via JS (high z-index fullscreen divs that block the player)
    page.evaluate("""
        () => {
            const selectors = [
                '[class*="overlay"]', '[class*="ad-overlay"]', '[id*="overlay"]',
                '[class*="modal"][class*="ad"]', '[class*="popup"]:not([class*="player"])',
                '[class*="bluetraffic"]', '[class*="smartpop"]', 'iframe[src*="bluetraffic"]',
                '[style*="z-index: 999"]', '[style*="z-index: 9999"]'
            ];
            selectors.forEach(sel => {
                try {
                    document.querySelectorAll(sel).forEach(el => {
                        if (el.offsetParent !== null && (el.offsetWidth > 200 || el.offsetHeight > 200)) {
                            el.remove();
                        }
                    });
                } catch (e) {}
            });
            // Remove chat-style ad overlays ("New message from Cristina/Stacy", "I wanna chat", blue OK)
            const chatAdMarkers = ['New message from', 'I wanna chat', 'Click here!', 'wanna chat with you'];
            document.querySelectorAll('div, section, aside, [class*="popup"], [class*="modal"], [class*="overlay"]').forEach(el => {
                if (!el.offsetParent || el.offsetWidth < 100) return;
                const text = (el.innerText || '').slice(0, 400);
                const isChatAd = chatAdMarkers.some(m => text.indexOf(m) >= 0);
                if (!isChatAd) return;
                const style = window.getComputedStyle(el);
                const z = parseInt(style.zIndex, 10) || 0;
                if (z > 50 || style.position === 'fixed') {
                    el.remove();
                } else {
                    let p = el.parentElement;
                    while (p && p !== document.body) {
                        const ps = window.getComputedStyle(p);
                        if (ps.position === 'fixed' || parseInt(ps.zIndex, 10) > 50) {
                            p.remove();
                            break;
                        }
                        p = p.parentElement;
                    }
                }
            });
            // Close player debug/info overlay (Stream Type, Buffer Health)
            document.querySelectorAll('[class*="jw-"][class*="close"], [class*="info-overlay"] [class*="close"], [class*="stats"] [class*="close"]').forEach(el => { try { el.click(); } catch (e) {} });
        }
    """)
    page.wait_for_timeout(500)
    # Click away chat ad "OK" buttons and any remaining close (Playwright by text)
    for text in ["OK", "Close", "×", "Skip"]:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first
            if btn.is_visible(timeout=400):
                btn.click(force=True, timeout=400)
                page.wait_for_timeout(200)
        except Exception:
            pass
    try:
        page.locator('button:has-text("OK")').first.click(force=True, timeout=400)
        page.wait_for_timeout(200)
    except Exception:
        pass


def extract_stream_urls(page_url: str, server_tabs: list[str] | None = None, for_download: bool = False) -> list[str]:
    """Extract stream URLs. Only VOE tab is used."""
    if server_tabs is None:
        server_tabs = ["VOE"]
    collected: set[str] = set()
    wait_after_tab_ms = 8000 if for_download else 4000

    def handle_route(route):
        request = route.request
        url = request.url
        if is_stream_url(url):
            collected.add(url)
        route.continue_()

    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            context = new_stealth_context(browser, java_script_enabled=True)
            context.set_default_timeout(PAGE_TIMEOUT_MS)
            page = context.new_page()

            # Capture request URLs
            page.route("**/*", handle_route)

            # Capture response URLs: by URL pattern and by Content-Type (DownloadHelper-style)
            def on_response(response):
                url = response.url
                if not url_not_skipped(url):
                    return
                if is_stream_url(url):
                    collected.add(url)
                    return
                try:
                    ct = response.headers.get("content-type") or ""
                    if is_media_content_type(ct):
                        collected.add(url)
                except Exception:
                    pass

            page.on("response", on_response)

            page.goto(page_url, wait_until="load", timeout=PAGE_TIMEOUT_MS)
            wait_for_cloudflare_pass(page)
            page.wait_for_timeout(1500)
            dismiss_ad_overlays(page)
            click_element_containing_antidebug_script(page)

            base = page_url.rsplit("/", 1)[0] + "/"

            # Only VOE tab
            for label in server_tabs:
                try:
                    # Try link by text, or any clickable with this text
                    tab = page.get_by_role("link", name=re.compile(label, re.I)).first
                    if not tab.is_visible(timeout=1000):
                        tab = page.locator(f"a:has-text('{label}')").first
                    if not tab.is_visible(timeout=1000):
                        tab = page.locator(f"*:has-text('{label}')").first
                    if tab.is_visible(timeout=1000):
                        tab.click()
                        page.wait_for_timeout(2000)
                        dismiss_ad_overlays(page)
                        click_element_containing_antidebug_script(page)
                        # VOE: wait for player iframe, click inside to start stream
                        if label == "VOE":
                            try:
                                # Prefer VOE player iframe (supremejav) so we get correct video (e.g. RBD-764)
                                try:
                                    page.wait_for_selector("iframe[src*='supremejav']", timeout=12_000)
                                except Exception:
                                    page.wait_for_selector("iframe[src^='http']", timeout=8_000)
                                page.wait_for_timeout(1500)
                                iframe_el = page.query_selector("iframe[src*='supremejav']") or page.query_selector("iframe[src^='http']")
                                if iframe_el:
                                    frame = iframe_el.content_frame()
                                    if frame:
                                        # Click inside player (body or video) to activate playback
                                        try:
                                            frame.locator("video").first.click(force=True, timeout=3000)
                                        except Exception:
                                            try:
                                                frame.locator("body").first.click(force=True, timeout=2000)
                                            except Exception:
                                                box = iframe_el.bounding_box()
                                                if box:
                                                    page.mouse.click(
                                                        box["x"] + box["width"] / 2,
                                                        box["y"] + box["height"] / 2,
                                                    )
                                        page.wait_for_timeout(wait_after_tab_ms)
                            except Exception:
                                pass
                        page.wait_for_timeout(wait_after_tab_ms)
                except Exception:
                    continue

                # Iframe with real URL (http/https) — usually the video player
                for iframe in page.query_selector_all("iframe[src]"):
                    src = (iframe.get_attribute("src") or "").strip()
                    if src.startswith("http"):
                        full = urljoin(base, src)
                        if is_likely_player_or_video(full) or not any(s in full.lower() for s in SKIP_SUBSTRINGS):
                            collected.add(full)

                for video in page.query_selector_all("video"):
                    src = video.get_attribute("src")
                    if src:
                        collected.add(urljoin(base, src))
                    for source in video.query_selector_all("source[src]"):
                        src = source.get_attribute("src")
                        if src:
                            collected.add(urljoin(base, src))

            # Final DOM pass: all iframes (player) and video
            for iframe in page.query_selector_all("iframe[src]"):
                src = (iframe.get_attribute("src") or "").strip()
                if src.startswith("http") and not any(s in src.lower() for s in SKIP_SUBSTRINGS):
                    collected.add(urljoin(base, src))
            for video in page.query_selector_all("video"):
                src = video.get_attribute("src")
                if src:
                    collected.add(urljoin(base, src))
                for source in video.query_selector_all("source[src]"):
                    src = source.get_attribute("src")
                    if src:
                        collected.add(urljoin(base, src))
            for el in page.query_selector_all("[data-src]"):
                src = el.get_attribute("data-src")
                if src and is_stream_url(src):
                    collected.add(urljoin(base, src))

            # Scan full HTML for URLs in scripts/data (including VOE player supremejav)
            content = page.content()
            for match in re.finditer(
                r'https?://[^\s"\'<>\)]+(?:\.m3u8|\.mp4|/stream/|/video/|/embed/|/play/|iframe|player|supremejav|supjav)',
                content,
                re.IGNORECASE,
            ):
                url = match.group(0).rstrip("'\">,)")
                if is_stream_url(url):
                    collected.add(url)
            for match in re.finditer(
                r'https?://[^\s"\'<>\)]*(?:supremejav|turbovidhls\.com/t/)[^\s"\'<>\)]*',
                content,
                re.IGNORECASE,
            ):
                url = match.group(0).rstrip("'\">,)")
                if url.startswith("http") and not any(s in url.lower() for s in SKIP_SUBSTRINGS):
                    collected.add(url)
            # Turbovidhls player path: /t/ID (ID often hex-like); supjav.com@code in fragment
            for match in re.finditer(
                r'https?://[^\s"\'<>\)]*turbovidhls[^\s"\'<>\)]*',
                content,
                re.IGNORECASE,
            ):
                url = match.group(0).rstrip("'\">,)")
                if url.startswith("http") and not any(s in url.lower() for s in SKIP_SUBSTRINGS):
                    collected.add(url)

            if not collected:
                try:
                    with open("debug_page.html", "w", encoding="utf-8") as f:
                        f.write(page.content())
                except Exception:
                    pass

            # Extract video code from page title (e.g. RBD-764, IPZ-590) for matching the right stream
            video_code = None
            try:
                title = page.title() or ""
                m = re.search(r"\b([A-Z]{2,5}-\d{3,5})\b", title, re.I)
                if m:
                    video_code = m.group(1).lower()
            except Exception:
                pass

            # Return only URLs that are clearly streams or player pages
            filtered = [u for u in sorted(collected) if is_stream_output(u)]
            return filtered, video_code
        finally:
            browser.close()


def get_downloadable_url(
    urls: list[str], prefer_voe_player: bool = False, video_code: str | None = None
) -> str | None:
    """Pick best URL for downloading. Stream in player is supjav.com@<code>-ub.mp4 (e.g. rbd-764-ub).
    If video_code (e.g. rbd-764): prefer m3u8 or player URL that contains this code.
    If prefer_voe_player: prefer VOE player pages (supremejav, turbovidhls) to open and get m3u8 from.
    """
    # Skip blob: — not directly downloadable
    candidates = [u for u in urls if u.startswith("http://") or u.startswith("https://")]
    if not candidates:
        return None
    lower_code = (video_code or "").lower().replace(" ", "")

    # Prefer URL that matches the page video (e.g. contains rbd-764 / supjav.com@rbd-764-ub)
    if lower_code:
        for u in candidates:
            if lower_code in u.lower() or f"{lower_code}-ub" in u.lower():
                if ".m3u8" in u.lower() and "_HLS_msn" not in u:
                    return u
        for u in candidates:
            if lower_code in u.lower():
                return u
        # Prefer VOE player URLs that might serve this video (supremejav, turbovidhls)
        player = [u for u in candidates if "supremejav.com/supjav" in u.lower() or "turbovidhls.com/t/" in u.lower()]
        if player:
            return player[0]

    if prefer_voe_player:
        player = [u for u in candidates if "supremejav.com/supjav" in u.lower() or "turbovidhls.com/t/" in u.lower()]
        if player:
            return player[0]
        # Do not use doppiocdn — wrong video; only supremejav/turbovidhls for correct stream
        return None

    # Prefer master playlist m3u8 (no _HLS_msn / _HLS_part)
    m3u8_master = [u for u in candidates if ".m3u8" in u.lower() and "_HLS_msn" not in u and "_HLS_part" not in u]
    if m3u8_master:
        return m3u8_master[0]
    for u in candidates:
        if ".m3u8" in u.lower():
            return u
    return candidates[0]


def extract_m3u8_from_player_page(player_url: str, referer: str = "https://supjav.com/") -> str | None:
    """Open VOE player page (supremejav) in headless browser, click play, capture m3u8 URL."""
    collected: set[str] = set()

    def on_response(response):
        url = response.url
        if ".m3u8" in url.lower() and (url.startswith("http://") or url.startswith("https://")):
            if not any(s in url.lower() for s in SKIP_SUBSTRINGS):
                collected.add(url)

    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            context = new_stealth_context(browser, extra_http_headers={"Referer": referer})
            context.set_default_timeout(30_000)
            page = context.new_page()
            page.on("response", on_response)
            page.goto(player_url, wait_until="load", timeout=30_000)
            page.wait_for_timeout(2000)
            # Click in player to start playback (video or body)
            try:
                page.locator("video").first.click(force=True, timeout=5000)
            except Exception:
                try:
                    page.locator("body").first.click(force=True, timeout=2000)
                except Exception:
                    pass
            page.wait_for_timeout(10_000)  # wait for m3u8 requests after play
            # Prefer master playlist (no segment params)
            m3u8_urls = [u for u in sorted(collected) if ".m3u8" in u]
            for u in m3u8_urls:
                if "_HLS_msn" not in u and "_HLS_part" not in u:
                    return u
            return m3u8_urls[0] if m3u8_urls else None
        finally:
            browser.close()


CLOUDFLARE_WAIT_MS = 25_000  # wait for Cloudflare "Verifying you are human" to pass


def wait_for_cloudflare_pass(page, timeout_ms: int = CLOUDFLARE_WAIT_MS) -> None:
    """Wait until past Cloudflare challenge (page shows VOE/SERVER links)."""
    try:
        page.wait_for_selector('a:has-text("VOE"), a:has-text("SERVER")', timeout=timeout_ms)
    except Exception:
        pass


def wait_for_player_page_loaded(page, timeout_ms: int = CLOUDFLARE_WAIT_MS) -> None:
    """After navigation to player page, wait until past Cloudflare (video/iframe visible)."""
    try:
        page.wait_for_selector("video, iframe[src^='http']", timeout=timeout_ms)
    except Exception:
        pass


PLAYER_CENTER_FILE = Path(__file__).resolve().parent / ".player_center.json"
VISUAL_LOG_FILE = Path(__file__).resolve().parent / ".visual_mode.log"


def _visual_log(msg: str, log_file: Path | None = None) -> None:
    """Append timestamp + message to visual log file for analysis."""
    from datetime import datetime
    log_path = log_file or VISUAL_LOG_FILE
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts} {msg}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def run_visual_mode(page_url: str) -> None:
    """Open page in visible browser, log stream-related requests. User clicks VOE manually; link opens in same tab."""
    user_data_dir = Path(__file__).resolve().parent / ".playwright_profile"
    user_data_dir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = None
        try:
            kwargs = {
                "headless": False,
                "args": STEALTH_LAUNCH_ARGS,
                "ignore_default_args": STEALTH_IGNORE_DEFAULT_ARGS,
                "user_agent": USER_AGENT,
                "viewport": {"width": 1280, "height": 720},
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
            if _chrome_available(p):
                kwargs["channel"] = "chrome"
            context = p.chromium.launch_persistent_context(str(user_data_dir), **kwargs)
            context.add_init_script(STEALTH_INIT_SCRIPT)
            ALT_M_SCRIPT = """
                (function() {
                    if (window.__altMAttached) return;
                    window.__altMAttached = true;
                    function setStreamFlag() {
                        var t = window.top || window;
                        t.__userSawStream = true;
                        t.__userSawStreamTime = typeof Date !== 'undefined' ? Date.now() : 0;
                    }
                    function onAltM(e) {
                        var isM = (e.key === 'm' || e.key === 'M' || e.code === 'KeyM');
                        if (e.altKey && isM) {
                            e.preventDefault();
                            e.stopPropagation();
                            var t = window.top || window;
                            if (window === t) {
                                setStreamFlag();
                            } else {
                                try { t.postMessage({ type: 'ALT_M_PRESSED' }, '*'); } catch (err) {}
                            }
                        }
                    }
                    document.addEventListener('keydown', onAltM, true);
                    window.addEventListener('keydown', onAltM, true);
                    if (window === window.top) {
                        window.addEventListener('message', function(e) {
                            if (e.data && e.data.type === 'ALT_M_PRESSED') {
                                setStreamFlag();
                            }
                        });
                        function addButton() {
                            if (document.getElementById('jav-download-trigger')) return;
                            var btn = document.createElement('button');
                            btn.id = 'jav-download-trigger';
                            btn.textContent = 'Download (Alt+M)';
                            btn.style.cssText = 'position:fixed !important; top:12px !important; right:12px !important; z-index:2147483647 !important; padding:12px 20px !important; background:#e65100 !important; color:#fff !important; border:none !important; border-radius:6px !important; cursor:pointer !important; font-size:16px !important; font-weight:bold !important; box-shadow:0 4px 12px rgba(0,0,0,0.5) !important;';
                            btn.onclick = function() { setStreamFlag(); };
                            (document.body || document.documentElement).appendChild(btn);
                        }
                        if (document.body) { addButton(); } else { document.addEventListener('DOMContentLoaded', addButton); }
                        setTimeout(addButton, 500);
                    }
                })();
            """
            context.add_init_script(ALT_M_SCRIPT)

            def add_download_button_to_main_frame():
                try:
                    page.evaluate("""
                        (function() {
                            if (window !== window.top) return;
                            if (document.getElementById('jav-download-trigger')) return;
                            var btn = document.createElement('button');
                            btn.id = 'jav-download-trigger';
                            btn.textContent = 'Download (Alt+M)';
                            btn.style.cssText = 'position:fixed !important; top:12px !important; right:12px !important; z-index:2147483647 !important; padding:12px 20px !important; background:#e65100 !important; color:#fff !important; border:none !important; border-radius:6px !important; cursor:pointer !important; font-size:16px !important; font-weight:bold !important; box-shadow:0 4px 12px rgba(0,0,0,0.5) !important;';
                            btn.onclick = function() { var t = window.top || window; t.__userSawStream = true; t.__userSawStreamTime = Date.now ? Date.now() : 0; };
                            (document.body || document.documentElement).appendChild(btn);
                        })();
                    """)
                except Exception:
                    pass

            def set_download_button_state(state: str):
                """state: 'idle' | 'downloading' | 'done' | 'failed' | 'no_url'"""
                try:
                    state_js = json.dumps(state)
                    page.evaluate(
                        f"""(function(state) {{
                            var btn = document.getElementById('jav-download-trigger');
                            if (!btn) return;
                            var styles = {{ idle: '#e65100', downloading: '#555', done: '#2e7d32', failed: '#c62828', no_url: '#c62828' }};
                            var texts = {{ idle: 'Download (Alt+M)', downloading: 'Downloading...', done: 'Done', failed: 'Failed', no_url: 'No URL' }};
                            btn.textContent = texts[state] || state;
                            btn.style.background = styles[state] || '#555';
                            btn.disabled = (state === 'downloading');
                        }})({state_js})"""
                    )
                except Exception:
                    pass
            context.set_default_timeout(PAGE_TIMEOUT_MS)

            def block_redirect_route(route):
                url = route.request.url
                if any(dom in url for dom in BLOCKED_REDIRECT_DOMAINS):
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", block_redirect_route)

            def on_new_page(new_page):
                try:
                    _visual_log("new_tab_blocked closing")
                    new_page.close()
                except Exception:
                    pass

            context.on("page", on_new_page)

            page = context.pages[0] if context.pages else context.new_page()

            def log(msg: str) -> None:
                _visual_log(msg)
                print(msg, file=sys.stderr)

            log("visual_mode started")
            log(f"goto {page_url}")

            stream_url_for_download = [None]  # best m3u8 for download (HLS playlist, not jwplayer assets)

            def _is_hls_playlist_url(url: str) -> bool:
                if ".m3u8" not in url:
                    return False
                lower = url.lower()
                if "jwplayer" in lower or "/jwplayer/" in lower:
                    return False
                if "master.m3u8" in lower or ("index" in lower and ".m3u8" in lower):
                    return True
                if "edgeon-bandwidth" in lower and ".m3u8" in lower and "urlset" in lower:
                    return True
                return False

            def on_response(response):
                url = response.url
                if not url.startswith("http"):
                    return
                if not url_not_skipped(url):
                    return
                if is_stream_url(url) or (response.headers.get("content-type") and is_media_content_type(response.headers.get("content-type", ""))):
                    if _is_hls_playlist_url(url):
                        stream_url_for_download[0] = url
                    print(f"[STREAM] {url[:120]}{'...' if len(url) > 120 else ''}")
                    sys.stdout.flush()

            page.on("response", on_response)
            page.goto(page_url, wait_until="load", timeout=PAGE_TIMEOUT_MS)
            log("cloudflare_wait_start")
            wait_for_cloudflare_pass(page)
            log("cloudflare_wait_done")
            page.wait_for_timeout(2000)
            log("dismiss_ad_overlays_start")
            dismiss_ad_overlays(page)
            page.wait_for_timeout(500)
            log("dismiss_ad_overlays_done")
            page.evaluate("""() => {
                document.querySelectorAll('a[target="_blank"]').forEach(a => { a.removeAttribute('target'); });
            }""")
            log("remove_target_blank_done")
            page.wait_for_timeout(500)
            add_download_button_to_main_frame()
            log("download_button_injected (initial page)")
            log("voe_click_start")
            try:
                tab = page.get_by_role("link", name=re.compile(r"VOE", re.I)).first
                if not tab.is_visible(timeout=2000):
                    tab = page.locator("a:has-text('VOE')").first
                if tab.is_visible(timeout=2000):
                    tab.click()
                    log("voe_click_done")
                    page.wait_for_timeout(3000)
                    try:
                        for frame in page.frames:
                            try:
                                frame.evaluate(ALT_M_SCRIPT)
                            except Exception:
                                pass
                        main_has = page.evaluate("() => !!window.__altMAttached")
                        log(f"alt_m_listener_attached (after VOE) main_frame_has_listener={main_has}")
                        page.wait_for_timeout(800)
                        add_download_button_to_main_frame()
                        log("download_button_injected (after VOE)")
                    except Exception as ex:
                        log(f"alt_m_attach_error (after VOE) {ex!r}")
                else:
                    log("voe_tab_not_visible")
            except Exception as e:
                log(f"voe_click_error {e!r}")
            stop_event = threading.Event()

            def wait_enter():
                input()
                stop_event.set()

            threading.Thread(target=wait_enter, daemon=True).start()
            last_waited_url = page.url
            _key_check_iters = [0]
            log("loop_start: When stream is visible: click blue 'Download (Alt+M)' button top-right, or press Alt+M (click page first). Press Enter to close.")
            while not stop_event.wait(2):
                try:
                    current_url = page.url
                    if current_url != last_waited_url:
                        last_waited_url = current_url
                        log("page_changed waiting_cloudflare_player")
                        wait_for_player_page_loaded(page)
                        log("page_changed_done")
                        try:
                            for i, frame in enumerate(page.frames):
                                try:
                                    frame.evaluate(ALT_M_SCRIPT)
                                except Exception:
                                    pass
                            n_frames = len(page.frames)
                            main_has = page.evaluate("() => !!window.__altMAttached")
                            log(f"alt_m_listener_attached (after page change) frames={n_frames} main_frame_has_listener={main_has}")
                            page.wait_for_timeout(800)
                            add_download_button_to_main_frame()
                            log("download_button_injected (after page change)")
                        except Exception as ex:
                            log(f"alt_m_attach_error {ex!r}")
                    try:
                        check = page.evaluate("""() => ({
                            pressed: window.__userSawStream === true,
                            time: window.__userSawStreamTime || 0,
                            raw: window.__userSawStream
                        })""")
                        user_saw_stream = isinstance(check, dict) and check.get("pressed") is True
                        ctrl_r_time = check.get("time", 0) if isinstance(check, dict) else 0
                        _key_check_iters[0] += 1
                        pressed_val = check.get("pressed") if isinstance(check, dict) else None
                        raw_val = check.get("raw") if isinstance(check, dict) else None
                        log(f"key_check_poll: iter={_key_check_iters[0]} pressed={pressed_val!r} raw={raw_val!r}")
                        if not user_saw_stream and isinstance(check, dict):
                            if raw_val is not None and raw_val is not False:
                                log(f"key_check_debug: raw __userSawStream={raw_val!r} (not true)")
                        if _key_check_iters[0] % 15 == 1 and _key_check_iters[0] > 1:
                            log("key_check: still waiting for Alt+M (flag=false)")
                    except Exception as eval_err:
                        user_saw_stream = False
                        ctrl_r_time = 0
                        log(f"key_check_error: evaluate failed {eval_err!r}")
                    if user_saw_stream:
                        from datetime import datetime
                        ts = datetime.now().strftime("%H:%M:%S")
                        log(f"key_check: Alt+M was pressed (flag=true, time={ctrl_r_time}) at {ts}")
                        log("key_combo: Alt+M detected — user confirmed stream visible")
                        page.evaluate("() => { window.__userSawStream = false; window.__userSawStreamTime = 0; }")
                        download_url = stream_url_for_download[0]
                        if download_url:
                            set_download_button_state("downloading")
                            log("========== DOWNLOAD STARTED ==========")
                            log(f"find_stream: using captured m3u8 url (len={len(download_url)})")
                            out_path = Path("video").resolve()
                            log(f"download_start: url={download_url[:80]}...")
                            log(f"save_to: {out_path}.%(ext)s (folder: {out_path.parent})")
                            if download_video(download_url, out_path, referer="https://supjav.com/"):
                                set_download_button_state("done")
                                log("========== DOWNLOAD FINISHED OK ==========")
                            else:
                                set_download_button_state("failed")
                                log("========== DOWNLOAD FAILED ==========")
                        else:
                            set_download_button_state("no_url")
                            log("========== NO STREAM URL — CANNOT DOWNLOAD ==========")
                            log("find_stream: no m3u8 url captured yet")
                    while try_close_ad_overlay(page):
                        log("overlay_closed")
                        page.wait_for_timeout(500)
                except Exception as e:
                    log(f"loop_error {e!r}")
        finally:
            if context is not None:
                context.close()


def download_video(url: str, output_path: str | Path, referer: str = "https://supjav.com/") -> bool:
    """Download video from URL using yt-dlp. Returns True on success. Progress is printed to stderr."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_tpl = str(output_path.with_suffix("")) + ".%(ext)s"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--newline",
        "--add-header", f"Referer:{referer}",
        "--user-agent", USER_AGENT,
        "-o", out_tpl,
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=False,
            text=True,
            timeout=600,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Download failed (exit {e.returncode}): {e.stderr or e.stdout or str(e)}", file=sys.stderr)
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"Download failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract streaming video URLs from a Supjav video page."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help=f"Page URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--download",
        "-d",
        action="store_true",
        help="Download video from VOE tab to current directory",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="video",
        help="Output path for download (default: video)",
    )
    parser.add_argument(
        "--visual",
        "-v",
        action="store_true",
        help="Open page in visible browser; log stream URLs when you click (Enter to close)",
    )
    args = parser.parse_args()

    if args.visual:
        run_visual_mode(args.url)
        return 0

    try:
        # Only VOE tab; ad overlays are dismissed so player iframe is accessible
        urls, video_code = extract_stream_urls(
            args.url,
            server_tabs=["VOE"],
            for_download=args.download,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not urls:
        print("Stream URLs not found", file=sys.stderr)
        return 1

    if args.download:
        download_url = get_downloadable_url(urls, prefer_voe_player=True, video_code=video_code)
        if not download_url:
            print("No downloadable URL (only blob: found). Cannot download.", file=sys.stderr)
            return 1
        # If we got a player page (not m3u8), open it and get m3u8 (supremejav or turbovidhls)
        if ".m3u8" not in download_url.lower() and (
            "supremejav" in download_url or "turbovidhls.com/t/" in download_url
        ):
            label = "RBD-764" if video_code else "VOE player"
            print(f"Opening VOE player page to get stream URL ({label})...", file=sys.stderr)
            m3u8_url = extract_m3u8_from_player_page(download_url)
            if m3u8_url:
                download_url = m3u8_url
            else:
                print("Could not get stream from VOE player.", file=sys.stderr)
                return 1
        print(f"Downloading from: {download_url}", file=sys.stderr)
        if download_video(download_url, args.output, referer="https://supjav.com/"):
            print(f"Saved to: {args.output}", file=sys.stderr)
            return 0
        return 1

    for u in urls:
        print(u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
