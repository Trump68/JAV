"""
Extract streaming video URLs from a Supjav video page.
Opens the page in headless browser, optionally switches server tab,
captures network requests and DOM (iframe/video/m3u8), outputs unique URLs.
Can download video from the VOE tab via yt-dlp.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse
import urllib.request

from playwright.sync_api import sync_playwright

try:
    from playwright._impl._errors import TargetClosedError as _TargetClosedError
except ImportError:
    _TargetClosedError = type("TargetClosedError", (Exception,), {})

DEFAULT_URL = "https://supjav.com/403831.html"
PAGE_TIMEOUT_MS = 60_000
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
    "goldensacam.com",
    "purplesacam.com",
    "aj2532.bid",
    "altaffiliatesol",
    "adclickad",
    "t.me",
    "dillingers.ie",
    "dillingers.com",
    "cactusheadroomscaling",
    "popads.",
    "popcash.",
    "exoclick",
    "trafficjunky",
    "juicyads",
    "propellerads",
    "adsterra",
    "clickadu",
    "hilltopads",
    "outbrain",
    "taboola",
    "revcontent",
    "mgid.com",
    "onclkds",
    "adsrvr",
    "doubleclick",
    "googlesyndication",
    "adnxs",
    "criteo",
    "adform",
    "smartadserver",
    "rubiconproject",
    "pubmatic",
    "openx.net",
    "clicksor",
    "adskeeper",
    "revenuehits",
    "popmyads",
    "adcolony",
    "vungle",
    "applovin",
    "inmobi",
    "tapjoy",
)
# Main frame must stay only on these (supjav + player/stream); any other navigation is blocked
ALLOWED_MAIN_DOMAINS = (
    "supjav.com",
    "supremejav.com",
    "turbovid",
    "voe.sx",
    "doppiocdn.com",
    "edgeon-bandwidth.com",
    "dianaavoidthey",
    "streamtape.com",
    "streamtape.xyz",
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
    if any(x in lower for x in ("turbovidhls.com/t/", "supremejav.com/supjav", "turboviplay.com", "turbosplayer.com", "doppiocdn.com", "streamtape")):
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


def _click_center(page, locator) -> bool:
    """Click element by moving mouse to its center (more reliable for some buttons e.g. ST)."""
    try:
        box = locator.bounding_box(timeout=2000)
        if not box or not box.get("width") or not box.get("height"):
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        page.mouse.click(x, y)
        return True
    except Exception:
        return False


def click_center_play_button(page) -> bool:
    """No longer used: saved click from .player_center.json was removed."""
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


# Only click inside these iframes (player), never in ad/other iframes — avoids opening ads on 2nd/3rd click
PLAYER_IFRAME_SRC_SUBSTRINGS = (
    "supremejav",
    "turbovid",
    "doppio",
    "voe.sx",
    "dianaavoidthey",
    "supjav.com",  # same-origin player
    "streamtape",
)


def _frame_is_player_iframe(frame) -> bool:
    """True if frame is an iframe with player src (not ad). Main frame is excluded — player is in iframe."""
    if frame == frame.page.main_frame:
        return False
    try:
        el = frame.frame_element()
        src = (el.get_attribute("src") or "").lower()
        return any(s in src for s in PLAYER_IFRAME_SRC_SUBSTRINGS)
    except Exception:
        return False


def try_click_player(page) -> bool:
    """Click only the player: <video> or center of player iframe. Avoids clicking ad or wrong elements."""
    try:
        try:
            page.wait_for_selector("iframe[src*='supremejav'], iframe[src*='doppio'], iframe[src^='http']", timeout=2000)
        except Exception:
            pass
        # 1) <video> in player iframes — this is the actual player
        for frame in page.frames:
            if not _frame_is_player_iframe(frame):
                continue
            try:
                video = frame.locator("video").first
                if video.is_visible(timeout=800):
                    video.click(force=True, timeout=800)
                    return True
            except Exception:
                pass
        # 2) Center of player iframe only (main video area) — largest player iframe first
        try:
            player_iframes = []
            for iframe_el in page.query_selector_all("iframe"):
                try:
                    src = (iframe_el.get_attribute("src") or "").lower()
                    if not any(s in src for s in PLAYER_IFRAME_SRC_SUBSTRINGS):
                        continue
                    box = iframe_el.bounding_box()
                    if not box or box.get("width", 0) < 200 or box.get("height", 0) < 150:
                        continue
                    player_iframes.append((iframe_el, box["width"] * box["height"]))
                except Exception:
                    continue
            player_iframes.sort(key=lambda x: -x[1])  # largest first
            for iframe_el, _ in player_iframes:
                try:
                    box = iframe_el.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        page.mouse.click(cx, cy)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        # 3) Play button only (no generic button/body) inside player iframes
        for frame in page.frames:
            if frame == page.main_frame or not _frame_is_player_iframe(frame):
                continue
            try:
                for sel in ["[class*='play'][class*='button']", "[class*='big-play']", "[aria-label*='lay']", "[class*='jwplay']"]:
                    try:
                        el = frame.locator(sel).first
                        if el.is_visible(timeout=400):
                            el.click(force=True, timeout=400)
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

            page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
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
                        elif label == "ST":
                            # ST (Streamtape): wait for player iframe, click inside to start stream and capture get_video
                            try:
                                page.wait_for_selector("iframe[src*='streamtape'], iframe[src^='http']", timeout=12_000)
                                page.wait_for_timeout(1500)
                                iframe_el = page.query_selector("iframe[src*='streamtape']") or page.query_selector("iframe[src^='http']")
                                if iframe_el:
                                    frame = iframe_el.content_frame()
                                    if frame:
                                        try:
                                            frame.locator("video").first.click(force=True, timeout=3000)
                                        except Exception:
                                            try:
                                                frame.locator("body").first.click(force=True, timeout=2000)
                                            except Exception:
                                                box = iframe_el.bounding_box()
                                                if box:
                                                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
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

    # Streamtape: prefer get_video URL (downloadable) over embed /e/ (yt-dlp can't use it)
    get_video = [u for u in candidates if "streamtape" in u.lower() and "get_video" in u.lower()]
    if get_video:
        return get_video[0]

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
            context.set_default_timeout(PAGE_TIMEOUT_MS)
            page = context.new_page()
            page.on("response", on_response)
            page.goto(player_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
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


CLOUDFLARE_WAIT_MS = 12_500  # wait for Cloudflare "Verifying you are human" to pass (half of previous 25s)


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
DOWNLOAD_DIR = Path(__file__).resolve().parent / "download"
LAST_DOWNLOAD_URL_FILE = Path(__file__).resolve().parent / "last_download_url.txt"
STREAM_URLS_LOG = Path(__file__).resolve().parent / "stream_urls.log"

# Target stream URL pattern: all substrings must be present (query params may vary between runs)
TARGET_STREAM_URL_PARTS = (
    "edgeon-bandwidth.com",
    "1im9wjkozr96",
    "index-v1-a1.m3u8",
)


def _visual_log(msg: str, log_file: Path | None = None) -> None:
    """Optional: append to visual log (disabled to reduce disk writes)."""
    pass


def _log_stream_url(url: str, source: str = "capture") -> None:
    """Append stream URL to stream_urls.log (timestamp, source, url) for later analysis."""
    from datetime import datetime
    if not url or not url.strip():
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts}\t{source}\t{url.strip()}\n"
    try:
        with open(STREAM_URLS_LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def _is_downloadable_stream_url(url: str | None) -> bool:
    """True if URL can be used for download (yt-dlp etc). Streamtape /e/ embed is not downloadable — need get_video."""
    if not url:
        return False
    lower = url.lower()
    if "streamtape" in lower and "/e/" in lower and "get_video" not in lower:
        return False
    return True


def run_visual_mode(
    page_url: str,
    auto_download: bool = True,
    output_filename: str = "video.m4v",
    server_tab: str = "VOE",
) -> bool:
    """Open page in visible browser; click server_tab (VOE, ST, etc.) then dismiss ads. Returns True if download succeeded (done/stopped), False otherwise."""
    # When saving to a subdir (e.g. download/CODE/file.m4v), do not wipe download/; only ensure target dir exists
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DOWNLOAD_DIR / output_filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    visual_download_success: list = [False]
    user_data_dir = Path(__file__).resolve().parent / ".playwright_profile"
    user_data_dir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = None
        browser_closed_ref = [False]
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
            DOWNLOAD_BUTTON_SCRIPT = """
                (function() {
                    if (window.__downloadBtnAttached) return;
                    window.__downloadBtnAttached = true;
                    window.__userStopDownload = false;
                    window.__downloadInProgress = false;
                    function setStreamFlag() {
                        var t = window.top || window;
                        t.__userSawStream = true;
                        t.__userSawStreamTime = typeof Date !== 'undefined' ? Date.now() : 0;
                    }
                    if (window === window.top) {
                        function addButton() {
                            if (document.getElementById('jav-download-trigger')) return;
                            var btn = document.createElement('button');
                            btn.id = 'jav-download-trigger';
                            btn.textContent = 'Download';
                            btn.style.cssText = 'position:fixed !important; top:0 !important; left:0 !important; right:0 !important; width:100% !important; z-index:2147483647 !important; padding:14px 20px !important; background:#e65100 !important; color:#fff !important; border:none !important; border-radius:0 !important; cursor:pointer !important; font-size:16px !important; font-weight:bold !important; box-shadow:0 4px 12px rgba(0,0,0,0.5) !important; box-sizing:border-box !important;';
                            btn.onclick = function() { var t = window.top || window; if (t.__downloadInProgress) { t.__userStopDownload = true; } else { setStreamFlag(); } };
                            (document.body || document.documentElement).appendChild(btn);
                        }
                        if (document.body) { addButton(); } else { document.addEventListener('DOMContentLoaded', addButton); }
                        setTimeout(addButton, 500);
                    }
                })();
            """
            context.add_init_script(DOWNLOAD_BUTTON_SCRIPT)

            def add_download_button_to_main_frame():
                try:
                    page.evaluate("""
                        (function() {
                            if (window !== window.top) return;
                            if (document.getElementById('jav-download-trigger')) return;
                            var btn = document.createElement('button');
                            btn.id = 'jav-download-trigger';
                            btn.textContent = 'Download';
                            btn.style.cssText = 'position:fixed !important; top:0 !important; left:0 !important; right:0 !important; width:100% !important; z-index:2147483647 !important; padding:14px 20px !important; background:#e65100 !important; color:#fff !important; border:none !important; border-radius:0 !important; cursor:pointer !important; font-size:16px !important; font-weight:bold !important; box-shadow:0 4px 12px rgba(0,0,0,0.5) !important; box-sizing:border-box !important;';
                            btn.onclick = function() { var t = window.top || window; if (t.__downloadInProgress) { t.__userStopDownload = true; } else { t.__userSawStream = true; t.__userSawStreamTime = Date.now ? Date.now() : 0; } };
                            (document.body || document.documentElement).appendChild(btn);
                        })();
                    """)
                except Exception:
                    pass

            def set_download_button_state(state: str):
                """state: 'idle' | 'downloading' | 'done' | 'failed' | 'no_url'"""
                if browser_closed_ref[0]:
                    return
                try:
                    state_js = json.dumps(state)
                    page.evaluate(
                        f"""(function(state) {{
                            var btn = document.getElementById('jav-download-trigger');
                            if (!btn) return;
                            var styles = {{ idle: '#e65100', downloading: '#555', done: '#2e7d32', failed: '#c62828', no_url: '#c62828', stopped: '#2e7d32' }};
                            var texts = {{ idle: 'Download', downloading: 'Downloading...', done: 'Done', failed: 'Failed', no_url: 'No URL', stopped: 'Stopped (saved)' }};
                            btn.textContent = texts[state] || state;
                            btn.style.background = styles[state] || '#555';
                            btn.disabled = false;
                            (window.top || window).__downloadInProgress = (state === 'downloading');
                        }})({state_js})"""
                    )
                except (_TargetClosedError, Exception):
                    pass

            def set_download_button_progress(text: str):
                """Set button text to progress string (e.g. '45% · 2.5 MB/s') during download."""
                if browser_closed_ref[0]:
                    return
                try:
                    safe = (text or "Downloading...")[:120]
                    text_js = json.dumps(safe)
                    page.evaluate(
                        f"""(function(t) {{
                            var btn = document.getElementById('jav-download-trigger');
                            if (!btn) return;
                            btn.textContent = t;
                            btn.style.background = '#555';
                            btn.disabled = false;
                            (window.top || window).__downloadInProgress = true;
                        }})({text_js})"""
                    )
                except (_TargetClosedError, Exception):
                    pass
            context.set_default_timeout(PAGE_TIMEOUT_MS)

            def block_redirect_route(route):
                url = route.request.url
                # Abort any request to blocked ad/redirect domains (e.g. goldensacam) regardless of type
                if any(dom in url for dom in BLOCKED_REDIRECT_DOMAINS):
                    route.abort()
                    return
                # Only allow document navigations within supjav ecosystem (intercept and forbid others)
                res_type = getattr(route.request, "resource_type", None)
                if res_type == "document":
                    if not any(dom in url for dom in ALLOWED_MAIN_DOMAINS):
                        route.abort()
                        return
                # Main-frame document: also block if frame is main (some navigations may report differently)
                try:
                    req = route.request
                    frame = getattr(req, "frame", None)
                    if frame and frame == page.main_frame and (res_type == "document" or res_type is None):
                        if not any(dom in url for dom in ALLOWED_MAIN_DOMAINS) or any(dom in url for dom in BLOCKED_REDIRECT_DOMAINS):
                            route.abort()
                            return
                except Exception:
                    pass
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

            def _on_framenavigated(frame):
                """Intercept main-frame navigation to forbidden site and go back immediately."""
                try:
                    if frame != page.main_frame:
                        return
                    url = frame.url
                    if any(dom in url for dom in BLOCKED_REDIRECT_DOMAINS) or not any(dom in url for dom in ALLOWED_MAIN_DOMAINS):
                        _visual_log("intercept_forbidden_navigation going_back")
                        page.go_back(timeout=5000)
                except Exception:
                    pass

            page.on("framenavigated", _on_framenavigated)

            def log(msg: str) -> None:
                _visual_log(msg)
                print(msg, file=sys.stderr)

            from datetime import datetime as _dt
            timeline_entries: list[tuple[str, str]] = []

            def timeline(action: str) -> None:
                ts = _dt.now().strftime("%H:%M:%S.%f")[:-3]
                timeline_entries.append((ts, action))
                _visual_log(f"[TIMELINE] {ts} {action}")

            def _is_target_stream_url(url: str) -> bool:
                return all(part in url for part in TARGET_STREAM_URL_PARTS)

            log("visual_mode started")
            timeline("start")
            log(f"goto {page_url}")
            timeline("goto_page")

            stream_url_for_download = [None]  # best m3u8 for download (HLS playlist, not jwplayer assets)
            target_stream_seen_ref = [False]

            def _is_hls_playlist_url(url: str) -> bool:
                if ".m3u8" not in url:
                    return False
                lower = url.lower()
                if "jwplayer" in lower or "/jwplayer/" in lower:
                    return False
                # Segment URLs (not playlists): skip so we keep master/playlist only
                if "_HLS_msn" in lower or "_HLS_part" in lower or "/segment" in lower or "segment/" in lower:
                    return False
                if "master.m3u8" in lower or ("index" in lower and ".m3u8" in lower):
                    return True
                if "edgeon-bandwidth" in lower and ".m3u8" in lower and "urlset" in lower:
                    return True
                # ST/TV and other players often use playlist.m3u8, video.m3u8, or plain .m3u8 — accept as playlist
                if ".m3u8" in lower and ("playlist" in lower or "video.m3u8" in lower or "manifest" in lower):
                    return True
                # Path ends with .m3u8 and filename is not a segment index (e.g. "0.m3u8", "1.m3u8")
                try:
                    path = url.split("?")[0].rstrip("/")
                    if path.endswith(".m3u8"):
                        name = path.split("/")[-1]
                        base = name[:-5]  # without .m3u8
                        if not (base.isdigit() or (len(base) <= 2 and base.isalnum())):
                            return True
                except Exception:
                    pass
                return False

            def on_response(response):
                url = response.url
                if not url.startswith("http"):
                    return
                if not url_not_skipped(url):
                    return
                is_media = bool(response.headers.get("content-type") and is_media_content_type(response.headers.get("content-type", "")))
                lower = url.lower()
                is_st_tv_m3u8 = server_tab in ("ST", "TV") and ".m3u8" in lower
                is_streamtape = "streamtape" in lower and (
                    ".m3u8" in lower or ".mp4" in lower or "get_video" in lower or "/file/" in lower or "/e/" in lower or is_media
                )
                if not (is_stream_url(url) or is_media or is_st_tv_m3u8 or is_streamtape):
                    return
                if _is_hls_playlist_url(url):
                    stream_url_for_download[0] = url
                    timeline("stream_captured_m3u8")
                elif is_st_tv_m3u8:
                    if "_HLS_msn" not in lower and "_HLS_part" not in lower and "segment" not in lower:
                        stream_url_for_download[0] = url
                        timeline("stream_captured_m3u8_st_tv")
                elif is_streamtape:
                    current = stream_url_for_download[0] or ""
                    if "get_video" in url.lower():
                        stream_url_for_download[0] = url
                    elif "get_video" not in current.lower():
                        stream_url_for_download[0] = url
                    timeline("stream_captured_streamtape")
                if _is_target_stream_url(url):
                    target_stream_seen_ref[0] = True
                    timeline(f"TARGET_STREAM_APPEARED: {url}")
                    log("Target stream link appeared.")
                    stream_url_for_download[0] = url
                    if auto_download and _is_downloadable_stream_url(url):
                        auto_download_pending_ref[0] = True
                print(f"[STREAM] {url[:120]}{'...' if len(url) > 120 else ''}")
                sys.stdout.flush()

            page.on("response", on_response)
            page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            timeline("page_loaded")
            log("cloudflare_wait_start")
            wait_for_cloudflare_pass(page)
            log("cloudflare_wait_done")
            timeline("cloudflare_passed")
            page.wait_for_timeout(2000)
            page.evaluate("""() => {
                document.querySelectorAll('a.btn-server[target="_blank"]').forEach(a => { a.removeAttribute('target'); });
            }""")
            timeline("remove_target_blank_done")
            page.wait_for_timeout(500)
            tabs_to_try = ["VOE", "TV", "ST"] if server_tab == "VOE" else [server_tab]
            log(f"server_tab_click_start (try: {tabs_to_try}) — only a.btn-server or SERVER block (avoid ad links)")
            tab_clicked = False
            try:
                try:
                    page.wait_for_selector('text=SERVER', timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
                for try_tab in tabs_to_try:
                    tab_clicked = False
                    if try_tab == "ST":
                        dismiss_ad_overlays(page)
                        page.wait_for_timeout(400)
                        for _ in range(2):
                            try_close_ad_overlay(page)
                            page.wait_for_timeout(200)
                    _tab_label_esc = re.escape(try_tab)
                    # Build JS that clicks only a.btn-server with safe href (no ad domains) — use this FIRST for VOE/TV to avoid opening ads
                    _label_esc_js = try_tab.replace("\\", "\\\\").replace("'", "\\'")
                    _click_tab_js = f"""() => {{
                            var label = '{_label_esc_js}';
                            var adLike = /ads?\\b|popads|popcash|exoclick|propeller|dillinger|cactushead|juicyads|trafficjunky|revcontent|taboola|outbrain|mgid\\.com|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                            var btns = document.querySelectorAll('a.btn-server');
                            for (var i = 0; i < btns.length; i++) {{
                                var a = btns[i];
                                if ((a.textContent || a.innerText || '').trim() !== label) continue;
                                var h = (a.getAttribute('href') || '').trim();
                                if (adLike.test(h)) continue;
                                a.scrollIntoView({{ block: 'center' }});
                                a.click();
                                return true;
                            }}
                            var server = null;
                            document.querySelectorAll('*').forEach(function(el) {{
                                if (server) return;
                                var t = (el.innerText || '').trim();
                                if (t.indexOf('SERVER') >= 0 && t.indexOf(label) >= 0 && t.length < 150) {{
                                    var links = el.querySelectorAll('a.btn-server');
                                    for (var j = 0; j < links.length; j++) {{
                                        var a = links[j];
                                        if ((a.textContent || '').trim() !== label) continue;
                                        if (adLike.test((a.getAttribute('href') || ''))) continue;
                                        a.scrollIntoView({{ block: 'center' }});
                                        a.click();
                                        server = true;
                                        return true;
                                    }}
                                }}
                            }});
                            return !!server;
                        }}"""
                    # Try safe JS click first for ALL tabs (skips ad hrefs, bypasses overlays)
                    tab_clicked = page.evaluate(_click_tab_js)
                    if not tab_clicked:
                        for frame in page.frames:
                            if frame == page.main_frame:
                                continue
                            try:
                                if frame.evaluate(_click_tab_js):
                                    tab_clicked = True
                                    break
                            except Exception:
                                pass
                    if tab_clicked:
                        page.wait_for_timeout(400)
                    # Only if safe click failed: try Playwright locator (may hit ad if multiple VOE links)
                    if not tab_clicked:
                        try:
                            btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^" + _tab_label_esc + r"$")).first
                            if btn.is_visible(timeout=3000):
                                btn.scroll_into_view_if_needed()
                                page.wait_for_timeout(200)
                                if try_tab == "ST":
                                    tab_clicked = _click_center(page, btn)
                                    if not tab_clicked:
                                        btn.click(force=True)
                                        tab_clicked = True
                                else:
                                    btn.click(force=True)
                                    tab_clicked = True
                        except Exception:
                            pass
                    if not tab_clicked:
                        for frame in page.frames:
                            try:
                                btn = frame.locator("a.btn-server").filter(has_text=re.compile(r"^" + _tab_label_esc + r"$")).first
                                if btn.is_visible(timeout=2000):
                                    btn.scroll_into_view_if_needed()
                                    page.wait_for_timeout(200)
                                    if try_tab == "ST":
                                        tab_clicked = _click_center(page, btn)
                                        if not tab_clicked:
                                            btn.click(force=True)
                                            tab_clicked = True
                                    else:
                                        btn.click(force=True)
                                        tab_clicked = True
                                    break
                            except Exception:
                                pass
                    if not tab_clicked:
                        tab_clicked = page.evaluate(_click_tab_js)
                        if not tab_clicked:
                            for frame in page.frames:
                                if frame == page.main_frame:
                                    continue
                                try:
                                    if frame.evaluate(_click_tab_js):
                                        tab_clicked = True
                                        break
                                except Exception:
                                    pass
                    if tab_clicked:
                        server_tab = try_tab
                        timeline(f"server_tab_clicked_{server_tab}")
                        break
                    if try_tab == "ST" and not tab_clicked:
                        page.wait_for_timeout(2_000)
                        dismiss_ad_overlays(page)
                        page.wait_for_timeout(400)
                        try_close_ad_overlay(page)
                        try:
                            btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^ST$")).first
                            if btn.is_visible(timeout=2000):
                                btn.scroll_into_view_if_needed()
                                page.wait_for_timeout(200)
                                tab_clicked = _click_center(page, btn)
                                if not tab_clicked:
                                    btn.click(force=True)
                                    tab_clicked = True
                                pass
                        except Exception:
                            pass
                        if not tab_clicked:
                            tab_clicked = page.evaluate(_click_tab_js)
                        if tab_clicked:
                            server_tab = "ST"
                            timeline("server_tab_clicked_st")
                            break
                    page.wait_for_timeout(300)
                if not tab_clicked:
                    log(f"server_tab_not_visible (tried {tabs_to_try})")
                else:
                    log(f"server_tab_clicked: {server_tab}")
                    page.wait_for_timeout(3000)
                # ST: verify iframe loaded; if not, retry clicks
                if server_tab == "ST" and tab_clicked:
                    def _st_iframe_loaded():
                        for f in page.frames:
                            if f == page.main_frame:
                                continue
                            furl = f.url or ""
                            if "streamtape" in furl.lower() or "supremejav" in furl.lower():
                                return True
                        return False
                    if not _st_iframe_loaded():
                        for retry in range(5):
                            dismiss_ad_overlays(page)
                            try_close_ad_overlay(page)
                            page.wait_for_timeout(500)
                            page.evaluate(_click_tab_js)
                            page.wait_for_timeout(3000)
                            if _st_iframe_loaded():
                                break
                            try:
                                btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^ST$")).first
                                if btn.is_visible(timeout=1000):
                                    btn.evaluate("el => el.click()")
                                    page.wait_for_timeout(3000)
                                    if _st_iframe_loaded():
                                        break
                            except Exception:
                                pass
            except Exception as e:
                log(f"server_tab_click_error {e!r}")
            dismiss_ad_overlays(page)
            page.wait_for_timeout(500)
            timeline("dismiss_ad_overlays_done")
            for _ in range(2):
                try_close_ad_overlay(page)
                page.wait_for_timeout(300)
            add_download_button_to_main_frame()
            timeline("download_button_injected_initial")
            try:
                for frame in page.frames:
                    try:
                        frame.evaluate(DOWNLOAD_BUTTON_SCRIPT)
                    except Exception:
                        pass
                page.evaluate("() => !!window.__downloadBtnAttached")
                page.wait_for_timeout(800)
                add_download_button_to_main_frame()
            except Exception as ex:
                log(f"download_button_attach_error (after tab) {ex!r}")
            stop_event = threading.Event()

            def wait_enter():
                input()
                stop_event.set()

            threading.Thread(target=wait_enter, daemon=True).start()
            last_waited_url = page.url
            _key_check_iters = [0]
            auto_click_iters = [0]
            voe_click_loop_done_ref = [False]  # VOE: run "click until stream" loop only once
            tv_click_loop_done_ref = [False]   # TV/ST: run "click until stream" loop only once (~60s timeout)
            voe_failed_try_tv_ref = [False]    # after VOE timeout, try TV once
            tv_failed_try_st_ref = [False]     # after TV timeout, try ST once
            download_proc_ref: list = []
            stopped_by_user_ref: list = [False]
            download_progress_text_ref: list = [None]
            download_finished_ref: list = [None]
            download_thread_ref: list = [None]
            auto_download_pending_ref: list = [False]
            download_data_flowing = threading.Event()

            def progress_from_download_thread(text: str):
                """Called from download thread: only store progress; main thread updates the button."""
                download_progress_text_ref[0] = text
                if text and any(c.isdigit() for c in text):
                    download_data_flowing.set()

            log("Ready. Click Download when stream is visible; click again to stop download.")
            while True:
                poll_interval = 0.4 if download_proc_ref else 2.0
                if stop_event.wait(poll_interval):
                    break
                try:
                    if download_progress_text_ref[0] is not None:
                        set_download_button_progress(download_progress_text_ref[0])
                    if download_finished_ref[0] is not None:
                        set_download_button_state(download_finished_ref[0])
                        log(f"Download state: {download_finished_ref[0]}.")
                        download_finished_ref[0] = None
                        download_progress_text_ref[0] = None
                    if download_proc_ref and page.evaluate("() => !!(window.__userStopDownload || (window.top && window.top.__userStopDownload))"):
                        try:
                            stopped_by_user_ref[0] = True
                            download_proc_ref[0].kill()
                            download_proc_ref[0].wait(timeout=5)
                        except Exception:
                            pass
                        download_proc_ref.clear()
                        page.evaluate("() => { try { window.__userStopDownload = false; if (window.top) window.top.__userStopDownload = false; } catch(e){} }")
                        set_download_button_state("stopped")
                        log("Download stopped (saved).")
                    if auto_download_pending_ref[0]:
                        auto_download_pending_ref[0] = False
                        download_url = stream_url_for_download[0]
                        if download_url and not _is_downloadable_stream_url(download_url):
                            log(f"Waiting for direct stream URL (have embed only: {download_url[:80]}...)")
                            download_url = None
                        if download_url:
                            log("Auto-download: target link appeared, starting.")
                            _log_stream_url(download_url, "download_click")
                            try:
                                LAST_DOWNLOAD_URL_FILE.write_text(download_url, encoding="utf-8")
                            except Exception:
                                pass
                            log("Stream URL saved to stream_urls.log and last_download_url.txt")
                            set_download_button_state("downloading")
                            log("Auto-download started. You can close the browser; download will continue.")
                            out_path = DOWNLOAD_DIR / output_filename
                            stopped_by_user_ref[0] = False
                            download_proc_ref.clear()

                            def run_download():
                                try:
                                    result = download_video(
                                        download_url,
                                        out_path,
                                        referer="https://supjav.com/",
                                        progress_callback=progress_from_download_thread,
                                        out_proc=download_proc_ref,
                                        stopped_by_user=stopped_by_user_ref,
                                    )
                                    download_proc_ref.clear()
                                    if stopped_by_user_ref[0]:
                                        download_finished_ref[0] = "stopped"
                                    elif result:
                                        download_finished_ref[0] = "done"
                                        log("Download finished.")
                                    else:
                                        download_finished_ref[0] = "failed"
                                        log("Download failed.")
                                except Exception as e:
                                    download_proc_ref.clear()
                                    download_finished_ref[0] = "failed"
                                    if not isinstance(e, _TargetClosedError):
                                        _visual_log(f"download_error: {e!r}")
                                finally:
                                    download_data_flowing.set()

                            t = threading.Thread(target=run_download, daemon=True)
                            download_thread_ref[0] = t
                            t.start()
                            log("Waiting for download to start before closing browser...")
                            data_ok = download_data_flowing.wait(timeout=20)
                            dl_failed = download_finished_ref[0] == "failed"
                            # If download failed or timed out without data, try ST as fallback
                            if (dl_failed or not data_ok) and server_tab in ("TV", "VOE"):
                                log(f"Download {'failed' if dl_failed else 'timed out'} on {server_tab}, switching to ST...")
                                # Kill stuck download process
                                if download_proc_ref:
                                    try:
                                        download_proc_ref[0].kill()
                                        download_proc_ref[0].wait(timeout=5)
                                    except Exception:
                                        pass
                                    download_proc_ref.clear()
                                download_finished_ref[0] = None
                                download_data_flowing.clear()
                                stream_url_for_download[0] = None
                                target_stream_seen_ref[0] = False
                                auto_download_pending_ref[0] = False
                                server_tab = "ST"
                                dismiss_ad_overlays(page)
                                page.wait_for_timeout(500)
                                _st_js = """() => {
                                    var btns = document.querySelectorAll('a.btn-server');
                                    for (var i = 0; i < btns.length; i++) {
                                        if ((btns[i].textContent || '').trim() === 'ST') {
                                            btns[i].scrollIntoView({block:'center'});
                                            btns[i].click();
                                            return true;
                                        }
                                    }
                                    return false;
                                }"""
                                try:
                                    page.evaluate(_st_js)
                                except Exception:
                                    pass
                                page.wait_for_timeout(5000)
                                page._st_click_loop_done = False
                                continue
                            elif dl_failed:
                                log("Download failed before data started flowing.")
                            elif data_ok:
                                log("Download confirmed, closing browser.")
                            else:
                                log("Timeout waiting for data flow, closing browser anyway.")
                            browser_closed_ref[0] = True
                            try:
                                context.close()
                            except Exception:
                                pass
                            break
                        else:
                            set_download_button_state("no_url")
                            _visual_log("No stream URL (auto).")
                    current_url = page.url
                    if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS):
                        _visual_log("blocked_ad_navigation going_back")
                        try:
                            page.go_back()
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        continue
                    if not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                        _visual_log("foreign_site_navigation going_back")
                        try:
                            page.go_back()
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        continue
                    if current_url != last_waited_url:
                        last_waited_url = current_url
                        timeline(f"page_changed: {current_url[:80]}...")
                        wait_for_player_page_loaded(page)
                        timeline("player_page_loaded")
                        try:
                            page.evaluate("window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;")
                            page.wait_for_timeout(300)
                        except Exception:
                            pass
                        try:
                            for i, frame in enumerate(page.frames):
                                try:
                                    frame.evaluate(DOWNLOAD_BUTTON_SCRIPT)
                                except Exception:
                                    pass
                            page.evaluate("() => !!window.__downloadBtnAttached")
                            page.wait_for_timeout(800)
                            add_download_button_to_main_frame()
                        except Exception as ex:
                            log(f"download_button_attach_error {ex!r}")
                    try:
                        check = page.evaluate("""() => ({
                            pressed: window.__userSawStream === true,
                            time: window.__userSawStreamTime || 0,
                            raw: window.__userSawStream
                        })""")
                        user_saw_stream = isinstance(check, dict) and check.get("pressed") is True
                        _key_check_iters[0] += 1
                    except Exception as eval_err:
                        user_saw_stream = False
                        ctrl_r_time = 0
                        _visual_log(f"key_check_error: {eval_err!r}")
                    if user_saw_stream:
                        timeline("user_clicked_download_button")
                        page.evaluate("() => { window.__userSawStream = false; window.__userSawStreamTime = 0; }")
                        download_url = stream_url_for_download[0]
                        if download_url and not _is_downloadable_stream_url(download_url):
                            log(f"Waiting for direct stream URL (have embed only: {download_url[:80]}...)")
                            set_download_button_state("idle")
                        elif download_url:
                            _log_stream_url(download_url, "download_click")
                            try:
                                LAST_DOWNLOAD_URL_FILE.write_text(download_url, encoding="utf-8")
                            except Exception:
                                pass
                            log("Stream URL saved to stream_urls.log and last_download_url.txt")
                            set_download_button_state("downloading")
                            log("Download started. You can close the browser; download will continue.")
                            out_path = DOWNLOAD_DIR / output_filename
                            stopped_by_user_ref[0] = False
                            download_proc_ref.clear()

                            def run_download():
                                try:
                                    result = download_video(
                                        download_url,
                                        out_path,
                                        referer="https://supjav.com/",
                                        progress_callback=progress_from_download_thread,
                                        out_proc=download_proc_ref,
                                        stopped_by_user=stopped_by_user_ref,
                                    )
                                    download_proc_ref.clear()
                                    if stopped_by_user_ref[0]:
                                        download_finished_ref[0] = "stopped"
                                    elif result:
                                        download_finished_ref[0] = "done"
                                        log("Download finished.")
                                    else:
                                        download_finished_ref[0] = "failed"
                                        log("Download failed.")
                                except Exception as e:
                                    download_proc_ref.clear()
                                    download_finished_ref[0] = "failed"
                                    if not isinstance(e, _TargetClosedError):
                                        _visual_log(f"download_error: {e!r}")
                                finally:
                                    download_data_flowing.set()

                            t = threading.Thread(target=run_download, daemon=True)
                            download_thread_ref[0] = t
                            t.start()
                            log("Waiting for download to start before closing browser...")
                            if download_data_flowing.wait(timeout=20) or download_finished_ref[0]:
                                if download_finished_ref[0] == "failed":
                                    log("Download failed before data started flowing.")
                                else:
                                    log("Download confirmed, closing browser.")
                            else:
                                log("Timeout waiting for data flow, closing browser anyway.")
                            browser_closed_ref[0] = True
                            try:
                                context.close()
                            except Exception:
                                pass
                            break
                        else:
                            set_download_button_state("no_url")
                            _visual_log("No stream URL.")
                    while try_close_ad_overlay(page):
                        _visual_log("overlay_closed")
                        timeline("overlay_closed")
                        page.wait_for_timeout(500)
                    on_player_page = current_url != page_url
                    if not on_player_page:
                        try:
                            if page.query_selector("iframe[src*='supremejav'], iframe[src*='dianaavoidthey'], iframe[src*='turbovid'], iframe[src*='doppio'], iframe[src*='streamtape']"):
                                on_player_page = True
                        except Exception:
                            pass
                    # Streamtape: if we have embed but no get_video yet, click play inside iframe to trigger it
                    if on_player_page and stream_url_for_download[0] and not _is_downloadable_stream_url(stream_url_for_download[0]):
                        if not getattr(page, "_st_click_loop_done", False):
                            page._st_click_loop_done = True
                            log("Streamtape: clicking play to get direct URL...")
                            try:
                                page.evaluate("window.scrollTo(0, 0)")
                                page.wait_for_timeout(500)
                            except Exception:
                                pass
                            for st_attempt in range(20):
                                cur_url = stream_url_for_download[0] or ""
                                if _is_downloadable_stream_url(cur_url):
                                    log("Streamtape: got direct URL, starting download.")
                                    auto_download_pending_ref[0] = True
                                    break
                                try:
                                    dismiss_ad_overlays(page)
                                    try_close_ad_overlay(page)
                                    # Find streamtape frame by frame.url (not by src attr — frame may have navigated)
                                    st_iframe = None
                                    for frame in page.frames:
                                        if frame == page.main_frame:
                                            continue
                                        try:
                                            furl = frame.url or ""
                                            if "streamtape" in furl.lower():
                                                try:
                                                    fel = frame.frame_element()
                                                except Exception:
                                                    fel = None
                                                st_iframe = (frame, fel)
                                                break
                                        except Exception:
                                            pass
                                    if not st_iframe:
                                        page.wait_for_timeout(3000)
                                        continue
                                    frame, fel = st_iframe
                                    box = None
                                    if fel:
                                        try:
                                            box = fel.bounding_box()
                                        except Exception:
                                            pass
                                    pass
                                    # If no bounding box from frame_element, try to find the iframe via selector
                                    if (not box or box.get("width", 0) < 100 or box.get("height", 0) < 50) and not fel:
                                        for iframe_sel in ["iframe[src*='streamtape']", "iframe[src*='supremejav']", "iframe"]:
                                            try:
                                                iel = page.locator(iframe_sel).first
                                                if iel.is_visible(timeout=500):
                                                    box = iel.bounding_box()
                                                    if box and box.get("width", 0) > 100:
                                                        fel = iel
                                                        break
                                            except Exception:
                                                pass
                                    if fel:
                                        try:
                                            fel.scroll_into_view_if_needed(timeout=2000)
                                            page.wait_for_timeout(500)
                                            box = fel.bounding_box()
                                        except Exception:
                                            pass
                                    cx = (box["x"] + box["width"] / 2) if box else 640
                                    cy = (box["y"] + box["height"] / 2) if box else 360
                                    # Try clicking inside the iframe via frame context
                                    clicked_inside = False
                                    for sel in [
                                        "#videolink", ".play-overlay", ".plyr__control--overlaid",
                                        "button[class*='play']", "[class*='play-btn']", "[class*='play_btn']",
                                        "video", "body",
                                    ]:
                                        try:
                                            el = frame.locator(sel).first
                                            if el.is_visible(timeout=500):
                                                el.click(force=True, timeout=1000)
                                                clicked_inside = True
                                                break
                                        except Exception:
                                            pass
                                    if not clicked_inside:
                                        # Click center of iframe from parent page
                                        page.mouse.move(cx, cy)
                                        page.wait_for_timeout(200)
                                        page.mouse.click(cx, cy)
                                    page.wait_for_timeout(1500)
                                    # Close any ad popup that opened from the first click
                                    dismiss_ad_overlays(page)
                                    try_close_ad_overlay(page)
                                    # Close new tabs that may have opened
                                    page.wait_for_timeout(500)
                                    # Second click — this one usually triggers play
                                    if not clicked_inside:
                                        page.mouse.click(cx, cy)
                                    else:
                                        for sel in ["video", ".play-overlay", "body"]:
                                            try:
                                                el = frame.locator(sel).first
                                                if el.is_visible(timeout=500):
                                                    el.click(force=True, timeout=1000)
                                                    break
                                            except Exception:
                                                pass
                                    page.wait_for_timeout(2000)
                                    # Try extracting get_video URL from iframe DOM
                                    try:
                                        gv_link = frame.evaluate("""() => {
                                            var sel = document.querySelector('a[href*="get_video"]');
                                            if (sel && sel.href) return sel.href;
                                            for (var id of ['videolink', 'ideoolink', 'robotlink', 'videolink2']) {
                                                var el = document.getElementById(id);
                                                if (!el) continue;
                                                var a = el.querySelector && el.querySelector('a[href*="get_video"]');
                                                if (a && a.href) return a.href;
                                                var text = (el.innerText || el.textContent || '').trim();
                                                if (text.indexOf('get_video') >= 0) {
                                                    var m = text.match(/https?:\\/\\/[^\\s"']+get_video[^\\s"']*/);
                                                    if (m) return m[0];
                                                }
                                            }
                                            var html = document.documentElement.innerHTML;
                                            var m = html.match(/https?:\\/\\/[^"\\s<>]+get_video[^"\\s<>]*/);
                                            return m ? m[0] : null;
                                        }""")
                                        if gv_link and isinstance(gv_link, str) and "get_video" in gv_link:
                                            stream_url_for_download[0] = gv_link
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                                page.wait_for_timeout(3000)
                    if not target_stream_seen_ref[0] and on_player_page:
                        auto_click_iters[0] += 1
                        if server_tab == "VOE" and not voe_click_loop_done_ref[0]:
                            # VOE: pattern — in loop do 2 clicks with 0.1s between them, then wait 2s; on each step check if stream appeared
                            voe_click_loop_done_ref[0] = True
                            _visual_log("auto_click_player: VOE — scroll up then 2-click pattern until stream appears (timeout ~60s)")
                            try:
                                page.evaluate("window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;")
                                page.wait_for_timeout(400)
                            except Exception:
                                pass
                            for attempt in range(30):  # ~30 * (2s + small overhead) ≈ 60 seconds
                                if target_stream_seen_ref[0] or stream_url_for_download[0]:
                                    _visual_log("auto_click_player: VOE — stream link available, breaking to start download")
                                    if stream_url_for_download[0] and _is_downloadable_stream_url(stream_url_for_download[0]) and not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                # keep overlays clean before each click burst
                                for _ in range(2):
                                    try_close_ad_overlay(page)
                                    page.wait_for_timeout(150)
                                # two clicks with 0.1s interval
                                for click_idx in range(2):
                                    if try_click_player(page):
                                        timeline("auto_click_player_voe")
                                        _visual_log(f"auto_click_player: VOE click burst #{attempt + 1} click {click_idx + 1}")
                                    page.wait_for_timeout(100)  # 0.1 sec between clicks
                                    if target_stream_seen_ref[0] or stream_url_for_download[0]:
                                        break
                                if target_stream_seen_ref[0] or stream_url_for_download[0]:
                                    _visual_log("auto_click_player: VOE — stream detected after click burst")
                                    if stream_url_for_download[0] and _is_downloadable_stream_url(stream_url_for_download[0]) and not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                # wait 2 seconds before next burst
                                page.wait_for_timeout(2_000)
                            # timeout: no stream/link within ~60 seconds — try TV once, then stop with error
                            if not target_stream_seen_ref[0] and not stream_url_for_download[0]:
                                if not voe_failed_try_tv_ref[0]:
                                    voe_failed_try_tv_ref[0] = True
                                    _visual_log("auto_click_player: VOE — timeout 60s, no stream; trying TV...")
                                    log("VOE: no stream within 60s, switching to TV...")
                                    dismiss_ad_overlays(page)
                                    page.wait_for_timeout(500)
                                    try:
                                        clicked_tv = page.evaluate("""() => {
                                            var adLike = /ads?\\b|popads|popcash|exoclick|propeller|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                                            var btns = document.querySelectorAll('a.btn-server');
                                            for (var i = 0; i < btns.length; i++) {
                                                var a = btns[i];
                                                if ((a.textContent || a.innerText || '').trim().toUpperCase() !== 'TV') continue;
                                                if (adLike.test((a.getAttribute('href') || '').trim())) continue;
                                                a.scrollIntoView({ block: 'center' });
                                                a.click();
                                                return true;
                                            }
                                            return false;
                                        }""")
                                        if clicked_tv:
                                            page.wait_for_timeout(3000)
                                            server_tab = "TV"
                                            auto_click_iters[0] = 5
                                        else:
                                            log("TV tab not found; stopping with error.")
                                            stop_event.set()
                                    except Exception as e:
                                        log(f"Failed to switch to TV: {e!r}; stopping.")
                                        stop_event.set()
                                else:
                                    _visual_log("auto_click_player: VOE — timeout 60s, no stream found, stopping with error")
                                    log("VOE: no stream detected within 60 seconds; stopping with error.")
                                    stop_event.set()
                        elif server_tab != "VOE" and not tv_click_loop_done_ref[0] and auto_click_iters[0] >= 5:
                            # TV / ST: same as VOE — loop with ~60s timeout; forbid navigation to other sites (go_back if left)
                            tv_click_loop_done_ref[0] = True
                            _visual_log(f"auto_click_player: {server_tab} — scroll + click pattern until stream (timeout ~60s)")
                            for attempt in range(4):  # ~15–20s per attempt → ~60s total
                                if target_stream_seen_ref[0] or stream_url_for_download[0]:
                                    _visual_log(f"auto_click_player: {server_tab} — stream link available")
                                    if stream_url_for_download[0] and _is_downloadable_stream_url(stream_url_for_download[0]) and not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                current_url = page.url
                                if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS) or not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                                    _visual_log(f"auto_click_player: {server_tab} — forbidden site, going back")
                                    try:
                                        page.go_back()
                                        page.wait_for_timeout(1500)
                                    except Exception:
                                        pass
                                    continue
                                try:
                                    page.evaluate("window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;")
                                    page.wait_for_timeout(400)
                                except Exception:
                                    pass
                                for _ in range(3):
                                    try_close_ad_overlay(page)
                                    page.wait_for_timeout(300)
                                if try_click_player(page):
                                    timeline("auto_click_player")
                                    _visual_log(f"auto_click_player: {server_tab} attempt {attempt + 1} click 1")
                                page.wait_for_timeout(500)
                                if server_tab in ("TV", "ST"):
                                    page.wait_for_timeout(10_000)
                                    current_url = page.url
                                    if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS) or not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                                        _visual_log(f"auto_click_player: {server_tab} — forbidden site after wait, going back")
                                        try:
                                            page.go_back()
                                            page.wait_for_timeout(1500)
                                        except Exception:
                                            pass
                                        continue
                                    if try_click_player(page):
                                        timeline(f"auto_click_player_second_{server_tab.lower()}")
                                        _visual_log(f"auto_click_player: {server_tab} attempt {attempt + 1} click 2")
                                    page.wait_for_timeout(2_000)
                                    current_url = page.url
                                    if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS) or not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                                        try:
                                            page.go_back()
                                            page.wait_for_timeout(1500)
                                        except Exception:
                                            pass
                                        continue
                                    if try_click_player(page):
                                        timeline(f"auto_click_player_third_{server_tab.lower()}")
                                        _visual_log(f"auto_click_player: {server_tab} attempt {attempt + 1} click 3")
                                    page.wait_for_timeout(500)
                                if target_stream_seen_ref[0] or stream_url_for_download[0]:
                                    if stream_url_for_download[0] and _is_downloadable_stream_url(stream_url_for_download[0]) and not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                if not target_stream_seen_ref[0]:
                                    if try_click_player(page):
                                        _visual_log("auto_click_player: extra click (stream not found)")
                                    page.wait_for_timeout(300)
                                    page.wait_for_timeout(5_000)
                                    try:
                                        btn = page.locator("#jav-download-trigger").first
                                        if btn.is_visible(timeout=1000):
                                            btn.click(force=True)
                                            download_url = stream_url_for_download[0]
                                            if download_url:
                                                _log_stream_url(download_url, "download_click")
                                                try:
                                                    LAST_DOWNLOAD_URL_FILE.write_text(download_url, encoding="utf-8")
                                                except Exception:
                                                    pass
                                    except Exception:
                                        pass
                            if not target_stream_seen_ref[0] and not stream_url_for_download[0]:
                                if server_tab == "TV" and not tv_failed_try_st_ref[0]:
                                    tv_failed_try_st_ref[0] = True
                                    _visual_log("auto_click_player: TV — timeout 60s, no stream; trying ST...")
                                    log("TV: no stream within 60s, switching to ST...")
                                    dismiss_ad_overlays(page)
                                    page.wait_for_timeout(500)
                                    try:
                                        clicked_st = page.evaluate("""() => {
                                            var adLike = /ads?\\b|popads|popcash|exoclick|propeller|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                                            var btns = document.querySelectorAll('a.btn-server');
                                            for (var i = 0; i < btns.length; i++) {
                                                var a = btns[i];
                                                if ((a.textContent || a.innerText || '').trim().toUpperCase() !== 'ST') continue;
                                                if (adLike.test((a.getAttribute('href') || '').trim())) continue;
                                                a.scrollIntoView({ block: 'center' });
                                                a.click();
                                                return true;
                                            }
                                            return false;
                                        }""")
                                        if clicked_st:
                                            page.wait_for_timeout(3000)
                                            server_tab = "ST"
                                            tv_click_loop_done_ref[0] = False
                                            auto_click_iters[0] = 5
                                        else:
                                            log("ST tab not found; stopping with error.")
                                            stop_event.set()
                                    except Exception as e:
                                        log(f"Failed to switch to ST: {e!r}; stopping.")
                                        stop_event.set()
                                else:
                                    _visual_log(f"auto_click_player: {server_tab} — timeout ~60s, no stream found, stopping with error")
                                    log(f"{server_tab}: no stream detected within ~60 seconds; stopping with error.")
                                    stop_event.set()
                except Exception as e:
                    _visual_log(f"loop_error: {e!r}")
                    if isinstance(e, _TargetClosedError) or "closed" in str(e).lower():
                        browser_closed_ref[0] = True
                        break
            if download_proc_ref:
                log("Browser closed. Waiting for download to finish...")
                try:
                    for proc in list(download_proc_ref):
                        proc.wait(timeout=3600)
                except Exception:
                    pass
                log("Done.")
            if download_thread_ref[0]:
                download_thread_ref[0].join(timeout=3700)
            visual_download_success[0] = download_finished_ref[0] in ("done", "stopped")
        finally:
            browser_closed_ref[0] = True
            if context is not None:
                try:
                    context.close()
                except BaseException:
                    pass
    return visual_download_success[0]


def _parse_ytdlp_progress(line: str) -> str | None:
    """Extract short progress string from yt-dlp stdout/stderr line. Returns None if not a progress line."""
    # [download]  45.2% of 120.00MiB at 2.50MiB/s ETA 00:25
    if "download" not in line.lower() and "MiB" not in line and "KiB" not in line and "ETA" not in line:
        return None
    m = re.search(r"(\d+\.?\d*)%\s*(?:of\s|\s|$)", line)
    if not m:
        return None
    pct = m.group(1)
    speed = ""
    eta = ""
    sm = re.search(r"at\s+([^\s]+)", line)
    if sm:
        speed = sm.group(1).strip()
    em = re.search(r"ETA\s+([^\s]+)", line)
    if em:
        eta = em.group(1).strip()
    if speed and eta:
        return f"{pct}% · {speed} · ETA {eta}"
    if speed:
        return f"{pct}% · {speed}"
    return f"{pct}%"


def resolve_streamtape_direct_url(embed_url: str, referer: str = "https://supjav.com/") -> str | None:
    """Open Streamtape embed page (/e/...), get direct video URL from DOM (get_video) or network, return it.
    yt-dlp does not support streamtape; we need the direct URL for generic download."""
    if "streamtape" not in embed_url.lower() or "/e/" not in embed_url:
        return None
    video_urls: list[str] = []
    get_video_urls: list[str] = []

    def on_response(response):
        url = response.url
        try:
            ct = (response.headers.get("content-type") or "").lower()
            if "video/" in ct or ("application/octet-stream" in ct and ".mp4" in url):
                video_urls.append(url)
            if "get_video" in url.lower() and url.startswith("http"):
                get_video_urls.append(url)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            context = new_stealth_context(browser, extra_http_headers={"Referer": referer})
            context.set_default_timeout(30_000)
            page = context.new_page()
            page.on("response", on_response)
            page.goto(embed_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5000)
            try:
                page.locator("video").first.click(force=True, timeout=5000)
            except Exception:
                try:
                    page.locator("body").first.click(force=True, timeout=2000)
                except Exception:
                    pass
            page.wait_for_timeout(8000)
            if video_urls:
                return video_urls[-1]
            if get_video_urls:
                return get_video_urls[-1]
            # Extract get_video from DOM: #videolink, #ideoolink, #robotlink or any a[href*="get_video"]
            try:
                link = page.evaluate("""() => {
                    var sel = document.querySelector('a[href*="get_video"]');
                    if (sel && sel.href) return sel.href;
                    for (var id of ['videolink', 'ideoolink', 'robotlink', 'videolink2']) {
                        var el = document.getElementById(id);
                        if (!el) continue;
                        var a = el.querySelector && el.querySelector('a[href*="get_video"]');
                        if (a && a.href) return a.href;
                        var text = (el.innerText || el.textContent || '').trim();
                        if (text.indexOf('get_video') >= 0) {
                            var m = text.match(/https?:\\/\\/[^\\s"']+get_video[^\\s"']*/);
                            if (m) return m[0];
                        }
                    }
                    var html = document.documentElement.innerHTML;
                    var m = html.match(/https?:\\/\\/[^"\\s<>]+get_video[^"\\s<>]*/);
                    return m ? m[0] : null;
                }""")
                if link and isinstance(link, str) and "get_video" in link:
                    return link
            except Exception:
                pass
            # Fallback: regex in page HTML
            try:
                html = page.content()
                m = re.search(r'https?://[^\s"\'<>]+get_video[^\s"\'<>]*', html)
                if m:
                    return m.group(0).rstrip("'\">,)")
            except Exception:
                pass
            return None
        finally:
            browser.close()


def _follow_redirect_to_video(url: str, referer: str = "https://streamtape.com/") -> str | None:
    """Follow redirects for get_video (or any) URL; return final URL (for streamtape CDN)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
        req.get_method = lambda: "HEAD"
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.geturl()
        except Exception:
            return None


def _download_direct_http(
    url: str,
    output_path: Path,
    referer: str,
    progress_callback: Callable[[str], None] | None = None,
    stopped_by_user: list | None = None,
) -> bool:
    """Download a direct video URL via HTTP (urllib), with progress reporting."""
    print(f"Downloading direct: {url[:100]}")
    sys.stdout.flush()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
        })
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 256 * 1024
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                while True:
                    if stopped_by_user and stopped_by_user[0]:
                        pass
                        return True
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        msg = f"{pct}% ({mb:.1f}/{total_mb:.1f} MB)"
                    else:
                        mb = downloaded / (1024 * 1024)
                        msg = f"{mb:.1f} MB downloaded"
                    if progress_callback:
                        try:
                            progress_callback(msg)
                        except Exception:
                            pass
                    if downloaded % (5 * 1024 * 1024) < chunk_size:
                        print(msg)
                        sys.stdout.flush()
        print(f"Download completed ({downloaded / (1024*1024):.1f} MB).")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"Download failed: {e}")
        sys.stdout.flush()
        return False


def download_video(
    url: str,
    output_path: str | Path,
    referer: str = "https://supjav.com/",
    progress_callback: Callable[[str], None] | None = None,
    out_proc: list | None = None,
    stopped_by_user: list | None = None,
) -> bool:
    """Download video from URL using yt-dlp. Returns True on success.
    If progress_callback is given, call it with progress string during download.
    If out_proc is a list, the Popen process is appended so caller can kill it to stop and save.
    If stopped_by_user is set by caller when killing, we return True (partial file saved)."""
    if "streamtape" in url.lower() and "get_video" in url.lower():
        if progress_callback:
            try:
                progress_callback("Resolving Streamtape get_video redirect...")
            except Exception:
                pass
        print(f"Streamtape get_video URL, resolving redirect...", file=sys.stderr)
        final = _follow_redirect_to_video(url, referer="https://streamtape.com/")
        if final and final != url:
            print(f"Resolved to CDN: {final[:120]}", file=sys.stderr)
            url = final
        else:
            print(f"Could not resolve get_video redirect, using as-is", file=sys.stderr)
    elif "streamtape.com/e/" in url or "streamtape.xyz/e/" in url:
        if progress_callback:
            try:
                progress_callback("Resolving Streamtape embed...")
            except Exception:
                pass
        direct = resolve_streamtape_direct_url(url, referer=referer)
        if direct:
            if "get_video" in direct.lower():
                final = _follow_redirect_to_video(direct, referer="https://streamtape.com/")
                if final:
                    direct = final
            url = direct
            print("Using direct Streamtape URL for download.", file=sys.stderr)
        else:
            print("Could not resolve Streamtape direct URL, trying original.", file=sys.stderr)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dl_referer = referer
    if "streamtape" in url.lower() or "tapecontent" in url.lower():
        dl_referer = "https://streamtape.com/"
    # Direct HTTP download for CDN URLs (tapecontent.net etc.) — yt-dlp hangs on these
    if "tapecontent" in url.lower() or (url.lower().endswith(".mp4") and "get_video" not in url.lower()):
        return _download_direct_http(url, output_path, dl_referer, progress_callback, stopped_by_user)
    if output_path.suffix:
        out_arg = str(output_path)
    else:
        out_arg = str(output_path.with_suffix("")) + ".%(ext)s"
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--newline",
        "--no-part",
        "--add-header", f"Referer:{dl_referer}",
        "--user-agent", USER_AGENT,
        "-o", out_arg,
        url,
    ]
    pass
    try:
        if progress_callback is not None:
            try:
                progress_callback("Downloading...")
            except Exception:
                pass
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            if out_proc is not None:
                out_proc.clear()
                out_proc.append(proc)
            assert proc.stdout is not None
            assert proc.stderr is not None

            def read_stderr():
                for line in proc.stderr:
                    print(line, end="", file=sys.stderr)

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            for line in proc.stdout:
                print(line, end="", file=sys.stderr)
                parsed = _parse_ytdlp_progress(line)
                if parsed:
                    try:
                        progress_callback(parsed)
                    except Exception:
                        pass
            stderr_thread.join(timeout=0.5)
            try:
                proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                print("Download failed (timeout)", file=sys.stderr)
                return False
            if stopped_by_user and stopped_by_user[0]:
                return True
            if proc.returncode != 0:
                print(f"Download failed (exit {proc.returncode})", file=sys.stderr)
                return False
            return True
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
        default="video.m4v",
        help="Output path for download (default: video.m4v); use e.g. CODE/CODE.m4v to save under download/CODE/",
    )
    parser.add_argument(
        "--visual",
        "-v",
        action="store_true",
        help="Open page in visible browser; log stream URLs when you click (Enter to close)",
    )
    parser.add_argument(
        "--no-auto-download",
        action="store_true",
        dest="no_auto_download",
        help="With --visual: do not start download automatically when target link appears",
    )
    parser.add_argument(
        "--server-tab",
        "-s",
        default="VOE",
        metavar="TAB",
        help="Server tab: VOE (default: try VOE then TV then ST), or ST, TV, FST",
    )
    args = parser.parse_args()

    if args.visual:
        ok = run_visual_mode(
            args.url,
            auto_download=not getattr(args, "no_auto_download", False),
            output_filename=args.output,
            server_tab=args.server_tab,
        )
        return 0 if ok else 1

    try:
        urls, video_code = extract_stream_urls(
            args.url,
            server_tabs=[args.server_tab],
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
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = DOWNLOAD_DIR / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if download_video(download_url, out_path, referer="https://supjav.com/"):
            print(f"Saved to: {out_path}", file=sys.stderr)
            return 0
        return 1

    for u in urls:
        print(u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
