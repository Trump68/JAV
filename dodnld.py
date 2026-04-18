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
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright

try:
    from playwright._impl._errors import TargetClosedError as _TargetClosedError
except ImportError:
    _TargetClosedError = type("TargetClosedError", (Exception,), {})

DEFAULT_URL = "https://supjav.com/403831.html"
PAGE_TIMEOUT_MS = 60_000
PLAYER_TIMEOUT_MS = 15_000
DEFAULT_DOWNLOAD_RETRIES = 20

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


def _persistent_profile_commandline_in_use(user_data_dir: Path) -> bool:
    """
    True if a Chrome/Chromium/Edge process appears to use this exact user-data-dir
    (typical conflict with launch_persistent_context).
    """
    try:
        resolved = user_data_dir.resolve()
    except OSError:
        resolved = user_data_dir
    needle_lo = str(resolved).lower()
    norm_slash = needle_lo.replace("\\", "/")
    if sys.platform == "win32":
        return _win_chrome_using_profile_path(needle_lo)
    return _posix_chrome_using_profile_path(needle_lo, norm_slash)


def _win_chrome_using_profile_path(needle_lo: str) -> bool:
    env = os.environ.copy()
    env["DODNLD_U"] = needle_lo
    ps = (
        "$n = $env:DODNLD_U; if (-not $n) { Write-Output FREE; exit 0 }; "
        "$alt = $n.Replace('\\','/'); "
        "$r = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { "
        "  $cl = $_.CommandLine; if (-not $cl) { return $false }; "
        "  $nm = $_.Name; if ($nm -notmatch '^(?i)(chrome|chromium|msedge)\\.exe$') { return $false }; "
        "  $t = $cl.ToLowerInvariant(); "
        "  return ($t.Contains($n) -or $t.Contains($alt)) "
        "}; "
        "if ($r) { Write-Output LOCKED } else { Write-Output FREE }"
    )
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (cp.stdout or "").strip().upper()
    return "LOCKED" in out


def _posix_chrome_using_profile_path(needle_lo: str, norm_slash: str) -> bool:
    for pat in ("chrome", "chromium", "Chromium"):
        try:
            cp = subprocess.run(
                ["pgrep", "-af", pat],
                capture_output=True,
                text=True,
                timeout=15,
                errors="replace",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        blob = ((cp.stdout or "") + "\n" + (cp.stderr or "")).lower()
        if needle_lo in blob or norm_slash in blob:
            return True
    return False


def _countdown_line_prefix(*, intro: str, label: str) -> str:
    """Short name shown on each countdown tick; explicit label wins, else first line of intro."""
    lab = (label or "").strip()
    if lab:
        return lab[:72]
    s = (intro or "").strip().split("\n")[0].rstrip(":").strip()
    if not s:
        return ""
    if len(s) > 72:
        return s[:69] + "…"
    return s


def _stderr_ts() -> str:
    """Time-of-day for stderr lines (HH:MM:SS.mmm), no calendar date."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _stderr_line(msg: str = "") -> None:
    """Print one full line to stderr with a timestamp prefix."""
    print(f"{_stderr_ts()}  {msg}", file=sys.stderr)
    sys.stderr.flush()


def _stderr_tick(msg: str) -> None:
    """Overwrite one stderr line (countdown) with timestamp + msg."""
    print(f"\r{_stderr_ts()}  {msg}   ", end="", file=sys.stderr)
    sys.stderr.flush()


def _stderr_sleep_countdown(total_s: float, intro: str, *, label: str = "") -> None:
    """Print intro on stderr, then countdown of remaining whole seconds on one line (\\r), then newline."""
    if total_s <= 0:
        return
    _stderr_line(intro)
    tick_name = _countdown_line_prefix(intro=intro, label=label)
    end = time.monotonic() + total_s
    last_shown: int | None = None
    while True:
        now = time.monotonic()
        if now >= end:
            break
        # Round remaining time up to whole seconds for display (30.0 → 30, 9.2 → 10).
        left = max(0, int((end - now) + 0.999999))
        if left != last_shown:
            last_shown = left
            if tick_name:
                _stderr_tick(f"[{tick_name}] {left}s…")
            else:
                _stderr_tick(f"{left}s…")
        time.sleep(min(1.0, end - now))
    _stderr_line("")


def _wait_until_persistent_profile_released(user_data_dir: Path, poll_seconds: float = 30.0) -> None:
    """If another process holds this profile, sleep poll_seconds and re-check until free."""
    while _persistent_profile_commandline_in_use(user_data_dir):
        intro = (
            f"Chrome/Chromium already using profile {user_data_dir}; "
            f"waiting {poll_seconds:.0f}s, countdown:"
        )
        _stderr_sleep_countdown(
            poll_seconds,
            intro,
            label="Chrome profile in use",
        )


# Countdown on stderr for waits >= this many seconds (or ms for Playwright page waits).
_COUNTDOWN_UI_MIN_SEC = 2.0
_COUNTDOWN_UI_MIN_MS = int(_COUNTDOWN_UI_MIN_SEC * 1000)


def page_wait_ms(
    page: Any,
    ms: float | int,
    intro: str = "",
    *,
    label: str = "",
    wall_deadline_monotonic: float | None = None,
) -> None:
    """Like page.wait_for_timeout(ms); for ms >= 2s prints a live countdown on stderr.

    If wall_deadline_monotonic is set (time.monotonic() instant), the wait ends at that
    deadline even if ms is larger, and the countdown line includes ``| cap Ns`` (seconds
    left until that deadline) for Streamtape-style overall caps.
    """
    wait_fn = getattr(page, "wait_for_timeout", None)
    if wait_fn is None:
        raise TypeError("page_wait_ms: object has no wait_for_timeout")
    total = int(float(ms))
    if total <= 0:
        return
    now0 = time.monotonic()
    if wall_deadline_monotonic is not None and now0 >= wall_deadline_monotonic:
        return
    if total < _COUNTDOWN_UI_MIN_MS:
        eff = total
        if wall_deadline_monotonic is not None:
            rem_ms = int((wall_deadline_monotonic - time.monotonic()) * 1000)
            if rem_ms <= 0:
                return
            eff = min(total, rem_ms)
        if eff > 0:
            wait_fn(eff)
        return
    if intro:
        _stderr_line(intro)
    tick_name = _countdown_line_prefix(intro=intro, label=label)
    end = time.monotonic() + total / 1000.0
    if wall_deadline_monotonic is not None:
        end = min(end, wall_deadline_monotonic)
    last_chunk: int | None = None
    last_cap: int | None = None
    while True:
        now = time.monotonic()
        if now >= end:
            break
        left = max(0, int((end - now) + 0.999999))
        cap_left: int | None = None
        if wall_deadline_monotonic is not None:
            cap_left = max(0, int((wall_deadline_monotonic - now) + 0.999999))
        if left != last_chunk or cap_left != last_cap:
            last_chunk = left
            last_cap = cap_left
            cap_seg = f" | cap {cap_left}s" if cap_left is not None else ""
            if tick_name:
                _stderr_tick(f"[{tick_name}] step {left}s{cap_seg}…")
            else:
                _stderr_tick(f"step {left}s{cap_seg}…")
        step_ms = min(1000, max(25, int((end - now) * 1000)))
        if wall_deadline_monotonic is not None:
            step_ms = min(step_ms, int(max(0, wall_deadline_monotonic - now) * 1000))
        if step_ms <= 0:
            break
        wait_fn(step_ms)
    _stderr_line("")


def _event_wait_with_countdown(
    ev: threading.Event, timeout_s: float, intro: str = "", *, label: str = ""
) -> bool:
    """threading.Event.wait with optional intro + countdown on stderr for timeout_s >= 2s."""
    if timeout_s <= 0:
        return ev.is_set()
    if timeout_s < _COUNTDOWN_UI_MIN_SEC:
        return ev.wait(timeout=timeout_s)
    if intro:
        _stderr_line(intro)
    tick_name = _countdown_line_prefix(intro=intro, label=label)
    end = time.monotonic() + timeout_s
    last_shown: int | None = None
    while time.monotonic() < end:
        if ev.is_set():
            _stderr_line("")
            return True
        rem = end - time.monotonic()
        left = max(0, int(rem + 0.999999))
        if left != last_shown:
            last_shown = left
            if tick_name:
                _stderr_tick(f"[{tick_name}] {left}s…")
            else:
                _stderr_tick(f"{left}s…")
        chunk = min(1.0, rem)
        if ev.wait(timeout=chunk):
            _stderr_line("")
            return True
    return False


def _proc_wait_with_countdown(
    proc: Any, timeout_s: float, intro: str = "", *, label: str = ""
) -> int | None:
    """subprocess Popen.wait with countdown on stderr (timeout_s >= 2s). Raises TimeoutExpired on expiry."""
    if timeout_s < _COUNTDOWN_UI_MIN_SEC:
        return proc.wait(timeout=timeout_s)
    if intro:
        _stderr_line(intro)
    tick_name = _countdown_line_prefix(intro=intro, label=label)
    end = time.monotonic() + timeout_s
    last_shown: int | None = None
    while time.monotonic() < end:
        code = proc.poll()
        if code is not None:
            _stderr_line("")
            return code
        rem = end - time.monotonic()
        left = max(0, int(rem + 0.999999))
        if left != last_shown:
            last_shown = left
            if tick_name:
                _stderr_tick(f"[{tick_name}] {left}s…")
            else:
                _stderr_tick(f"{left}s…")
        time.sleep(min(0.5, rem))
    _stderr_line("")
    raise subprocess.TimeoutExpired(proc.args, timeout_s)


def _thread_join_with_countdown(
    thread: threading.Thread, timeout_s: float, intro: str = "", *, label: str = ""
) -> None:
    """thread.join split into chunks with countdown for timeout_s >= 2s."""
    if timeout_s <= 0:
        thread.join(timeout=0)
        return
    if timeout_s < _COUNTDOWN_UI_MIN_SEC:
        thread.join(timeout=timeout_s)
        return
    if intro:
        _stderr_line(intro)
    tick_name = _countdown_line_prefix(intro=intro, label=label)
    end = time.monotonic() + timeout_s
    last_shown: int | None = None
    while time.monotonic() < end:
        if not thread.is_alive():
            _stderr_line("")
            return
        rem = end - time.monotonic()
        left = max(0, int(rem + 0.999999))
        if left != last_shown:
            last_shown = left
            if tick_name:
                _stderr_tick(f"[{tick_name}] {left}s…")
            else:
                _stderr_tick(f"{left}s…")
        thread.join(timeout=min(1.0, rem))


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
    if any(
        x in lower
        for x in (
            "turbovidhls.com/t/",
            "supremejav.com/supjav",
            "turboviplay.com",
            "turbosplayer.com",
            "doppiocdn.com",
            "streamtape",
            "streamta.pe",
            "strtape.",
        )
    ):
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
            page_wait_ms(page,500)
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
            page_wait_ms(page,150)
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
    "streamta.pe",
    "strtape.",
    # FST tab embeds (fc2 / generic players) — must match for try_click_player / bursts
    "fc2stream",
    "fc2.to",
    "javbee",
    "lysande",
    "beatbangers",
    "vidhide",
    "embedrise",
    "d0000d",
    "d0011d",
    "dood",
    "ds2cd",
    "ds2play",
    "streamlare",
    "vide0.net",
    "abysscdn",
)

# Weak hints: large http iframes whose src looks like a player embed (FST fallback if not in list above)
_FST_IFRAME_SRC_WEAK_HINTS = (
    "/embed/",
    "/player/",
    "/video/",
    "embed.php",
    "player.php",
    "watch?",
    "/e/",
)


def _frame_is_player_iframe(frame) -> bool:
    """True if frame is an iframe with player src or current URL matching player domains."""
    if frame == frame.page.main_frame:
        return False
    try:
        furl = (frame.url or "").lower()
        if any(s in furl for s in PLAYER_IFRAME_SRC_SUBSTRINGS):
            return True
        el = frame.frame_element()
        src = (el.get_attribute("src") or "").lower()
        return any(s in src for s in PLAYER_IFRAME_SRC_SUBSTRINGS)
    except Exception:
        return False


def _iframe_src_blocked_ad(src: str) -> bool:
    s = (src or "").lower()
    return any(
        x in s
        for x in (
            "doubleclick",
            "googlesyndication",
            "googleads",
            "adservice",
            "popads",
            "exoclick",
            "propeller",
            "adsterra",
            "hilltopads",
            "bluetraffic",
            "smartpop",
        )
    )


def _collect_fst_fallback_iframes(page) -> list[tuple[Any, float]]:
    """Largest http iframes that look like player embeds but missed PLAYER_IFRAME_SRC_SUBSTRINGS."""
    out: list[tuple[Any, float]] = []
    try:
        for iframe_el in page.query_selector_all("iframe[src]"):
            try:
                src = (iframe_el.get_attribute("src") or "").strip()
                if not src.startswith("http"):
                    continue
                if _iframe_src_blocked_ad(src):
                    continue
                sl = src.lower()
                if not any(h in sl for h in _FST_IFRAME_SRC_WEAK_HINTS):
                    continue
                box = iframe_el.bounding_box()
                if not box or box.get("width", 0) < 200 or box.get("height", 0) < 120:
                    continue
                out.append((iframe_el, box["width"] * box["height"]))
            except Exception:
                continue
        out.sort(key=lambda x: -x[1])
    except Exception:
        pass
    return out


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
            # FST: extra embeds that only match weak URL patterns
            for iframe_el, _ in _collect_fst_fallback_iframes(page):
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


def _auto_click_player_for_tab(page, server_tab: str) -> bool:
    """FST: burst center clicks first (many embeds need it); other tabs: single careful click."""
    if server_tab == "FST":
        return bool(try_click_fst_player_burst(page) or try_click_player(page))
    return bool(try_click_player(page))


def try_click_fst_player_burst(
    page,
    *,
    clicks: int = 6,
    gap_ms: int = 90,
) -> bool:
    """
    FST: several fast clicks on the player (video or iframe center), like VOE double-tap.
    Many FST embeds only start HLS after multiple center hits.
    """
    try:
        try:
            page.wait_for_selector("iframe[src^='http']", timeout=2500)
        except Exception:
            pass
        # 1) <video> inside known player frames — burst clicks
        for frame in page.frames:
            if not _frame_is_player_iframe(frame):
                continue
            try:
                video = frame.locator("video").first
                if video.is_visible(timeout=700):
                    for i in range(clicks):
                        video.click(force=True, timeout=700)
                        if i + 1 < clicks:
                            page_wait_ms(page,gap_ms)
                    return True
            except Exception:
                pass
        # 2) Largest matching iframe center — burst
        player_iframes: list = []
        for iframe_el in page.query_selector_all("iframe"):
            try:
                src = (iframe_el.get_attribute("src") or "").lower()
                if not any(s in src for s in PLAYER_IFRAME_SRC_SUBSTRINGS):
                    continue
                if _iframe_src_blocked_ad(src):
                    continue
                box = iframe_el.bounding_box()
                if not box or box.get("width", 0) < 180 or box.get("height", 0) < 120:
                    continue
                player_iframes.append((iframe_el, box["width"] * box["height"]))
            except Exception:
                continue
        player_iframes.sort(key=lambda x: -x[1])
        for iframe_el, _ in player_iframes[:1]:
            try:
                box = iframe_el.bounding_box()
                if not box:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                for i in range(clicks):
                    page.mouse.click(cx, cy)
                    if i + 1 < clicks:
                        page_wait_ms(page,gap_ms)
                return True
            except Exception:
                pass
        for iframe_el, _ in _collect_fst_fallback_iframes(page)[:1]:
            try:
                box = iframe_el.bounding_box()
                if not box:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                for i in range(clicks):
                    page.mouse.click(cx, cy)
                    if i + 1 < clicks:
                        page_wait_ms(page,gap_ms)
                return True
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
                        page_wait_ms(page,300)
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
    page_wait_ms(page,500)
    # Click away chat ad "OK" buttons and any remaining close (Playwright by text)
    for text in ["OK", "Close", "×", "Skip"]:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first
            if btn.is_visible(timeout=400):
                btn.click(force=True, timeout=400)
                page_wait_ms(page,200)
        except Exception:
            pass
    try:
        page.locator('button:has-text("OK")').first.click(force=True, timeout=400)
        page_wait_ms(page,200)
    except Exception:
        pass


def extract_stream_urls(page_url: str, server_tabs: list[str] | None = None, for_download: bool = False) -> list[str]:
    """Extract stream URLs: clicks each requested SERVER tab (VOE, ST, DS, comma-list, …) and collects network/DOM URLs."""
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
            page_wait_ms(page,1500)
            dismiss_ad_overlays(page)
            click_element_containing_antidebug_script(page)

            base = page_url.rsplit("/", 1)[0] + "/"

            tab_labels = _normalize_extract_stream_tab_labels(server_tabs)
            for label in tab_labels:
                try:
                    # Try link by text, or any clickable with this text
                    tab = page.get_by_role("link", name=re.compile(label, re.I)).first
                    if not tab.is_visible(timeout=1000):
                        tab = page.locator(f"a:has-text('{label}')").first
                    if not tab.is_visible(timeout=1000):
                        tab = page.locator(f"*:has-text('{label}')").first
                    if tab.is_visible(timeout=1000):
                        tab.click()
                        page_wait_ms(page,2000)
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
                                page_wait_ms(page,1500)
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
                                        page_wait_ms(page,wait_after_tab_ms)
                            except Exception:
                                pass
                        elif label == "ST":
                            # ST (Streamtape + mirrors streamta.pe / strtape.*): iframe src may not contain "streamtape"
                            try:
                                page.wait_for_selector(
                                    "iframe[src*='streamtape'], iframe[src*='streamta.'], iframe[src*='strtape'], iframe[src^='http']",
                                    timeout=12_000,
                                )
                                page_wait_ms(page,1500)
                                iframe_el = (
                                    page.query_selector("iframe[src*='streamtape']")
                                    or page.query_selector("iframe[src*='streamta.']")
                                    or page.query_selector("iframe[src*='strtape']")
                                    or page.query_selector("iframe[src^='http']")
                                )
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
                                    page_wait_ms(page,wait_after_tab_ms)
                            except Exception:
                                pass
                        elif label == "DS":
                            # DoodStream / dood embed (Supjav tab DS)
                            try:
                                page.wait_for_selector(
                                    "iframe[src*='dood'], iframe[src*='d0011d'], iframe[src*='d0000d'], "
                                    "iframe[src^='http']",
                                    timeout=12_000,
                                )
                                page_wait_ms(page,1500)
                                iframe_el = (
                                    page.query_selector("iframe[src*='dood']")
                                    or page.query_selector("iframe[src*='d0011d']")
                                    or page.query_selector("iframe[src*='d0000d']")
                                    or page.query_selector("iframe[src^='http']")
                                )
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
                                                    page.mouse.click(
                                                        box["x"] + box["width"] / 2,
                                                        box["y"] + box["height"] / 2,
                                                    )
                                    page_wait_ms(page,wait_after_tab_ms)
                            except Exception:
                                pass
                        page_wait_ms(page,wait_after_tab_ms)
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
    get_video = [u for u in candidates if _is_streamtape_like_url(u) and "get_video" in u.lower()]
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
            page_wait_ms(page, 2000, intro="HLS probe: after player load (2s):")
            # Click in player to start playback (video or body)
            try:
                page.locator("video").first.click(force=True, timeout=5000)
            except Exception:
                try:
                    page.locator("body").first.click(force=True, timeout=2000)
                except Exception:
                    pass
            page_wait_ms(page, 10_000, intro="HLS probe: wait for m3u8 after play (10s):")
            # Prefer master playlist (no segment params)
            m3u8_urls = [u for u in sorted(collected) if ".m3u8" in u]
            for u in m3u8_urls:
                if "_HLS_msn" not in u and "_HLS_part" not in u:
                    return u
            return m3u8_urls[0] if m3u8_urls else None
        finally:
            browser.close()


CLOUDFLARE_WAIT_MS = 12_500  # wait for Cloudflare "Verifying you are human" to pass (half of previous 25s)

# Visual mode: max wall-clock wait on FST for a *downloadable* stream before fallback / stop.
FST_TAB_MAX_WAIT_S = 120.0
# Visual mode: max wall time for Streamtape embed click loop (get_video / direct URL).
ST_STREAMTAPE_RESOLVE_MAX_S = 80.0


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
# Diagnostics: why SERVER tab (e.g. DS) did not activate — append-only analysis log.
SERVER_TAB_DEBUG_LOG = Path(__file__).resolve().parent / ".server_tab_debug.log"


def _flush_log_to_disk(f) -> None:
    """Flush Python buffer and sync to storage (best-effort; immediate visibility for tail/readers)."""
    try:
        f.flush()
        fd = f.fileno()
        if fd >= 0:
            os.fsync(fd)
    except Exception:
        pass


def _append_server_tab_debug(msg: str) -> None:
    """Append timestamped line to SERVER_TAB_DEBUG_LOG (for post-run DS / server-tab analysis)."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(SERVER_TAB_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{msg}\n")
            _flush_log_to_disk(f)
    except Exception:
        pass


def _server_tab_click_diag_js(label: str) -> str:
    """Returns JS that JSON.stringify's a.btn-server scan for label match + ad-regex rejection (same rules as click)."""
    esc = (label or "").replace("\\", "\\\\").replace("'", "\\'")
    return f"""() => {{
  var adLike = /ads?\\b|popads|popcash|exoclick|propeller|dillinger|cactushead|juicyads|trafficjunky|revcontent|taboola|outbrain|mgid\\.com|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
  function tabTxtNorm(s) {{
    return (s || '').replace(/\\s+/g, ' ').trim().toUpperCase();
  }}
  var labelU = '{esc}'.toUpperCase();
  var o = {{
    label: '{esc}',
    location: String(window.location.href || ''),
    btnServerCount: 0,
    labelMatches: [],
    wouldClickSkippedByAd: []
  }};
  var btns = document.querySelectorAll('a.btn-server');
  o.btnServerCount = btns.length;
  for (var i = 0; i < btns.length; i++) {{
    var a = btns[i];
    var raw = (a.textContent || a.innerText || '').trim();
    var tnorm = tabTxtNorm(raw);
    var h = (a.getAttribute('href') || '').trim();
    var ad = adLike.test(h);
    if (tnorm === labelU) {{
      o.labelMatches.push({{ i: i, text: raw, href: h.substring(0, 400), adRejected: ad }});
      if (ad) o.wouldClickSkippedByAd.push({{ i: i, href: h.substring(0, 400) }});
    }}
  }}
  return JSON.stringify(o);
}}"""


def _log_server_tab_dom_diag(page: Any, label: str, note: str) -> None:
    """Evaluate diagnostic JS on main frame and each child frame; append JSON lines to SERVER_TAB_DEBUG_LOG."""
    js = _server_tab_click_diag_js(label)

    def _one(ctx: Any, fname: str) -> None:
        try:
            raw = ctx.evaluate(js)
            _append_server_tab_debug(f"dom_diag {note} ctx={fname} {raw}")
        except Exception as e:
            _append_server_tab_debug(f"dom_diag {note} ctx={fname} evaluate_error={e!r}")

    _one(page, "main")
    for fi, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        try:
            fu = (frame.url or "")[:200]
        except Exception:
            fu = "?"
        _one(frame, f"frame[{fi}] {fu}")


# Set for CLI download progress lines: e.g. "kijima-airi: JUX-203 (FST) " + bar/percent…
_CONSOLE_PROGRESS_PREFIX: str = ""


def _build_console_progress_prefix(slug: str, code: str, tab: str) -> str:
    slug, code, tab = slug.strip(), code.strip(), tab.strip()
    if slug:
        if code and tab:
            return f"{slug}: {code} ({tab}) "
        if code:
            return f"{slug}: {code} "
        if tab:
            return f"{slug}: ({tab}) "
        return f"{slug}: "
    if code and tab:
        return f"{code} ({tab}) "
    if code:
        return f"{code} "
    if tab:
        return f"({tab}) "
    return ""


def _progress_tab_from_download_url(download_url: str, fallback: str) -> str:
    """Best-effort tab label for progress line: VOE/ST from URL hints, else requested tab (TV, FST, …)."""
    u = download_url.lower()
    fb = (fallback or "").strip().upper() or "VOE"
    if "supremejav" in u or "turbovidhls.com" in u:
        return "VOE"
    if (
        "streamtape" in u
        or "streamta." in u
        or "strtape" in u
        or "tapecontent" in u
        or "get_video" in u
    ):
        return "ST"
    if any(m in u for m in ("doodstream", "dood.", "d0011d", "d0000d", "ds2cd", "ds2play")):
        return "DS"
    return fb


def _movie_code_for_progress_line(output_filename: str, video_code: str | None) -> str:
    if video_code:
        return str(video_code).strip().upper()
    m = re.search(r"\b([A-Z]{2,5}-\d{3,5})\b", Path(output_filename).name, re.I)
    return m.group(1).upper() if m else ""


def _progress_slug_from_output(output_filename: str) -> str:
    parts = Path(output_filename).parts
    if len(parts) >= 3:
        return parts[0]
    return ""


def _set_console_progress_prefix_for_download(
    output_filename: str,
    download_url: str,
    tab_fallback: str,
    video_code: str | None,
    *,
    progress_slug: str = "",
    progress_code: str = "",
    progress_tab: str = "",
) -> None:
    global _CONSOLE_PROGRESS_PREFIX
    slug = (progress_slug or "").strip()
    if not slug:
        slug = _progress_slug_from_output(output_filename)
    code = (progress_code or "").strip().upper()
    if not code:
        code = _movie_code_for_progress_line(output_filename, video_code)
    tab = (progress_tab or "").strip().upper()
    if not tab:
        tab = str(tab_fallback or "VOE").split(",")[0].strip().upper() or "VOE"
    tab = _progress_tab_from_download_url(download_url, tab)
    _CONSOLE_PROGRESS_PREFIX = _build_console_progress_prefix(slug, code, tab)


def _clear_console_progress_prefix() -> None:
    global _CONSOLE_PROGRESS_PREFIX
    _CONSOLE_PROGRESS_PREFIX = ""


def _prefixed_console_progress_line(line: str) -> str:
    if not _CONSOLE_PROGRESS_PREFIX:
        return line
    return _CONSOLE_PROGRESS_PREFIX + line

# Target stream URL pattern: all substrings must be present (query params may vary between runs)
# (Legacy one-page fingerprint; see _is_target_stream_match below for dynamic match.)
TARGET_STREAM_URL_PARTS = (
    "edgeon-bandwidth.com",
    "1im9wjkozr96",
    "index-v1-a1.m3u8",
)

# HLS from these hosts is the real Supjav player/CDN — not preroll/ad networks.
_TRUSTED_STREAM_CDN_MARKERS: tuple[str, ...] = (
    "doppiocdn.com",
    "edgeon-bandwidth.com",
    "turbovidhls.com",
    "turboviplay.com",
    "turbosplayer.com",
    "supremejav.com",
    "dianaavoidthey",
    "voe.sx",
    "guardianagainstyou",
    "streamtape.com",
    "streamtape.xyz",
    "streamta.pe",
    "strtape.",
    "tapecontent.net",
    "tapewithadblock",
    "get_video",
    "supjav.com@",
    ".urlset/",
    "master.txt",
    "fc2stream",
    # DoodStream (Supjav tab "DS")
    "doodstream",
    "dood.",
    "d0011d",
    "d0000d",
    "ds2cd",
    "ds2play",
)

# Streamtape embed/CDN mirrors (Supjav may use streamta.pe / strtape.* — not matched by "streamtape" in url).
_STREAMTAPE_URL_MARKERS: tuple[str, ...] = (
    "streamtape.com",
    "streamtape.xyz",
    "streamta.pe",
    "strtape.",
    "tapecontent.net",
    "tapewithadblock",
)


def _is_streamtape_like_url(url: str) -> bool:
    """Streamtape and common mirrors (HLS/get_video may live on these hosts)."""
    if not url:
        return False
    lower = url.lower()
    return any(m in lower for m in _STREAMTAPE_URL_MARKERS)


def _is_trusted_stream_cdn(url: str) -> bool:
    lower = url.lower()
    return any(m in lower for m in _TRUSTED_STREAM_CDN_MARKERS)


def _server_tab_button_visible(page, label: str) -> bool:
    """True if a visible a.btn-server with this tab label exists (main or child frame)."""
    u = (label or "").strip().upper()
    if not u:
        return False
    for ctx in (page, *page.frames):
        try:
            for btn in ctx.locator("a.btn-server").all():
                try:
                    txt = (btn.inner_text() or "").strip().upper()
                    if txt == u and btn.is_visible(timeout=300):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
    return False


def _ds_server_tab_visible(page) -> bool:
    """True if Supjav shows a SERVER tab button labeled DS."""
    return _server_tab_button_visible(page, "DS")


def _fst_server_tab_visible(page) -> bool:
    """True if a visible SERVER tab button labeled FST exists (main or child frame)."""
    return _server_tab_button_visible(page, "FST")


def _fallback_candidate_tabs_after_current(tab_order: list[str], page, current: str) -> list[str]:
    """When leaving `current`: tabs after it in user -s order, then tabs before (visible a.btn-server only)."""
    order = [str(t).strip().upper() for t in tab_order if str(t).strip()]
    cur = (current or "").strip().upper()
    if not cur:
        return []
    seen: set[str] = set()
    out: list[str] = []
    try:
        i_cur = order.index(cur)
    except ValueError:
        i_cur = -1
    after = order[i_cur + 1 :] if i_cur >= 0 else list(order)
    for t in after:
        if t == cur or t in seen:
            continue
        if _server_tab_button_visible(page, t):
            out.append(t)
            seen.add(t)
    before = order[:i_cur] if i_cur >= 0 else []
    for t in before:
        if t == cur or t in seen:
            continue
        if _server_tab_button_visible(page, t):
            out.append(t)
            seen.add(t)
    return out


def _st_fallback_candidate_tabs(tab_order: list[str], page) -> list[str]:
    """When leaving ST: tabs listed after ST in user order, then tabs before ST."""
    return _fallback_candidate_tabs_after_current(tab_order, page, "ST")


def _maybe_adjust_tab_list_for_ds(
    page,
    tabs_to_try: list[str],
    *,
    user_picked_single_tab: bool,
) -> list[str]:
    """
    If DS is not on the page, drop it from the plan (unless user forced single-tab DS).
    If DS exists and was not listed, insert it once (after FST when FST leads, else at front)
    for multi-tab / default orders.
    """
    raw_in = [str(t).strip().upper() for t in tabs_to_try if str(t).strip()]
    out = list(raw_in)
    ds_visible = _ds_server_tab_visible(page)
    only_ds = user_picked_single_tab and out == ["DS"]
    try:
        purl = page.url
    except Exception:
        purl = "?"
    if "DS" in out and not ds_visible and not only_ds:
        out = [t for t in out if t != "DS"]
    elif ds_visible and "DS" not in out and not user_picked_single_tab:
        if out and out[0] == "FST":
            out.insert(1, "DS")
        else:
            out.insert(0, "DS")
    _append_server_tab_debug(
        f"_maybe_adjust_tab_list_for_ds: page_url={purl!r} raw_in={raw_in!r} out={out!r} "
        f"ds_visible={ds_visible} only_ds={only_ds} user_picked_single_tab={user_picked_single_tab}"
    )
    return out


def _normalize_extract_stream_tab_labels(server_tabs: list[str] | None) -> list[str]:
    """Split comma-separated entries so extract_stream_urls can click each tab."""
    raw = server_tabs if server_tabs is not None else ["VOE"]
    labels: list[str] = []
    for entry in raw:
        s = (entry or "").strip()
        if not s:
            continue
        if "," in s:
            labels.extend([p.strip().upper() for p in s.split(",") if p.strip()])
        else:
            labels.append(s.upper())
    return labels if labels else ["VOE"]


def _extract_video_code_from_title(title: str) -> str | None:
    if not title:
        return None
    m = re.search(r"\b([A-Z]{2,5}-\d{3,5})\b", title, re.I)
    return m.group(1).lower() if m else None


def _visual_log(msg: str, log_file: Path | None = None) -> None:
    """Append timestamped lines to .visual_mode.log for post-run analysis."""
    if log_file is None:
        log_file = VISUAL_LOG_FILE
    try:
        from datetime import datetime

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{msg}\n")
            _flush_log_to_disk(f)
    except Exception:
        pass


def _log_stream_url(url: str, source: str = "capture") -> None:
    """Append captured stream URLs to stream_urls.log."""
    from datetime import datetime

    if not url or not url.strip():
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts}\t{source}\t{url.strip()}\n"
    try:
        with open(STREAM_URLS_LOG, "a", encoding="utf-8") as f:
            f.write(line)
            _flush_log_to_disk(f)
    except Exception:
        pass
    return


def _is_downloadable_stream_url(url: str | None) -> bool:
    """True if URL can be used for download (yt-dlp etc). Only actual video/stream URLs, not JS/GIF/CSS."""
    if not url:
        return False
    lower = url.lower()
    blocked = (
        "mc.yandex", "yandex.ru/watch", "yandex.com/watch", "google-analytics",
        "googletagmanager", "doubleclick", "adservice", "tracking", "pixel",
    )
    if any(b in lower for b in blocked):
        return False
    if _is_streamtape_like_url(url) and "/e/" in lower and "get_video" not in lower:
        return False
    path_part = lower.split("?")[0]
    _junk_exts = (".js", ".css", ".gif", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".woff", ".woff2", ".json", ".ico")
    if any(path_part.endswith(ext) for ext in _junk_exts):
        return False
    if ".m3u8" in lower or ".mp4" in lower or ".ts" in lower or "get_video" in lower or "tapecontent" in lower:
        return True
    if "edgeon-bandwidth" in lower or "urlset" in lower:
        return True
    return False


def _find_supjav_player_iframe_pair(page: Any, tab: str):
    """ST: Streamtape/supremejav iframe; DS: DoodStream/supremejav. Returns (content_frame, fel_or_none)."""
    pair = None
    if tab == "ST":
        try:
            for sel in (
                "iframe[src*='streamtape']",
                "iframe[src*='streamtape.']",
                "iframe[src*='streamta.']",
                "iframe[src*='strtape']",
                "iframe[src*='supremejav']",
            ):
                loc = page.locator(sel).first
                if loc.is_visible(timeout=600):
                    cf = loc.content_frame()
                    if cf:
                        pair = (cf, loc)
                        break
        except Exception:
            pass
        if not pair:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    furl = (frame.url or "").lower()
                    if _is_streamtape_like_url(furl) or "supremejav" in furl:
                        try:
                            fel = frame.frame_element()
                        except Exception:
                            fel = None
                        pair = (frame, fel)
                        break
                except Exception:
                    pass
        return pair
    # DS — DoodStream mirrors + supremejav wrapper
    try:
        for sel in (
            "iframe[src*='dood']",
            "iframe[src*='d0011d']",
            "iframe[src*='d0000d']",
            "iframe[src*='ds2cd']",
            "iframe[src*='ds2play']",
            "iframe[src*='doodstream']",
            "iframe[src*='supremejav']",
        ):
            loc = page.locator(sel).first
            if loc.is_visible(timeout=600):
                cf = loc.content_frame()
                if cf:
                    pair = (cf, loc)
                    break
    except Exception:
        pass
    if not pair:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                furl = (frame.url or "").lower()
                if any(
                    m in furl
                    for m in (
                        "doodstream",
                        "dood.",
                        "d0011d",
                        "d0000d",
                        "ds2cd",
                        "ds2play",
                    )
                ) or "supremejav" in furl:
                    try:
                        fel = frame.frame_element()
                    except Exception:
                        fel = None
                    pair = (frame, fel)
                    break
            except Exception:
                pass
    return pair


def _page_has_turnstile(page: Any) -> bool:
    """True if Cloudflare Turnstile challenge is active in any frame (DS/Dood anti-bot block)."""
    try:
        for frame in page.frames:
            furl = (frame.url or "").lower()
            if "challenges.cloudflare.com/turnstile" in furl:
                return True
    except Exception:
        pass
    try:
        result = page.evaluate("""() => {
            var all = document.querySelectorAll('iframe');
            for (var i = 0; i < all.length; i++) {
                var src = (all[i].src || all[i].getAttribute('src') || '').toLowerCase();
                if (src.indexOf('challenges.cloudflare.com/turnstile') >= 0) return true;
            }
            return false;
        }""")
        if result:
            return True
    except Exception:
        pass
    return False


def _turnstile_already_solved(page: Any) -> bool:
    """True if cf-turnstile-response token is already filled (Turnstile passed automatically)."""
    try:
        result = page.evaluate("""() => {
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 10) return true;
            var all = document.querySelectorAll('input');
            for (var i = 0; i < all.length; i++) {
                if ((all[i].name || '').toLowerCase().indexOf('turnstile') >= 0 &&
                    all[i].value && all[i].value.length > 10) return true;
            }
            return false;
        }""")
        if result:
            return True
    except Exception:
        pass
    return False


def _try_click_turnstile_checkbox(page: Any) -> bool:
    """Try to click the Turnstile widget checkbox inside the challenge iframe. Returns True if clicked."""
    try:
        for frame in page.frames:
            furl = (frame.url or "").lower()
            if "challenges.cloudflare.com/turnstile" not in furl:
                continue
            for sel in (
                "input[type='checkbox']",
                ".ctp-checkbox-label",
                "[role='checkbox']",
                ".ctp-checkbox",
                "label",
            ):
                try:
                    el = frame.locator(sel).first
                    if el.is_visible(timeout=800):
                        el.click(force=True, timeout=1500)
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


def _wait_for_turnstile_solve(
    page: Any,
    *,
    emit: Callable[..., None],
    max_wait_s: float = 18.0,
) -> bool:
    """
    Wait up to max_wait_s for Turnstile to self-resolve (invisible/managed mode).
    After ~half the time, try clicking the checkbox (interactive mode).
    Returns True if solved (token present or Turnstile gone), False if timed out.
    """
    emit("DS: Cloudflare Turnstile detected — waiting for auto-resolve...")
    deadline = time.monotonic() + max_wait_s
    clicked = False
    check_interval = 1.5

    while time.monotonic() < deadline:
        # 1. Token filled → solved
        if _turnstile_already_solved(page):
            emit("DS: Turnstile auto-resolved (token found) — resuming.")
            return True
        # 2. Turnstile frame gone → solved (invisible mode passed)
        if not _page_has_turnstile(page):
            emit("DS: Turnstile frame gone — resuming.")
            return True
        # 3. After half the budget, try clicking the checkbox
        remaining = deadline - time.monotonic()
        if not clicked and remaining < max_wait_s / 2:
            emit("DS: Turnstile not auto-resolved — trying checkbox click...")
            clicked = _try_click_turnstile_checkbox(page)
            if clicked:
                emit("DS: Turnstile checkbox clicked — waiting for resolution...")
        try:
            page.wait_for_timeout(int(check_interval * 1000))
        except Exception:
            time.sleep(check_interval)

    emit(f"DS: Turnstile not resolved within {max_wait_s:.0f}s — falling back to next tab.")
    return False


def _run_supjav_embed_resolve_click_loop(
    page: Any,
    *,
    tab: str,
    stream_url_for_download: list,
    auto_download_pending_ref: list,
    max_wall_s: float,
    emit: Callable[..., None],
    turnstile_detected_ref: list | None = None,
) -> None:
    """Same iframe play/center/get_video scrape loop as Streamtape ST; used for DS (Dood) too."""
    if tab == "ST":
        emit("Streamtape: clicking play to get direct URL...")
        wait_label = f"Streamtape ST resolve (max {max_wall_s:.0f}s, get_video)"
        stop_msg = f"Streamtape: stop clicking after {max_wall_s:.0f}s (no direct URL yet)."
        ok_msg = "Streamtape: got direct URL, starting download."
    else:
        emit("DoodStream: clicking play to get stream URL...")
        wait_label = f"DoodStream DS resolve (max {max_wall_s:.0f}s, get_video / stream)"
        stop_msg = f"DoodStream: stop clicking after {max_wall_s:.0f}s (no direct stream URL yet)."
        ok_msg = "DoodStream: got direct stream URL, starting download."
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page_wait_ms(page, 500)
    except Exception:
        pass
    deadline = time.monotonic() + max_wall_s
    attempt = 0
    while True:
        if time.monotonic() >= deadline:
            emit(stop_msg)
            break
        attempt += 1
        if attempt > 100:
            break
        cur_url = stream_url_for_download[0] or ""
        if _is_downloadable_stream_url(cur_url):
            emit(ok_msg)
            auto_download_pending_ref[0] = True
            break
        try:
            # DS: check for Cloudflare Turnstile — try to resolve before giving up
            if tab != "ST" and attempt >= 2 and _page_has_turnstile(page):
                solved = _wait_for_turnstile_solve(page, emit=emit, max_wait_s=18.0)
                if not solved:
                    if turnstile_detected_ref is not None:
                        turnstile_detected_ref[0] = True
                    break
                # Turnstile passed — continue normal loop from this point
            dismiss_ad_overlays(page)
            try_close_ad_overlay(page)
            embed_pair = _find_supjav_player_iframe_pair(page, tab)
            if not embed_pair:
                _rem = deadline - time.monotonic()
                if _rem <= 0:
                    break
                page_wait_ms(
                    page,
                    int(min(3000, max(200, _rem * 1000))),
                    label=wait_label,
                    wall_deadline_monotonic=deadline,
                )
                continue
            frame, fel = embed_pair
            box = None
            if fel:
                try:
                    box = fel.bounding_box()
                except Exception:
                    pass
            if (not box or box.get("width", 0) < 100 or box.get("height", 0) < 50) and not fel:
                iframe_sels = (
                    [
                        "iframe[src*='streamtape']",
                        "iframe[src*='streamta.']",
                        "iframe[src*='strtape']",
                        "iframe[src*='supremejav']",
                        "iframe",
                    ]
                    if tab == "ST"
                    else [
                        "iframe[src*='dood']",
                        "iframe[src*='d0011d']",
                        "iframe[src*='d0000d']",
                        "iframe[src*='ds2cd']",
                        "iframe[src*='ds2play']",
                        "iframe[src*='doodstream']",
                        "iframe[src*='supremejav']",
                        "iframe",
                    ]
                )
                for iframe_sel in iframe_sels:
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
                    page_wait_ms(page, 500)
                    box = fel.bounding_box()
                except Exception:
                    pass
            cx = (box["x"] + box["width"] / 2) if box else 640
            cy = (box["y"] + box["height"] / 2) if box else 360
            clicked_inside = False
            for sel in (
                "#videolink",
                ".play-overlay",
                ".plyr__control--overlaid",
                "button[class*='play']",
                "[class*='play-btn']",
                "[class*='play_btn']",
                "video",
                "body",
            ):
                try:
                    el = frame.locator(sel).first
                    if el.is_visible(timeout=500):
                        el.click(force=True, timeout=1000)
                        clicked_inside = True
                        break
                except Exception:
                    pass
            if not clicked_inside:
                page.mouse.move(cx, cy)
                page_wait_ms(page, 200)
                page.mouse.click(cx, cy)
            page_wait_ms(page, 1500)
            dismiss_ad_overlays(page)
            try_close_ad_overlay(page)
            page_wait_ms(page, 500)
            if not clicked_inside:
                page.mouse.click(cx, cy)
            else:
                for sel in ("video", ".play-overlay", "body"):
                    try:
                        el = frame.locator(sel).first
                        if el.is_visible(timeout=500):
                            el.click(force=True, timeout=1000)
                            break
                    except Exception:
                        pass
            page_wait_ms(
                page,
                2000,
                label=wait_label,
                wall_deadline_monotonic=deadline,
            )
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
        _rem = deadline - time.monotonic()
        if _rem <= 0:
            break
        page_wait_ms(
            page,
            int(min(3000, max(200, _rem * 1000))),
            label=wait_label,
            wall_deadline_monotonic=deadline,
        )


def run_visual_mode(
    page_url: str,
    auto_download: bool = True,
    output_filename: str = "video.m4v",
    server_tab: str = "VOE",
    skip_st: bool = False,
) -> bool:
    """Open page in visible browser; click server_tab (VOE, ST, etc.) then dismiss ads. Returns True if download succeeded (done/stopped), False otherwise."""
    requested_server_tab = str(server_tab).upper()
    
    # Parse server_tab: can be single tab (VOE) or comma-separated list (FST,VOE,TV,ST,DS)
    if "," in requested_server_tab:
        tab_list = [t.strip().upper() for t in requested_server_tab.split(",")]
        user_picked_single_tab = False
    else:
        # Single tab or default VOE
        if requested_server_tab == "VOE":
            # Default order: FST→(DS if on page)→VOE→TV→ST — DS inserted after CF if visible
            tab_list = ["FST", "VOE", "TV", "ST"]
            user_picked_single_tab = False
        else:
            # Single tab specified: stay on it
            tab_list = [requested_server_tab]
            user_picked_single_tab = True
    try:
        from datetime import datetime

        with open(VISUAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 72 + "\n")
            f.write(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\tSESSION_START\t"
                f"url={page_url}\tserver_tab={server_tab}\tauto_download={auto_download}\n"
            )
            _flush_log_to_disk(f)
        _append_server_tab_debug(
            f"=== SESSION_START url={page_url!r} server_tab={server_tab!r} auto_download={auto_download} ==="
        )
    except Exception:
        pass
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
            _wait_until_persistent_profile_released(user_data_dir)
            context = p.chromium.launch_persistent_context(str(user_data_dir), **kwargs)
            context.add_init_script(STEALTH_INIT_SCRIPT)
            DOWNLOAD_BUTTON_SCRIPT = """
                (function() {
                    // Download button disabled by user request
                })();
            """
            # context.add_init_script(DOWNLOAD_BUTTON_SCRIPT)


            def add_download_button_to_main_frame():
                # Download button disabled by user request
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
                _stderr_line(msg)

            timeline_entries: list[tuple[str, str]] = []

            def timeline(action: str) -> None:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                timeline_entries.append((ts, action))
                _visual_log(f"[TIMELINE] {ts} {action}")

            log("visual_mode started")
            timeline("start")
            log(f"goto {page_url}")
            timeline("goto_page")

            stream_url_for_download = [None]  # best m3u8 for download (HLS playlist, not jwplayer assets)
            stream_referer_for_download = [None]  # actual Referer from browser request
            target_stream_seen_ref = [False]
            stream_score_ref = [-1]
            explicit_low_quality_seen_ref = [False]  # active tab produced explicit <720 streams
            # Must exist before page.on("response") — handlers can run during page.goto.
            auto_download_pending_ref: list = [False]
            download_proc_ref: list = []
            stopped_by_user_ref: list = [False]
            download_progress_text_ref: list = [None]
            download_finished_ref: list = [None]
            download_thread_ref: list = [None]
            visual_auto_download_started_ref: list = [False]
            video_code_ref: list[str | None] = [None]  # e.g. abc-123 from title; used to reject ad HLS on TV/ST
            # FST: wall-clock deadline (monotonic) to avoid infinite wait on embed-only / stuck player.
            fst_wall_deadline_ref: list[float | None] = [None]
            last_tab_for_fst_deadline_ref: list[str | None] = [None]
            # fst vs FST breaks on_response (is_st_tv_m3u8, FST bypass) — align with CLI.
            server_tab = requested_server_tab

            def _arm_fst_wall_deadline_if_needed() -> None:
                if server_tab != last_tab_for_fst_deadline_ref[0]:
                    last_tab_for_fst_deadline_ref[0] = server_tab
                    if server_tab == "FST":
                        fst_wall_deadline_ref[0] = time.monotonic() + FST_TAB_MAX_WAIT_S
                    else:
                        fst_wall_deadline_ref[0] = None

            def _fst_wall_timed_out() -> bool:
                d = fst_wall_deadline_ref[0]
                return bool(
                    d is not None
                    and server_tab == "FST"
                    and time.monotonic() >= d
                )

            def _is_target_stream_url(url: str) -> bool:
                if all(part in url for part in TARGET_STREAM_URL_PARTS):
                    return True
                lower = url.lower()
                vc = video_code_ref[0]
                if vc and vc in lower and ".m3u8" in lower and _is_trusted_stream_cdn(url):
                    return True
                if "edgeon-bandwidth.com" in lower and "index-v1-a1.m3u8" in lower:
                    return True
                return False

            def _tab_log(msg: str) -> None:
                _visual_log(f"[tab] {msg}")

            def _is_fst_hls_txt_playlist_url(url: str) -> bool:
                """FST embeds (e.g. fc2stream) may serve HLS manifests as master.txt / index-*.txt under .urlset/ (not .m3u8)."""
                lower = url.lower()
                path = lower.split("?")[0].rstrip("/")
                if not path.endswith(".txt"):
                    return False
                if "urlset" not in lower:
                    return False
                base = path.rsplit("/", 1)[-1]
                if base.startswith("seg-") or re.match(r"^seg-\d+", base):
                    return False
                if base == "master.txt" or base.startswith("index"):
                    return True
                return False

            def _is_hls_playlist_url(url: str) -> bool:
                if _is_fst_hls_txt_playlist_url(url):
                    return True
                if ".m3u8" not in url:
                    return False
                lower = url.lower()
                # Skip jwplayer JS/CSS etc, but accept m3u8 even from jwplayer paths (VOE/dianaavoidthey)
                if ("jwplayer" in lower or "/jwplayer/" in lower) and ".m3u8" not in lower:
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

            def _capture_request_referer(response):
                """Extract Referer header from the browser request that produced this response."""
                try:
                    return response.request.headers.get("referer") or None
                except Exception:
                    return None

            def _is_ad_like_stream_url(url: str) -> bool:
                lower = url.lower()
                ad_tokens = (
                    "/ads/", "ad_", "_ad", "preroll", "midroll", "postroll", "vast", "vmap",
                    "doubleclick", "googlesyndication", "pubads", "2mdn.net",
                    "adservice", "adserver", "promo", "banner", "tracking", "pixel",
                    "ssai", "dai.google", "fwmrm.net", "interstitial", "adpod",
                )
                return any(tok in lower for tok in ad_tokens)

            def _is_analytics_redirect_url(url: str) -> bool:
                lower = url.lower()
                return (
                    "mc.yandex" in lower
                    or "yandex.ru/watch" in lower
                    or "yandex.com/watch" in lower
                    or "google-analytics" in lower
                    or "googletagmanager" in lower
                    or "doubleclick" in lower
                )

            def _quality_from_url(url: str) -> int | None:
                lower = url.lower()
                m = re.search(r"(\d{3,4})p", lower)
                if not m:
                    return None
                try:
                    return int(m.group(1))
                except ValueError:
                    return None

            def _stream_candidate_score(url: str) -> int:
                lower = url.lower()
                score = 0
                if _is_hls_playlist_url(url) or ".m3u8" in lower:
                    score += 80
                if "master.m3u8" in lower or "urlset" in lower:
                    score += 30
                if _is_streamtape_like_url(url) and "get_video" in lower:
                    score += 100
                # Prefer higher quality variants when present in URL (e.g. 240p/720p/1080p).
                m = re.search(r"(\d{3,4})p", lower)
                if m:
                    try:
                        score += int(m.group(1))
                    except ValueError:
                        pass
                # Penalize ad-like links heavily.
                if _is_ad_like_stream_url(lower):
                    score -= 500
                if _is_trusted_stream_cdn(url):
                    score += 120
                vc = video_code_ref[0]
                if vc and vc in lower:
                    score += 200
                return score

            def _set_stream_url(url, response, *, force: bool = False):
                path_lower = url.lower().split("?")[0]
                _junk = (".js", ".css", ".gif", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".woff", ".woff2", ".ico")
                if any(path_lower.endswith(ext) for ext in _junk):
                    _tab_log(f"reject junk-ext: {url[:160]}")
                    return
                if _is_analytics_redirect_url(url):
                    _tab_log(f"reject analytics-redirect: {url[:160]}")
                    return
                if not force and _is_ad_like_stream_url(url):
                    _tab_log(f"reject ad-like: {url[:160]}")
                    return
                # TV/ST/DS: preroll/ad HLS often comes from generic CDNs — only keep real player hosts or URL containing title code.
                if (
                    not force
                    and server_tab in ("TV", "ST", "DS")
                    and ".m3u8" in url.lower()
                ):
                    if _is_trusted_stream_cdn(url) or _is_streamtape_like_url(url):
                        pass
                    elif video_code_ref[0] and video_code_ref[0] in url.lower():
                        pass
                    else:
                        _tab_log(f"reject TV/ST/DS m3u8 (not trusted player CDN / no title code in URL): {url[:160]}")
                        return
                # Reject explicit low-quality variants (<720p) for all tabs.
                if not force:
                    q = _quality_from_url(url)
                    if q is not None and q < 720:
                        # Only track "only low quality" for FST — avoids stray CDN lines after switching to ST.
                        if server_tab == "FST":
                            explicit_low_quality_seen_ref[0] = True
                        _tab_log(f"reject low-quality {q}p: {url[:160]}")
                        return
                new_score = _stream_candidate_score(url)
                if force or stream_url_for_download[0] is None or new_score >= stream_score_ref[0]:
                    stream_url_for_download[0] = url
                    stream_score_ref[0] = new_score
                    _tab_log(f"select score={new_score}: {url[:180]}")
                ref = _capture_request_referer(response)
                if ref:
                    stream_referer_for_download[0] = ref

            def on_response(response):
                url = response.url
                if not url.startswith("http"):
                    return
                lower = url.lower()
                is_media = bool(response.headers.get("content-type") and is_media_content_type(response.headers.get("content-type", "")))
                # FST streams may come from domains that hit generic skip-substring filters.
                # Do not drop those if they look like real media for active FST tab.
                is_fst_media_candidate = server_tab == "FST" and (
                    ".m3u8" in lower or ".mp4" in lower or is_media or _is_fst_hls_txt_playlist_url(url)
                )
                if not url_not_skipped(url) and not is_fst_media_candidate:
                    _tab_log(f"skip by url_not_skipped: {url[:160]}")
                    return
                is_st_tv_m3u8 = server_tab in ("ST", "TV", "FST", "DS") and ".m3u8" in lower
                is_streamtape = _is_streamtape_like_url(url) and (
                    ".m3u8" in lower or ".mp4" in lower or "get_video" in lower or "/file/" in lower or "/e/" in lower or is_media
                )
                _tab_log(
                    f"resp ct_media={is_media} m3u8={'.m3u8' in lower} mp4={'.mp4' in lower} "
                    f"cand={is_fst_media_candidate} url={url[:160]}"
                )
                if not (is_stream_url(url) or is_media or is_st_tv_m3u8 or is_streamtape or _is_fst_hls_txt_playlist_url(url)):
                    return
                if _is_hls_playlist_url(url):
                    _set_stream_url(url, response)
                    timeline("stream_captured_m3u8")
                elif is_st_tv_m3u8:
                    if "_HLS_msn" not in lower and "_HLS_part" not in lower and "segment" not in lower:
                        _set_stream_url(url, response)
                        timeline("stream_captured_m3u8_st_tv_fst")
                elif is_streamtape:
                    current = stream_url_for_download[0] or ""
                    if "get_video" in url.lower():
                        _set_stream_url(url, response)
                    elif "get_video" not in current.lower():
                        _set_stream_url(url, response)
                    timeline("stream_captured_streamtape")
                if _is_target_stream_url(url):
                    target_stream_seen_ref[0] = True
                    timeline(f"TARGET_STREAM_APPEARED: {url}")
                    log("Target stream link appeared.")
                    _set_stream_url(url, response, force=True)
                    if auto_download and _is_downloadable_stream_url(url):
                        auto_download_pending_ref[0] = True
                # doppiocdn / Streamtape rarely match TARGET_STREAM_URL_PARTS — arm auto-download for any active server tab.
                if (
                    auto_download
                    and server_tab in ("ST", "TV", "FST", "VOE", "DS")
                    and stream_url_for_download[0]
                    and _is_downloadable_stream_url(stream_url_for_download[0])
                    and not download_proc_ref
                    and not visual_auto_download_started_ref[0]
                ):
                    auto_download_pending_ref[0] = True

            # Context sees responses from all frames/tabs; page.on can miss iframe CDN (FST / doppiocdn m3u8).
            context.on("response", on_response)
            page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            timeline("page_loaded")
            log("cloudflare_wait_start")
            wait_for_cloudflare_pass(page)
            log("cloudflare_wait_done")
            timeline("cloudflare_passed")
            page_wait_ms(page, 2000, intro="visual: page settle after Cloudflare (2s):")
            try:
                video_code_ref[0] = _extract_video_code_from_title(page.title() or "")
                if video_code_ref[0]:
                    log(f"video_code_from_title: {video_code_ref[0]}")
            except Exception:
                pass
            page.evaluate("""() => {
                document.querySelectorAll('a.btn-server[target="_blank"]').forEach(a => { a.removeAttribute('target'); });
            }""")
            timeline("remove_target_blank_done")
            page_wait_ms(page,500)
            tabs_to_try = tab_list
            if skip_st:
                tabs_to_try = [t for t in tabs_to_try if t != "ST"]
                if "ST" in tab_list and len(tab_list) == 1:
                    log("skip_st is enabled and server_tab=ST was requested; stopping.")
                    return False
            tabs_to_try = _maybe_adjust_tab_list_for_ds(
                page, tabs_to_try, user_picked_single_tab=user_picked_single_tab
            )
            log(f"server_tab_click_start (try: {tabs_to_try}) — only a.btn-server or SERVER block (avoid ad links)")
            log(f"server_tab_click_order: {'-'.join(tabs_to_try)}")
            try:
                _st_purl = page.url
            except Exception:
                _st_purl = ""
            _append_server_tab_debug(
                f"server_tab_phase_start: page_url={_st_purl!r} tab_list={tab_list!r} tabs_to_try={tabs_to_try!r} "
                f"user_picked_single_tab={user_picked_single_tab} skip_st={skip_st}"
            )
            if "DS" in tabs_to_try:
                _log_server_tab_dom_diag(page, "DS", "before_server_tab_loop")
            tab_clicked = False
            try:
                try:
                    page.wait_for_selector('text=SERVER', timeout=8000)
                except Exception:
                    pass
                # Dismiss ad overlays before clicking any server tab
                dismiss_ad_overlays(page)
                for _ in range(3):
                    try_close_ad_overlay(page)
                    page_wait_ms(page,200)
                page_wait_ms(page,500)
                for try_tab in tabs_to_try:
                    log(f"server_tab_click_button: {try_tab}")
                    tab_clicked = False
                    _is_ds_try = str(try_tab).upper() == "DS"
                    if _is_ds_try:
                        _append_server_tab_debug("DS: iteration_start")
                        _log_server_tab_dom_diag(page, "DS", "DS_after_dismiss_before_js")
                    if try_tab == "ST":
                        dismiss_ad_overlays(page)
                        page_wait_ms(page,400)
                        for _ in range(2):
                            try_close_ad_overlay(page)
                            page_wait_ms(page,200)
                    if try_tab == "DS":
                        dismiss_ad_overlays(page)
                        page_wait_ms(page,400)
                        for _ in range(2):
                            try_close_ad_overlay(page)
                            page_wait_ms(page,200)
                    if str(try_tab).upper() == "FST":
                        dismiss_ad_overlays(page)
                        page_wait_ms(page,400)
                        for _ in range(2):
                            try_close_ad_overlay(page)
                            page_wait_ms(page,200)
                    _tab_label_re = re.compile(r"^" + re.escape(try_tab) + r"$", re.I)
                    # Build JS that clicks only a.btn-server with safe href (no ad domains) — use this FIRST for VOE/TV to avoid opening ads
                    _label_esc_js = try_tab.replace("\\", "\\\\").replace("'", "\\'")
                    _click_tab_js = f"""() => {{
                            var label = '{_label_esc_js}';
                            var labelU = label.toUpperCase();
                            function tabTxtNorm(s) {{
                                return (s || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                            }}
                            var adLike = /ads?\\b|popads|popcash|exoclick|propeller|dillinger|cactushead|juicyads|trafficjunky|revcontent|taboola|outbrain|mgid\\.com|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                            var btns = document.querySelectorAll('a.btn-server');
                            for (var i = 0; i < btns.length; i++) {{
                                var a = btns[i];
                                if (tabTxtNorm(a.textContent || a.innerText) !== labelU) continue;
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
                                var tu = t.toUpperCase();
                                if (tu.indexOf('SERVER') >= 0 && tu.indexOf(labelU) >= 0 && t.length < 150) {{
                                    var links = el.querySelectorAll('a.btn-server');
                                    for (var j = 0; j < links.length; j++) {{
                                        var a = links[j];
                                        if (tabTxtNorm(a.textContent || a.innerText) !== labelU) continue;
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
                    if _is_ds_try:
                        _append_server_tab_debug(f"DS: safe_js_main_result tab_clicked={tab_clicked}")
                    if not tab_clicked:
                        for frame in page.frames:
                            if frame == page.main_frame:
                                continue
                            try:
                                if frame.evaluate(_click_tab_js):
                                    tab_clicked = True
                                    break
                            except Exception as e:
                                if _is_ds_try:
                                    _append_server_tab_debug(f"DS: safe_js_child_frame_error={e!r}")
                    if _is_ds_try:
                        _append_server_tab_debug(f"DS: after_all_safe_js tab_clicked={tab_clicked}")
                        if not tab_clicked:
                            _log_server_tab_dom_diag(page, "DS", "DS_safe_js_failed_DOM_snapshot")
                    if tab_clicked:
                        log(f"server_tab_click_success: {try_tab}")
                        page_wait_ms(page,400)
                    # Only if safe click failed: try Playwright locator (may hit ad if multiple VOE links)
                    if not tab_clicked:
                        try:
                            btn = page.locator("a.btn-server").filter(has_text=_tab_label_re).first
                            if btn.is_visible(timeout=3000):
                                btn.scroll_into_view_if_needed()
                                page_wait_ms(page,200)
                                if try_tab in ("ST", "DS"):
                                    tab_clicked = _click_center(page, btn)
                                    if not tab_clicked:
                                        btn.click(force=True)
                                        tab_clicked = True
                                else:
                                    btn.click(force=True)
                                    tab_clicked = True
                        except Exception as e:
                            if _is_ds_try:
                                _append_server_tab_debug(f"DS: playwright_main_locator_error={e!r}")
                            pass
                        if _is_ds_try:
                            _append_server_tab_debug(f"DS: playwright_main_locator_round_done tab_clicked={tab_clicked}")
                    if not tab_clicked:
                        for frame in page.frames:
                            try:
                                btn = frame.locator("a.btn-server").filter(has_text=_tab_label_re).first
                                if btn.is_visible(timeout=2000):
                                    btn.scroll_into_view_if_needed()
                                    page_wait_ms(page,200)
                                    if try_tab in ("ST", "DS"):
                                        tab_clicked = _click_center(page, btn)
                                        if not tab_clicked:
                                            btn.click(force=True)
                                            tab_clicked = True
                                    else:
                                        btn.click(force=True)
                                        tab_clicked = True
                                    if _is_ds_try:
                                        _append_server_tab_debug(f"DS: playwright_in_frame_ok tab_clicked={tab_clicked}")
                                    break
                            except Exception as e:
                                if _is_ds_try:
                                    _append_server_tab_debug(f"DS: playwright_frame_locator_error={e!r}")
                                pass
                    if not tab_clicked:
                        tab_clicked = page.evaluate(_click_tab_js)
                        if _is_ds_try:
                            _append_server_tab_debug(f"DS: second_round_safe_js_main tab_clicked={tab_clicked}")
                        if not tab_clicked:
                            for frame in page.frames:
                                if frame == page.main_frame:
                                    continue
                                try:
                                    if frame.evaluate(_click_tab_js):
                                        tab_clicked = True
                                        break
                                except Exception as e:
                                    if _is_ds_try:
                                        _append_server_tab_debug(f"DS: second_round_safe_js_frame_error={e!r}")
                    if _is_ds_try:
                        _append_server_tab_debug(f"DS: after_second_round_js tab_clicked={tab_clicked}")
                    if tab_clicked:
                        server_tab = str(try_tab).upper()
                        timeline(f"server_tab_clicked_{server_tab}")
                        break
                    if try_tab == "ST" and not tab_clicked:
                        page_wait_ms(page,2_000)
                        dismiss_ad_overlays(page)
                        page_wait_ms(page,400)
                        try_close_ad_overlay(page)
                        try:
                            btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^ST$")).first
                            if btn.is_visible(timeout=2000):
                                btn.scroll_into_view_if_needed()
                                page_wait_ms(page,200)
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
                    if try_tab == "DS" and not tab_clicked:
                        _append_server_tab_debug("DS: dedicated_retry_block_start")
                        _log_server_tab_dom_diag(page, "DS", "DS_before_dedicated_retry")
                        page_wait_ms(page,2_000)
                        dismiss_ad_overlays(page)
                        page_wait_ms(page,400)
                        try_close_ad_overlay(page)
                        try:
                            btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^DS$")).first
                            if btn.is_visible(timeout=2000):
                                btn.scroll_into_view_if_needed()
                                page_wait_ms(page,200)
                                tab_clicked = _click_center(page, btn)
                                _append_server_tab_debug(f"DS: dedicated_retry_center_click tab_clicked={tab_clicked}")
                                if not tab_clicked:
                                    btn.click(force=True)
                                    tab_clicked = True
                                _append_server_tab_debug(f"DS: dedicated_retry_after_force tab_clicked={tab_clicked}")
                            else:
                                _append_server_tab_debug("DS: dedicated_retry_DS_btn_not_visible_2000ms")
                        except Exception as e:
                            _append_server_tab_debug(f"DS: dedicated_retry_block_error={e!r}")
                            pass
                        if not tab_clicked:
                            tab_clicked = page.evaluate(_click_tab_js)
                            _append_server_tab_debug(f"DS: dedicated_retry_safe_js_after block tab_clicked={tab_clicked}")
                        if tab_clicked:
                            server_tab = "DS"
                            timeline("server_tab_clicked_ds")
                            break
                    if str(try_tab).upper() == "FST" and not tab_clicked and _fst_server_tab_visible(page):
                        _label_esc = str(try_tab).replace("\\", "\\\\").replace("'", "\\'")
                        _fst_no_ad_js = f"""() => {{
                            var label = '{_label_esc}';
                            var labelU = label.toUpperCase();
                            function tabTxtNorm(s) {{
                                return (s || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                            }}
                            var btns = document.querySelectorAll('a.btn-server');
                            for (var i = 0; i < btns.length; i++) {{
                                var a = btns[i];
                                if (tabTxtNorm(a.textContent || a.innerText) !== labelU) continue;
                                a.scrollIntoView({{ block: 'center' }});
                                a.click();
                                return true;
                            }}
                            document.querySelectorAll('*').forEach(function(el) {{
                                var t = (el.innerText || '').trim();
                                var tu = t.toUpperCase();
                                if (tu.indexOf('SERVER') >= 0 && tu.indexOf(labelU) >= 0 && t.length < 150) {{
                                    var links = el.querySelectorAll('a.btn-server');
                                    for (var j = 0; j < links.length; j++) {{
                                        var a = links[j];
                                        if (tabTxtNorm(a.textContent || a.innerText) !== labelU) continue;
                                        a.scrollIntoView({{ block: 'center' }});
                                        a.click();
                                        return true;
                                    }}
                                }}
                            }});
                            return false;
                        }}"""
                        log("FST tab: retry without ad-href filter (href can look like ad)")
                        for _fst_retry in range(6):
                            dismiss_ad_overlays(page)
                            try_close_ad_overlay(page)
                            page_wait_ms(page,400)
                            tab_clicked = page.evaluate(_fst_no_ad_js)
                            if not tab_clicked:
                                for frame in page.frames:
                                    if frame == page.main_frame:
                                        continue
                                    try:
                                        if frame.evaluate(_fst_no_ad_js):
                                            tab_clicked = True
                                            break
                                    except Exception:
                                        pass
                            if tab_clicked:
                                break
                            try:
                                btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^FST$", re.I)).first
                                if btn.is_visible(timeout=2500):
                                    btn.scroll_into_view_if_needed()
                                    page_wait_ms(page,200)
                                    btn.click(force=True)
                                    tab_clicked = True
                                    break
                            except Exception:
                                pass
                            try:
                                page.get_by_role("link", name=re.compile(r"^FST$", re.I)).first.click(timeout=4000)
                                tab_clicked = True
                                break
                            except Exception:
                                pass
                            page_wait_ms(page,1200)
                        if tab_clicked:
                            server_tab = "FST"
                            timeline("server_tab_clicked_fst")
                            break
                    page_wait_ms(page,300)
                if not tab_clicked:
                    log(f"server_tab_not_visible (tried {tabs_to_try})")
                    try:
                        _fail_url = page.url
                    except Exception:
                        _fail_url = ""
                    _append_server_tab_debug(
                        f"server_tab_not_visible: tried={tabs_to_try!r} page_url={_fail_url!r}"
                    )
                    if "DS" in tabs_to_try:
                        _log_server_tab_dom_diag(page, "DS", "final_failure_DOM_snapshot")
                    # No usable server tab on this page: fail fast so caller can skip this item.
                    return False
                else:
                    log(f"server_tab_clicked: {server_tab}")
                    page_wait_ms(
                        page,
                        3000,
                        label=f"after SERVER tab {server_tab} click (player settle)",
                    )
                    _arm_fst_wall_deadline_if_needed()
                # VOE: verify iframe loaded (page defaults to TV; first click may be eaten by ad overlay)
                if server_tab == "VOE" and tab_clicked:
                    _VOE_IFRAME_MARKERS = ("dianaavoidthey", "voe.sx", "voe-", "voeunblock", "guardianagainstyou")

                    def _voe_iframe_loaded():
                        for f in page.frames:
                            if f == page.main_frame:
                                continue
                            furl = (f.url or "").lower()
                            if any(m in furl for m in _VOE_IFRAME_MARKERS):
                                return True
                            try:
                                el = f.frame_element()
                                src = (el.get_attribute("src") or "").lower()
                                if any(m in src for m in _VOE_IFRAME_MARKERS):
                                    return True
                            except Exception:
                                pass
                        # Also check for supremejav iframe that wraps VOE (not streamtape/turbo)
                        for f in page.frames:
                            if f == page.main_frame:
                                continue
                            furl = (f.url or "").lower()
                            if "supremejav" in furl and "streamtape" not in furl and "turbo" not in furl:
                                for sf in f.child_frames:
                                    sfurl = (sf.url or "").lower()
                                    if any(m in sfurl for m in _VOE_IFRAME_MARKERS):
                                        return True
                                # supremejav iframe loaded; VOE might be nested further
                                if "jwplayer" in furl or len(f.child_frames) > 0:
                                    return True
                        return False

                    if not _voe_iframe_loaded():
                        log("VOE iframe not loaded after click, retrying with ad dismissal...")
                        _voe_label_js = _click_tab_js.replace("'{}'".format(tabs_to_try[0].replace("\\", "\\\\").replace("'", "\\'")), "'VOE'") if tabs_to_try[0] != "VOE" else _click_tab_js
                        # Rebuild JS specifically for VOE
                        _voe_click_js = """() => {
                            var adLike = /ads?\\b|popads|popcash|exoclick|propeller|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                            var btns = document.querySelectorAll('a.btn-server');
                            for (var i = 0; i < btns.length; i++) {
                                var a = btns[i];
                                if ((a.textContent || a.innerText || '').trim() !== 'VOE') continue;
                                var h = (a.getAttribute('href') || '').trim();
                                if (adLike.test(h)) continue;
                                a.scrollIntoView({ block: 'center' });
                                a.click();
                                return true;
                            }
                            return false;
                        }"""
                        for retry in range(5):
                            dismiss_ad_overlays(page)
                            try_close_ad_overlay(page)
                            page_wait_ms(page,500)
                            # Close any popup windows opened by the ad click
                            try:
                                all_pages = context.pages
                                if len(all_pages) > 1:
                                    for p in all_pages[1:]:
                                        try:
                                            p.close()
                                        except Exception:
                                            pass
                                    log(f"Closed {len(all_pages) - 1} popup tab(s)")
                            except Exception:
                                pass
                            page.evaluate(_voe_click_js)
                            page_wait_ms(page,4000)
                            if _voe_iframe_loaded():
                                log(f"VOE iframe loaded after retry {retry + 1}")
                                break
                            # Also try Playwright native click
                            try:
                                btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^VOE$")).first
                                if btn.is_visible(timeout=1000):
                                    btn.click(force=True)
                                    page_wait_ms(page,4000)
                                    if _voe_iframe_loaded():
                                        log(f"VOE iframe loaded after native click retry {retry + 1}")
                                        break
                            except Exception:
                                pass
                        if not _voe_iframe_loaded():
                            log("VOE iframe failed to load after retries, will rely on click loop")

                # ST: verify iframe loaded; if not, retry clicks
                if server_tab == "ST" and tab_clicked:
                    def _st_iframe_loaded():
                        for f in page.frames:
                            if f == page.main_frame:
                                continue
                            furl = f.url or ""
                            if _is_streamtape_like_url(furl) or "supremejav" in furl.lower():
                                return True
                        return False
                    if not _st_iframe_loaded():
                        for retry in range(5):
                            dismiss_ad_overlays(page)
                            try_close_ad_overlay(page)
                            page_wait_ms(page,500)
                            page.evaluate(_click_tab_js)
                            page_wait_ms(
                                page,
                                3000,
                                label="ST: wait for Streamtape / player iframe",
                            )
                            if _st_iframe_loaded():
                                break
                            try:
                                btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^ST$")).first
                                if btn.is_visible(timeout=1000):
                                    btn.evaluate("el => el.click()")
                                    page_wait_ms(
                                        page,
                                        3000,
                                        label="ST: wait for Streamtape / player iframe",
                                    )
                                    if _st_iframe_loaded():
                                        break
                            except Exception:
                                pass

                # DS: verify DoodStream / wrapper iframe (same pattern as ST)
                if server_tab == "DS" and tab_clicked:
                    def _ds_iframe_loaded():
                        for f in page.frames:
                            if f == page.main_frame:
                                continue
                            furl = (f.url or "").lower()
                            if any(
                                m in furl
                                for m in (
                                    "doodstream",
                                    "dood.",
                                    "d0011d",
                                    "d0000d",
                                    "ds2cd",
                                    "ds2play",
                                )
                            ):
                                return True
                            if "supremejav" in furl:
                                return True
                        return False

                    if not _ds_iframe_loaded():
                        for _retry_ds in range(5):
                            dismiss_ad_overlays(page)
                            try_close_ad_overlay(page)
                            page_wait_ms(page, 500)
                            page.evaluate(_click_tab_js)
                            page_wait_ms(
                                page,
                                3000,
                                label="DS: wait for DoodStream / player iframe",
                            )
                            if _ds_iframe_loaded():
                                break
                            try:
                                btn = page.locator("a.btn-server").filter(has_text=re.compile(r"^DS$")).first
                                if btn.is_visible(timeout=1000):
                                    btn.evaluate("el => el.click()")
                                    page_wait_ms(
                                        page,
                                        3000,
                                        label="DS: wait for DoodStream / player iframe",
                                    )
                                    if _ds_iframe_loaded():
                                        break
                            except Exception:
                                pass
            except Exception as e:
                log(f"server_tab_click_error {e!r}")
            dismiss_ad_overlays(page)
            page_wait_ms(page,500)
            timeline("dismiss_ad_overlays_done")
            for _ in range(2):
                try_close_ad_overlay(page)
                page_wait_ms(page,300)
            add_download_button_to_main_frame()
            timeline("download_button_injected_initial")
            try:
                for frame in page.frames:
                    try:
                        frame.evaluate(DOWNLOAD_BUTTON_SCRIPT)
                    except Exception:
                        pass
                page.evaluate("() => !!window.__downloadBtnAttached")
                page_wait_ms(page,800)
                add_download_button_to_main_frame()
            except Exception as ex:
                log(f"download_button_attach_error (after tab) {ex!r}")
            stop_event = threading.Event()

            def wait_enter():
                # In some environments stdin can be closed (no tty), which raises EOFError.
                # Do not set stop_event on EOF — otherwise visual mode exits immediately and
                # auto-download never runs when launched from a non-interactive parent.
                try:
                    input()
                except EOFError:
                    return
                stop_event.set()

            threading.Thread(target=wait_enter, daemon=True).start()
            last_waited_url = page.url
            _key_check_iters = [0]
            auto_click_iters = [0]
            voe_click_loop_done_ref = [False]  # VOE: run "click until stream" loop only once
            tv_click_loop_done_ref = [False]   # TV/ST/DS: run "click until stream" loop only once (~60s timeout)
            voe_failed_try_tv_ref = [False]    # after VOE timeout, try TV once
            tv_failed_try_fst_ref = [False]    # after TV timeout, try FST once
            fst_failed_try_st_ref = [False]    # after FST timeout, try ST once
            st_fallback_from_st_attempted_ref = [False]  # after ST gives up, try user-order fallbacks once
            ds_fallback_from_ds_attempted_ref = [False]  # after DS gives up, next tabs per -s (e.g. ST after DS)
            download_data_flowing = threading.Event()

            def progress_from_download_thread(text: str):
                """Called from download thread: only store progress; main thread updates the button."""
                download_progress_text_ref[0] = text
                if text and any(c.isdigit() for c in text):
                    download_data_flowing.set()

            _fallback_tab_click_js = """() => {
                        const target = "__TAB__";
                        var adLike = /ads?\\b|popads|popcash|exoclick|propeller|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                        var btns = document.querySelectorAll('a.btn-server');
                        for (var i = 0; i < btns.length; i++) {
                            var a = btns[i];
                            if ((a.textContent || a.innerText || '').trim().toUpperCase() !== target) continue;
                            if (adLike.test((a.getAttribute('href') || '').trim())) continue;
                            a.scrollIntoView({ block: 'center' });
                            a.click();
                            return true;
                        }
                        return false;
                    }"""

            def _evaluate_fallback_tab_click(target_tab: str) -> bool:
                js = _fallback_tab_click_js.replace("__TAB__", target_tab)
                if page.evaluate(js):
                    return True
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    try:
                        if frame.evaluate(js):
                            return True
                    except Exception:
                        pass
                return False

            def _switch_to_next_fallback(reason: str) -> None:
                nonlocal server_tab
                if user_picked_single_tab:
                    log(f"{server_tab}: {reason} — staying on {requested_server_tab} (single-tab mode).")
                    return
                if server_tab == "ST" and not st_fallback_from_st_attempted_ref[0]:
                    st_fallback_from_st_attempted_ref[0] = True
                    candidates = _st_fallback_candidate_tabs(tabs_to_try, page)
                    if not candidates:
                        log(
                            "ST: no other visible server tabs to try after Streamtape "
                            f"(launch order was {'-'.join(tabs_to_try)})."
                        )
                        return
                    dismiss_ad_overlays(page)
                    page_wait_ms(page, 500)
                    switched = False
                    for cand in candidates:
                        if cand == "ST" and skip_st:
                            continue
                        _visual_log(
                            f"auto_click_player: {server_tab} — {reason}; trying {cand} "
                            f"(fallback order from -s: {'-'.join(tabs_to_try)})..."
                        )
                        log(f"{server_tab}: {reason}, trying {cand}...")
                        try:
                            clicked_next = _evaluate_fallback_tab_click(cand)
                            if clicked_next:
                                page_wait_ms(
                                    page,
                                    3000,
                                    label=f"after fallback click -> {cand} (player settle)",
                                )
                                server_tab = cand
                                explicit_low_quality_seen_ref[0] = False
                                tv_click_loop_done_ref[0] = False
                                try:
                                    page._st_click_loop_done = False
                                    page._ds_click_loop_done = False
                                except Exception:
                                    pass
                                if cand == "VOE":
                                    voe_click_loop_done_ref[0] = False
                                if cand == "DS":
                                    ds_fallback_from_ds_attempted_ref[0] = False
                                auto_click_iters[0] = 5
                                switched = True
                                break
                        except Exception as e:
                            log(f"Failed to switch to {cand}: {e!r}; trying next candidate...")
                    if not switched:
                        log(
                            f"ST: could not click any fallback tab among {candidates} "
                            "(visible but blocked or ad-href filtered); stopping with error."
                        )
                        stop_event.set()
                    return

                # DS: next tab follows -s order (e.g. VOE,TV,FST,DS,ST -> after DS try ST, not hardcoded FST)
                if server_tab == "DS" and not ds_fallback_from_ds_attempted_ref[0]:
                    ds_fallback_from_ds_attempted_ref[0] = True
                    candidates = _fallback_candidate_tabs_after_current(tabs_to_try, page, "DS")
                    if not candidates:
                        log(
                            f"DS: no visible server tab to try after DS in order "
                            f"({'-'.join(tabs_to_try)})."
                        )
                        return
                    dismiss_ad_overlays(page)
                    page_wait_ms(page, 500)
                    switched = False
                    for cand in candidates:
                        if cand == "ST" and skip_st:
                            continue
                        _visual_log(
                            f"auto_click_player: {server_tab} — {reason}; trying {cand} "
                            f"(fallback order from -s: {'-'.join(tabs_to_try)})..."
                        )
                        log(f"{server_tab}: {reason}, trying {cand}...")
                        try:
                            clicked_next = _evaluate_fallback_tab_click(cand)
                            if clicked_next:
                                page_wait_ms(
                                    page,
                                    3000,
                                    label=f"after fallback click -> {cand} (player settle)",
                                )
                                server_tab = cand
                                explicit_low_quality_seen_ref[0] = False
                                tv_click_loop_done_ref[0] = False
                                try:
                                    page._st_click_loop_done = False
                                    page._ds_click_loop_done = False
                                except Exception:
                                    pass
                                if cand == "VOE":
                                    voe_click_loop_done_ref[0] = False
                                if cand == "ST":
                                    st_fallback_from_st_attempted_ref[0] = False
                                auto_click_iters[0] = 5
                                switched = True
                                break
                        except Exception as e:
                            log(f"Failed to switch to {cand}: {e!r}; trying next candidate...")
                    if not switched:
                        log(
                            f"DS: could not click any fallback tab among {candidates} "
                            "(visible but blocked or ad-href filtered); stopping with error."
                        )
                        stop_event.set()
                    return

                next_tab = None
                if server_tab == "VOE" and not voe_failed_try_tv_ref[0]:
                    voe_failed_try_tv_ref[0] = True
                    next_tab = "DS" if _ds_server_tab_visible(page) else "TV"
                elif server_tab == "TV" and not tv_failed_try_fst_ref[0]:
                    tv_failed_try_fst_ref[0] = True
                    next_tab = "FST"
                elif server_tab == "FST" and not fst_failed_try_st_ref[0] and not skip_st:
                    fst_failed_try_st_ref[0] = True
                    next_tab = "ST"
                if not next_tab:
                    return
                _visual_log(f"auto_click_player: {server_tab} — {reason}; trying {next_tab}...")
                log(f"{server_tab}: {reason}, switching to {next_tab}...")
                dismiss_ad_overlays(page)
                page_wait_ms(page,500)
                try:
                    clicked_next = _evaluate_fallback_tab_click(next_tab)
                    if clicked_next:
                        page_wait_ms(
                            page,
                            3000,
                            label=f"after fallback click -> {next_tab} (player settle)",
                        )
                        server_tab = next_tab
                        explicit_low_quality_seen_ref[0] = False
                        tv_click_loop_done_ref[0] = False
                        try:
                            page._st_click_loop_done = False
                            page._ds_click_loop_done = False
                        except Exception:
                            pass
                        if next_tab == "VOE":
                            voe_click_loop_done_ref[0] = False
                        auto_click_iters[0] = 5
                    else:
                        log(f"{next_tab} tab not found; stopping with error.")
                        stop_event.set()
                except Exception as e:
                    log(f"Failed to switch to {next_tab}: {e!r}; stopping.")
                    stop_event.set()

            log("Ready. Click Download when stream is visible; click again to stop download.")
            while True:
                poll_interval = 0.4 if download_proc_ref else 2.0
                if stop_event.wait(poll_interval):
                    break
                try:
                    _arm_fst_wall_deadline_if_needed()
                    if _fst_wall_timed_out():
                        usable_fst = bool(
                            stream_url_for_download[0]
                            and _is_downloadable_stream_url(stream_url_for_download[0])
                        )
                        if not usable_fst and not download_proc_ref:
                            prev_fst = server_tab
                            _switch_to_next_fallback(
                                f"FST timeout — no downloadable stream within {FST_TAB_MAX_WAIT_S:.0f}s"
                            )
                            _arm_fst_wall_deadline_if_needed()
                            if server_tab == prev_fst == "FST":
                                log(
                                    f"FST: no downloadable stream within {FST_TAB_MAX_WAIT_S:.0f}s; stopping."
                                )
                                stop_event.set()
                                continue
                    if download_progress_text_ref[0] is not None:
                        set_download_button_progress(download_progress_text_ref[0])
                    if (
                        auto_download
                        and not visual_auto_download_started_ref[0]
                        and not download_proc_ref
                        and stream_url_for_download[0]
                        and _is_downloadable_stream_url(stream_url_for_download[0])
                        and server_tab in ("ST", "TV", "FST", "VOE", "DS")
                    ):
                        if not auto_download_pending_ref[0]:
                            log("Auto-download: stream URL ready (main loop poll).")
                        auto_download_pending_ref[0] = True
                    if download_finished_ref[0] is not None:
                        set_download_button_state(download_finished_ref[0])
                        log(f"Download state: {download_finished_ref[0]}.")
                        download_finished_ref[0] = None
                        download_progress_text_ref[0] = None
                    if download_proc_ref and page.evaluate("() => !!(window.__userStopDownload || (window.top && window.top.__userStopDownload))"):
                        try:
                            stopped_by_user_ref[0] = True
                            download_proc_ref[0].kill()
                            _proc_wait_with_countdown(
                                download_proc_ref[0], 5.0, "Child process after kill (max 5s):"
                            )
                        except Exception:
                            pass
                        download_proc_ref.clear()
                        page.evaluate("() => { try { window.__userStopDownload = false; if (window.top) window.top.__userStopDownload = false; } catch(e){} }")
                        set_download_button_state("stopped")
                        log("Download stopped (saved).")
                    if auto_download_pending_ref[0]:
                        download_url = stream_url_for_download[0]
                        if download_url and not _is_downloadable_stream_url(download_url):
                            log(f"Waiting for direct stream URL (have embed only: {download_url[:80]}...)")
                            download_url = None
                        if download_url:
                            auto_download_pending_ref[0] = False
                            visual_auto_download_started_ref[0] = True
                            log("Auto-download: target link appeared, starting.")
                            _log_stream_url(download_url, "download_click")
                            try:
                                LAST_DOWNLOAD_URL_FILE.write_text(download_url, encoding="utf-8")
                            except Exception:
                                pass
                            log("Stream URL saved to last_download_url.txt")
                            set_download_button_state("downloading")
                            log("Auto-download started. You can close the browser; download will continue.")
                            out_path = DOWNLOAD_DIR / output_filename
                            stopped_by_user_ref[0] = False
                            download_proc_ref.clear()

                            dl_referer = stream_referer_for_download[0] or "https://supjav.com/"

                            def run_download():
                                try:
                                    _set_console_progress_prefix_for_download(
                                        output_filename,
                                        download_url,
                                        str(server_tab),
                                        video_code_ref[0],
                                    )
                                    try:
                                        result = download_video(
                                            download_url,
                                            out_path,
                                            referer=dl_referer,
                                            progress_callback=progress_from_download_thread,
                                            out_proc=download_proc_ref,
                                            stopped_by_user=stopped_by_user_ref,
                                        )
                                    finally:
                                        _clear_console_progress_prefix()
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
                            _dl_first_byte_timeout = 40 if server_tab == "FST" else 20
                            data_ok = _event_wait_with_countdown(
                                download_data_flowing,
                                float(_dl_first_byte_timeout),
                                f"Waiting for first download bytes (max {_dl_first_byte_timeout:.0f}s):",
                            )
                            dl_failed = download_finished_ref[0] == "failed"
                            # If download failed or timed out without data, try next fallback tab.
                            if (dl_failed or not data_ok) and server_tab in ("TV", "VOE", "FST", "DS"):
                                if user_picked_single_tab:
                                    log(
                                        f"{server_tab}: download {'failed' if dl_failed else 'timed out waiting for data'}; "
                                        f"single-tab mode — not switching server tab."
                                    )
                                    if download_proc_ref:
                                        try:
                                            download_proc_ref[0].kill()
                                            _proc_wait_with_countdown(
                                                download_proc_ref[0],
                                                5.0,
                                                "Child process after kill (max 5s):",
                                            )
                                        except Exception:
                                            pass
                                        download_proc_ref.clear()
                                    download_finished_ref[0] = None
                                    download_data_flowing.clear()
                                    auto_download_pending_ref[0] = False
                                    stream_url_for_download[0] = None
                                    stream_referer_for_download[0] = None
                                    target_stream_seen_ref[0] = False
                                    explicit_low_quality_seen_ref[0] = False
                                    # Allow another auto-download attempt on this tab (same issue as multi-tab fallback).
                                    visual_auto_download_started_ref[0] = False
                                    download_thread_ref[0] = None
                                    continue
                                fallback_tab = "FST" if server_tab in ("TV", "DS") else "ST"
                                if fallback_tab == "ST" and skip_st:
                                    log(
                                        f"Download {'failed' if dl_failed else 'timed out'} on {server_tab}; "
                                        "skip_st is set — no ST fallback, stopping."
                                    )
                                    stop_event.set()
                                    continue
                                log(
                                    f"Download {'failed' if dl_failed else 'timed out'} on {server_tab}, "
                                    f"switching to {fallback_tab}..."
                                )
                                # Kill stuck download process
                                if download_proc_ref:
                                    try:
                                        download_proc_ref[0].kill()
                                        _proc_wait_with_countdown(
                                            download_proc_ref[0],
                                            5.0,
                                            "Child process after kill (max 5s):",
                                        )
                                    except Exception:
                                        pass
                                    download_proc_ref.clear()
                                download_finished_ref[0] = None
                                download_data_flowing.clear()
                                stream_url_for_download[0] = None
                                stream_referer_for_download[0] = None
                                target_stream_seen_ref[0] = False
                                auto_download_pending_ref[0] = False
                                # Otherwise on_response / main loop never re-arm auto_download_pending
                                # (guards use visual_auto_download_started_ref and download_thread_ref).
                                visual_auto_download_started_ref[0] = False
                                download_thread_ref[0] = None
                                server_tab = fallback_tab
                                dismiss_ad_overlays(page)
                                page_wait_ms(page,500)
                                _fallback_js = """() => {
                                    var want = '__TAB__';
                                    var btns = document.querySelectorAll('a.btn-server');
                                    for (var i = 0; i < btns.length; i++) {
                                        if ((btns[i].textContent || '').trim().toUpperCase() === want) {
                                            btns[i].scrollIntoView({block:'center'});
                                            btns[i].click();
                                            return true;
                                        }
                                    }
                                    return false;
                                }""".replace("__TAB__", fallback_tab)
                                try:
                                    page.evaluate(_fallback_js)
                                except Exception:
                                    pass
                                page_wait_ms(page, 5000, intro="visual: fallback tab settle (5s):")
                                # Reset click-loop state so fallback tab gets its own timeout loop.
                                try:
                                    page._st_click_loop_done = False
                                    page._ds_click_loop_done = False
                                except Exception:
                                    pass
                                tv_click_loop_done_ref[0] = False
                                auto_click_iters[0] = 5
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
                            # Pending stays True until a direct m3u8/mp4/get_video URL is available (embed-only phase).
                            pass
                    current_url = page.url
                    if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS):
                        _visual_log("blocked_ad_navigation going_back")
                        try:
                            page.go_back()
                            page_wait_ms(page,1000)
                        except Exception:
                            pass
                        continue
                    if not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                        _visual_log("foreign_site_navigation going_back")
                        try:
                            page.go_back()
                            page_wait_ms(page,1000)
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
                            page_wait_ms(page,300)
                        except Exception:
                            pass
                        try:
                            for i, frame in enumerate(page.frames):
                                try:
                                    frame.evaluate(DOWNLOAD_BUTTON_SCRIPT)
                                except Exception:
                                    pass
                            page.evaluate("() => !!window.__downloadBtnAttached")
                            page_wait_ms(page,800)
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
                            visual_auto_download_started_ref[0] = True
                            _log_stream_url(download_url, "download_click")
                            try:
                                LAST_DOWNLOAD_URL_FILE.write_text(download_url, encoding="utf-8")
                            except Exception:
                                pass
                            log("Stream URL saved to last_download_url.txt")
                            set_download_button_state("downloading")
                            log("Download started. You can close the browser; download will continue.")
                            out_path = DOWNLOAD_DIR / output_filename
                            stopped_by_user_ref[0] = False
                            download_proc_ref.clear()
                            dl_referer = stream_referer_for_download[0] or "https://supjav.com/"

                            def run_download():
                                try:
                                    _set_console_progress_prefix_for_download(
                                        output_filename,
                                        download_url,
                                        str(server_tab),
                                        video_code_ref[0],
                                    )
                                    try:
                                        result = download_video(
                                            download_url,
                                            out_path,
                                            referer=dl_referer,
                                            progress_callback=progress_from_download_thread,
                                            out_proc=download_proc_ref,
                                            stopped_by_user=stopped_by_user_ref,
                                        )
                                    finally:
                                        _clear_console_progress_prefix()
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
                            _dl_wait_manual = 40 if server_tab == "FST" else 20
                            if (
                                _event_wait_with_countdown(
                                    download_data_flowing,
                                    float(_dl_wait_manual),
                                    f"Waiting for download to start (max {_dl_wait_manual:.0f}s):",
                                )
                                or download_finished_ref[0]
                            ):
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
                        page_wait_ms(page,500)
                    on_player_page = current_url != page_url
                    if not on_player_page:
                        try:
                            if page.query_selector(
                                "iframe[src*='supremejav'], iframe[src*='dianaavoidthey'], iframe[src*='turbovid'], "
                                "iframe[src*='doppio'], iframe[src*='dood'], iframe[src*='d0011d'], iframe[src*='d0000d'], "
                                "iframe[src*='streamtape'], iframe[src*='streamta.'], iframe[src*='strtape']"
                            ):
                                on_player_page = True
                        except Exception:
                            pass
                    if (
                        on_player_page
                        and explicit_low_quality_seen_ref[0]
                        and not stream_url_for_download[0]
                        and not user_picked_single_tab
                    ):
                        _switch_to_next_fallback("only explicit <720 stream variants seen")
                    # Streamtape: only while SERVER tab is ST — after fallback to DS/TV/… the old ST embed URL
                    # can still sit in stream_url_for_download until the new player loads; do not re-run ST clicks.
                    if (
                        server_tab == "ST"
                        and on_player_page
                        and (not stream_url_for_download[0] or not _is_downloadable_stream_url(stream_url_for_download[0]))
                    ):
                        if not getattr(page, "_st_click_loop_done", False):
                            page._st_click_loop_done = True
                            _run_supjav_embed_resolve_click_loop(
                                page,
                                tab="ST",
                                stream_url_for_download=stream_url_for_download,
                                auto_download_pending_ref=auto_download_pending_ref,
                                max_wall_s=ST_STREAMTAPE_RESOLVE_MAX_S,
                                emit=log,
                            )
                    # DoodStream (DS): same capped iframe play / get_video loop as Streamtape ST.
                    if (
                        server_tab == "DS"
                        and on_player_page
                        and (not stream_url_for_download[0] or not _is_downloadable_stream_url(stream_url_for_download[0]))
                    ):
                        if not getattr(page, "_ds_click_loop_done", False):
                            page._ds_click_loop_done = True
                            # Quick pre-check: Turnstile already active before loop starts?
                            if _page_has_turnstile(page):
                                log("DS: Cloudflare Turnstile detected before embed loop — switching to next fallback tab")
                                _append_server_tab_debug("DS: Turnstile detected at pre-loop check — fast fallback")
                                _switch_to_next_fallback("DS: Cloudflare Turnstile in player (anti-bot)")
                            else:
                                _ds_turnstile_ref = [False]
                                _run_supjav_embed_resolve_click_loop(
                                    page,
                                    tab="DS",
                                    stream_url_for_download=stream_url_for_download,
                                    auto_download_pending_ref=auto_download_pending_ref,
                                    max_wall_s=ST_STREAMTAPE_RESOLVE_MAX_S,
                                    emit=log,
                                    turnstile_detected_ref=_ds_turnstile_ref,
                                )
                                if _ds_turnstile_ref[0]:
                                    log("DS: Cloudflare Turnstile blocked embed loop — switching to next fallback tab")
                                    _append_server_tab_debug("DS: Turnstile detected inside loop — fast fallback")
                                    _switch_to_next_fallback("DS: Cloudflare Turnstile in player (anti-bot)")
                    if not target_stream_seen_ref[0] and on_player_page:
                        auto_click_iters[0] += 1
                        if server_tab == "VOE" and not voe_click_loop_done_ref[0]:
                            # VOE: pattern — in loop do 2 clicks with 0.1s between them, then wait 2s; on each step check if stream appeared
                            voe_click_loop_done_ref[0] = True
                            _visual_log("auto_click_player: VOE — scroll up then 2-click pattern until stream appears (timeout ~20s)")
                            try:
                                page.evaluate("window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;")
                                page_wait_ms(page,400)
                            except Exception:
                                pass
                            def _have_downloadable_stream():
                                u = stream_url_for_download[0]
                                return bool(u and _is_downloadable_stream_url(u))

                            for attempt in range(10):  # ~10 * (2s + small overhead) ≈ 20 seconds
                                if _have_downloadable_stream():
                                    if not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                pass
                                # keep overlays clean before each click burst
                                for _ in range(2):
                                    try_close_ad_overlay(page)
                                    page_wait_ms(page,150)
                                # two clicks with 0.1s interval
                                clicked = False
                                for click_idx in range(2):
                                    if try_click_player(page):
                                        clicked = True
                                    page_wait_ms(page,100)
                                    if _have_downloadable_stream():
                                        break
                                pass
                                if _have_downloadable_stream():
                                    if not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                # wait 2 seconds before next burst
                                page_wait_ms(page,2_000)
                            # timeout: no downloadable stream within ~20 seconds — try DS (if tab exists), then TV once, then stop with error
                            if not _have_downloadable_stream():
                                if not voe_failed_try_tv_ref[0]:
                                    voe_failed_try_tv_ref[0] = True
                                    dismiss_ad_overlays(page)
                                    page_wait_ms(page,500)
                                    switched_from_voe = False
                                    if _ds_server_tab_visible(page):
                                        _visual_log("auto_click_player: VOE — timeout 20s, no stream; trying DS...")
                                        log("VOE: no stream within 20s, trying DS...")
                                        try:
                                            clicked_ds = page.evaluate("""() => {
                                                var adLike = /ads?\\b|popads|popcash|exoclick|propeller|goldensacam|purplesacam|aj2532\\.bid|altaffiliatesol|adclickad|t\\.me|adsterra|clickadu|hilltopads|onclkds|adsrvr/i;
                                                var btns = document.querySelectorAll('a.btn-server');
                                                for (var i = 0; i < btns.length; i++) {
                                                    var a = btns[i];
                                                    if ((a.textContent || a.innerText || '').trim().toUpperCase() !== 'DS') continue;
                                                    if (adLike.test((a.getAttribute('href') || '').trim())) continue;
                                                    a.scrollIntoView({ block: 'center' });
                                                    a.click();
                                                    return true;
                                                }
                                                return false;
                                            }""")
                                            if clicked_ds:
                                                page_wait_ms(page,3000)
                                                server_tab = "DS"
                                                auto_click_iters[0] = 5
                                                tv_click_loop_done_ref[0] = False
                                                switched_from_voe = True
                                        except Exception as e:
                                            log(f"Failed to switch to DS after VOE timeout: {e!r}")
                                    if not switched_from_voe:
                                        _visual_log("auto_click_player: VOE — timeout 20s, no stream; trying TV...")
                                        log("VOE: no stream within 20s, switching to TV...")
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
                                                page_wait_ms(page,3000)
                                                server_tab = "TV"
                                                auto_click_iters[0] = 5
                                            else:
                                                if skip_st:
                                                    log("TV tab not found; skip_st enabled, stopping with error.")
                                                    stop_event.set()
                                                else:
                                                    log("TV tab not found; trying ST fallback...")
                                                    # Try ST once before giving up.
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
                                                            page_wait_ms(page,3000)
                                                            server_tab = "ST"
                                                            auto_click_iters[0] = 5
                                                        else:
                                                            log("ST tab not found; stopping with error.")
                                                            stop_event.set()
                                                    except Exception as e:
                                                        log(f"Failed ST fallback after TV not found: {e!r}; stopping.")
                                                        stop_event.set()
                                        except Exception as e:
                                            log(f"Failed to switch to TV: {e!r}; stopping.")
                                            stop_event.set()
                                else:
                                    _visual_log("auto_click_player: VOE — timeout 60s, no stream found, stopping with error")
                                    log("VOE: no stream detected within 60 seconds; stopping with error.")
                                    stop_event.set()
                        elif server_tab in ("ST", "DS") and not tv_click_loop_done_ref[0] and auto_click_iters[0] >= 5:
                            # Streamtape / DoodStream: clicks + cap already done in _run_supjav_embed_resolve_click_loop.
                            # Do not duplicate generic auto_click_player 10s / 5s bursts (TV/FST only).
                            tv_click_loop_done_ref[0] = True
                            tab_timeout_s = int(ST_STREAMTAPE_RESOLVE_MAX_S)
                            usable_dl = bool(
                                stream_url_for_download[0]
                                and _is_downloadable_stream_url(stream_url_for_download[0])
                            )
                            if not usable_dl:
                                prev_tab = server_tab
                                _switch_to_next_fallback(
                                    f"{server_tab}: no downloadable stream after embed resolve (cap {tab_timeout_s}s)"
                                )
                                if server_tab == prev_tab:
                                    _visual_log(
                                        f"auto_click_player: {server_tab} — timeout ~{tab_timeout_s}s, "
                                        "embed/stream not playable or no further tab; stopping"
                                    )
                                    log(
                                        f"{server_tab}: no playable downloadable stream after wait; stopping."
                                    )
                                    stop_event.set()
                        elif server_tab not in ("VOE", "ST", "DS") and not tv_click_loop_done_ref[0] and auto_click_iters[0] >= 5:
                            # TV/FST: scroll + click pattern (~60s / FST wall). Not used for ST/DS (embed loop handles those).
                            tv_click_loop_done_ref[0] = True
                            if server_tab == "FST" and user_picked_single_tab:
                                tab_attempts = 4
                                tab_timeout_s = 40
                            elif server_tab == "FST":
                                tab_attempts = 3
                                tab_timeout_s = 40
                            else:
                                tab_attempts = 4
                                tab_timeout_s = 60
                            _visual_log(
                                f"auto_click_player: {server_tab} — scroll + click pattern until stream "
                                f"(~{tab_timeout_s}s per pass; FST hard cap {FST_TAB_MAX_WAIT_S:.0f}s wall clock)"
                            )
                            for attempt in range(tab_attempts):  # ~15s per attempt
                                if server_tab == "FST" and _fst_wall_timed_out():
                                    _visual_log(
                                        f"auto_click_player: FST — wall timeout {FST_TAB_MAX_WAIT_S:.0f}s, ending click loop"
                                    )
                                    break
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
                                        page_wait_ms(page,1500)
                                    except Exception:
                                        pass
                                    continue
                                try:
                                    page.evaluate("window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;")
                                    page_wait_ms(page,400)
                                except Exception:
                                    pass
                                for _ in range(3):
                                    try_close_ad_overlay(page)
                                    page_wait_ms(page,300)
                                if _auto_click_player_for_tab(page, server_tab):
                                    timeline("auto_click_player")
                                    _visual_log(f"auto_click_player: {server_tab} attempt {attempt + 1} click 1")
                                page_wait_ms(page,500)
                                if server_tab in ("TV", "FST"):
                                    # FST: shorter wait — burst already fired several hits; long idle delays startup.
                                    page_wait_ms(
                                        page,
                                        4_000 if server_tab == "FST" else 10_000,
                                        intro=(
                                            f"auto_click_player: {server_tab} post-click wait "
                                            f"({'4s' if server_tab == 'FST' else '10s'}):"
                                        ),
                                    )
                                    current_url = page.url
                                    if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS) or not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                                        _visual_log(f"auto_click_player: {server_tab} — forbidden site after wait, going back")
                                        try:
                                            page.go_back()
                                            page_wait_ms(page,1500)
                                        except Exception:
                                            pass
                                        continue
                                    if _auto_click_player_for_tab(page, server_tab):
                                        timeline(f"auto_click_player_second_{server_tab.lower()}")
                                        _visual_log(f"auto_click_player: {server_tab} attempt {attempt + 1} click 2")
                                    page_wait_ms(page,2_000)
                                    current_url = page.url
                                    if any(dom in current_url for dom in BLOCKED_REDIRECT_DOMAINS) or not any(dom in current_url for dom in ALLOWED_MAIN_DOMAINS):
                                        try:
                                            page.go_back()
                                            page_wait_ms(page,1500)
                                        except Exception:
                                            pass
                                        continue
                                    if _auto_click_player_for_tab(page, server_tab):
                                        timeline(f"auto_click_player_third_{server_tab.lower()}")
                                        _visual_log(f"auto_click_player: {server_tab} attempt {attempt + 1} click 3")
                                    page_wait_ms(page,500)
                                if target_stream_seen_ref[0] or stream_url_for_download[0]:
                                    if stream_url_for_download[0] and _is_downloadable_stream_url(stream_url_for_download[0]) and not auto_download_pending_ref[0]:
                                        auto_download_pending_ref[0] = True
                                    break
                                if explicit_low_quality_seen_ref[0] and not stream_url_for_download[0]:
                                    if user_picked_single_tab:
                                        _visual_log(
                                            f"auto_click_player: {server_tab} — explicit <720 seen; "
                                            f"keeping tab (single-tab mode), waiting for usable stream"
                                        )
                                    else:
                                        _visual_log(f"auto_click_player: {server_tab} — only explicit <720 seen; switching to next fallback")
                                        break
                                if not target_stream_seen_ref[0]:
                                    if _auto_click_player_for_tab(page, server_tab):
                                        _visual_log("auto_click_player: extra click (stream not found)")
                                    page_wait_ms(page,300)
                                    page_wait_ms(
                                        page,
                                        5_000,
                                        intro=f"auto_click_player: {server_tab} extra wait for stream (5s):",
                                    )
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
                            usable_dl = bool(
                                stream_url_for_download[0]
                                and _is_downloadable_stream_url(stream_url_for_download[0])
                            )
                            if server_tab == "FST":
                                if not usable_dl:
                                    prev_tab = server_tab
                                    _switch_to_next_fallback(
                                        f"FST: no downloadable stream after wait (~{tab_timeout_s}s / "
                                        f"{FST_TAB_MAX_WAIT_S:.0f}s cap)"
                                    )
                                    _arm_fst_wall_deadline_if_needed()
                                    if server_tab == prev_tab:
                                        _visual_log(
                                            f"auto_click_player: FST — no downloadable stream within "
                                            f"~{tab_timeout_s}s or {FST_TAB_MAX_WAIT_S:.0f}s wall clock; stopping"
                                        )
                                        log(
                                            f"FST: no downloadable stream after auto-click / timeout; stopping."
                                        )
                                        stop_event.set()
                            elif server_tab == "TV" and not usable_dl:
                                prev_tab = server_tab
                                _switch_to_next_fallback(
                                    f"{server_tab}: no downloadable stream after wait (~{tab_timeout_s}s)"
                                )
                                if server_tab == prev_tab:
                                    _visual_log(
                                        f"auto_click_player: {server_tab} — timeout ~{tab_timeout_s}s, "
                                        "embed/stream not playable or no further tab; stopping"
                                    )
                                    log(
                                        f"{server_tab}: no playable downloadable stream after wait; stopping."
                                    )
                                    stop_event.set()
                            elif not target_stream_seen_ref[0] and not stream_url_for_download[0]:
                                prev_tab = server_tab
                                _switch_to_next_fallback(f"no stream within {tab_timeout_s}s")
                                if server_tab == prev_tab:
                                    _visual_log(f"auto_click_player: {server_tab} — timeout ~{tab_timeout_s}s, no stream found, stopping with error")
                                    log(f"{server_tab}: no stream detected within ~{tab_timeout_s} seconds; stopping with error.")
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
                        _proc_wait_with_countdown(proc, 3600.0, "Download subprocess (max 3600s):")
                except Exception:
                    pass
                log("Done.")
            if download_thread_ref[0]:
                _thread_join_with_countdown(
                    download_thread_ref[0],
                    3700.0,
                    "Download thread join (max 3700s):",
                )
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
    total = ""
    tm = re.search(r"\bof\s+([0-9]*\.?[0-9]+\s*[KMG]i?B)\b", line, re.I)
    if tm:
        total = tm.group(1).replace(" ", "")
    sm = re.search(r"at\s+([^\s]+)", line)
    if sm:
        speed = sm.group(1).strip()
    em = re.search(r"ETA\s+([^\s]+)", line)
    if em:
        eta = em.group(1).strip()
    parts: list[str] = [f"{pct}%"]
    if total:
        parts.append(f"of {total}")
    if speed:
        parts.append(speed)
    if eta:
        parts.append(f"ETA {eta}")
    return " · ".join(parts)


def resolve_streamtape_direct_url(embed_url: str, referer: str = "https://supjav.com/") -> str | None:
    """Open Streamtape embed page (/e/...), get direct video URL from DOM (get_video) or network, return it.
    yt-dlp does not support streamtape; we need the direct URL for generic download."""
    if not _is_streamtape_like_url(embed_url) or "/e/" not in embed_url:
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
            page_wait_ms(page, 5000, intro="embed resolve: after load before play click (5s):")
            try:
                page.locator("video").first.click(force=True, timeout=5000)
            except Exception:
                try:
                    page.locator("body").first.click(force=True, timeout=2000)
                except Exception:
                    pass
            page_wait_ms(page, 8000, intro="embed resolve: wait for video/get_video URLs (8s):")
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
    """Download a direct video URL via HTTP (urllib), with progress reporting and retry on connection drop."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    max_retries = 5
    for attempt in range(max_retries):
        if attempt > 0:
            print(f"Retry attempt {attempt}/{max_retries - 1}...")
            sys.stdout.flush()
        
        start_byte = 0
        if output_path.exists():
            start_byte = output_path.stat().st_size
        if start_byte > 0:
            print(f"Resuming from {start_byte} bytes ({start_byte / (1024*1024):.1f} MB)")
            sys.stdout.flush()
        else:
            print(f"Downloading direct: {url[:100]}")
            sys.stdout.flush()
        try:
            headers = {"User-Agent": USER_AGENT, "Referer": referer}
            if start_byte > 0:
                headers["Range"] = f"bytes={start_byte}-"
            req = urllib.request.Request(url, headers=headers)
            # Use socket timeout to detect stalls (no data for 30s)
            resp = urllib.request.urlopen(req, timeout=600)
            if hasattr(resp, 'fp') and hasattr(resp.fp, '_sock'):
                resp.fp._sock.setsockopt(__import__('socket').SOL_SOCKET, __import__('socket').SO_RCVTIMEO, 30000)
            status = getattr(resp, "status", 200)
            if status == 206:
                # Partial content — append to existing file
                content_length = int(resp.headers.get("Content-Length", 0))
                content_range = resp.headers.get("Content-Range", "")
                total = start_byte + content_length
                if content_range and "/" in content_range:
                    try:
                        total = int(content_range.split("/")[1].strip())
                    except (ValueError, IndexError):
                        pass
                mode = "ab"
            elif status == 200:
                # Server ignored Range — must re-download from start
                resp.close()
                start_byte = 0
                headers_pop = {k: v for k, v in headers.items() if k.lower() != "range"}
                req = urllib.request.Request(url, headers=headers_pop)
                resp = urllib.request.urlopen(req, timeout=600)
                if hasattr(resp, 'fp') and hasattr(resp.fp, '_sock'):
                    resp.fp._sock.setsockopt(__import__('socket').SOL_SOCKET, __import__('socket').SO_RCVTIMEO, 30000)
                total = int(resp.headers.get("Content-Length", 0))
                content_length = total
                mode = "wb"
            elif status in (403, 404, 416):
                resp.close()
                print(f"Download failed: server returned {status} (partial file kept)")
                sys.stdout.flush()
                return False
            else:
                resp.close()
                print(f"Download failed: unexpected status {status}")
                sys.stdout.flush()
                return False

            chunk_size = 256 * 1024
            start_time = time.time()
            downloaded = start_byte
            written_this_session = 0
            bar_width = 30
            last_rendered_pct: int | None = None
            last_print_time = 0.0
            last_line_len = 0

            def _print_progress_line(line: str) -> None:
                """Print progress in a single console line (overwrite with CR)."""
                nonlocal last_line_len
                full = _prefixed_console_progress_line(line)
                pad = " " * max(0, last_line_len - len(full))
                print("\r" + full + pad, end="", flush=True)
                last_line_len = len(full)

            # Streamtape (ST) tends to fluctuate; show speed/ETA based on the last N seconds
            # rather than averaging from the beginning of the session.
            is_streamtape_like = (
                _is_streamtape_like_url(url)
                or "tapecontent" in url.lower()
                or _is_streamtape_like_url(referer or "")
            )
            speed_window_seconds = 5 * 60
            speed_window_samples: deque[tuple[float, int]] = deque()
            if is_streamtape_like:
                speed_window_samples.append((start_time, downloaded))
            with open(output_path, mode) as f:
                while True:
                    if stopped_by_user and stopped_by_user[0]:
                        return True
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    written_this_session += len(chunk)
                    downloaded = start_byte + written_this_session
                    now = time.time()
                    elapsed = now - start_time
                    if total > 0:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        speed_mbs = 0.0
                        eta_str = ""
                        rate_bps = 0.0

                        if is_streamtape_like and speed_window_samples is not None:
                            # Sample at most once per second to keep deque small.
                            if not speed_window_samples or (now - speed_window_samples[-1][0]) >= 1.0:
                                speed_window_samples.append((now, downloaded))
                            cutoff = now - speed_window_seconds
                            while speed_window_samples and speed_window_samples[0][0] < cutoff:
                                speed_window_samples.popleft()

                            if len(speed_window_samples) >= 2:
                                t0, b0 = speed_window_samples[0]
                                dt = now - t0
                                db = downloaded - b0
                                if dt > 0 and db > 0:
                                    rate_bps = db / dt
                                    speed_mbs = (db / (1024 * 1024)) / dt
                        else:
                            speed_mbs = (written_this_session / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                            rate_bps = written_this_session / elapsed if elapsed > 0 else 0

                        if speed_mbs > 0 and downloaded < total and rate_bps > 0:
                            eta_sec = int((total - downloaded) / rate_bps)
                            eta_str = f"{eta_sec // 3600}:{(eta_sec % 3600) // 60:02d}:{eta_sec % 60:02d}" if eta_sec >= 3600 else f"{eta_sec // 60}:{eta_sec % 60:02d}"
                            msg = f"{pct}% ({mb:.1f}/{total_mb:.1f} MB) {speed_mbs:.2f} MB/s ETA {eta_str}"
                        else:
                            msg = f"{pct}% ({mb:.1f}/{total_mb:.1f} MB)"
                        # Console single-line progress bar
                        should_render = (last_rendered_pct != int(pct)) or (now - last_print_time >= 0.5)
                        if should_render:
                            filled = int(pct * bar_width / 100)
                            filled = max(0, min(bar_width, filled))
                            bar = "#" * filled + "-" * (bar_width - filled)
                            if eta_str:
                                line = f"[{bar}] {pct}% {mb:.1f}/{total_mb:.1f} MB {speed_mbs:.2f} MB/s ETA {eta_str}"
                            else:
                                line = f"[{bar}] {pct}% {mb:.1f}/{total_mb:.1f} MB"
                            _print_progress_line(line)
                            last_rendered_pct = int(pct)
                            last_print_time = now
                    else:
                        mb = downloaded / (1024 * 1024)
                        speed_mbs = mb / elapsed if elapsed > 0 else 0
                        msg = f"{mb:.1f} MB downloaded" + (f" {speed_mbs:.2f} MB/s" if speed_mbs > 0 else "")
                        if now - last_print_time >= 1.0:
                            _print_progress_line(f"Downloaded {mb:.1f} MB ({speed_mbs:.2f} MB/s)")
                            last_print_time = now
                    if progress_callback:
                        try:
                            progress_callback(msg)
                        except Exception:
                            pass
            resp.close()
            # Finish overwriting line with a newline so next output doesn't share the same line.
            print()
            print(f"Download completed ({downloaded / (1024*1024):.1f} MB).")
            sys.stdout.flush()
            return True
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"Connection interrupted at {downloaded / (1024*1024):.1f} MB: {e}")
            sys.stdout.flush()
            if attempt < max_retries - 1:
                print(f"Will retry (attempt {attempt + 1}/{max_retries - 1})...")
                sys.stdout.flush()
                _stderr_sleep_countdown(2.0, "Wait before retry:")
                continue
            else:
                return False
        except Exception as e:
            print(f"Download failed: {e}")
            sys.stdout.flush()
            return False
    
    print("Download failed after all retry attempts.")
    sys.stdout.flush()
    return False


def _yt_dlp_suppress_log_line(line: str) -> bool:
    """True if this yt-dlp log line should not be printed (noisy extractor chatter)."""
    if not line:
        return False
    return bool(re.match(r"\s*\[generic\]", line, re.I))


def _find_downloaded_output_file(output_path: Path) -> Path | None:
    """Best-effort resolution of the final downloaded file path for size reporting."""
    if output_path.exists():
        return output_path
    try:
        # If output path has no suffix, yt-dlp may create output_path + ".ext"
        if not output_path.suffix:
            candidates = list(output_path.parent.glob(output_path.name + ".*"))
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
                return candidates[0]
        else:
            # Fallback: same stem with any extension
            candidates = list(output_path.parent.glob(output_path.stem + ".*"))
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
                return candidates[0]
    except Exception:
        pass
    return None


def _report_final_file_size(output_path: Path, progress_callback: Callable[[str], None] | None = None) -> None:
    """Print final downloaded file size and notify callback if present."""
    final_file = _find_downloaded_output_file(output_path)
    if not final_file or not final_file.exists():
        return
    try:
        size_mb = final_file.stat().st_size / (1024 * 1024)
        msg = f"Final file size: {size_mb:.1f} MB ({final_file.name})"
        _stderr_line(msg)
        if progress_callback is not None:
            try:
                progress_callback(msg)
            except Exception:
                pass
    except Exception:
        pass


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
    streamtape_retry_url = None
    if _is_streamtape_like_url(url) and "get_video" in url.lower():
        streamtape_retry_url = url
        if progress_callback:
            try:
                progress_callback("Resolving Streamtape get_video redirect...")
            except Exception:
                pass
        _stderr_line("Streamtape get_video URL, resolving redirect...")
        final = _follow_redirect_to_video(url, referer="https://streamtape.com/")
        if final and final != url:
            _stderr_line(f"Resolved to CDN: {final[:120]}")
            url = final
        else:
            _stderr_line("Could not resolve get_video redirect, using as-is")
    elif _is_streamtape_like_url(url) and "/e/" in url:
        streamtape_retry_url = url
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
            _stderr_line("Using direct Streamtape URL for download.")
        else:
            _stderr_line("Could not resolve Streamtape direct URL, trying original.")
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dl_referer = referer
    if _is_streamtape_like_url(url) or "tapecontent" in url.lower():
        dl_referer = "https://streamtape.com/"
    # Direct HTTP download for CDN URLs (tapecontent.net etc.) — yt-dlp hangs on these
    if "tapecontent" in url.lower() or (url.lower().endswith(".mp4") and "get_video" not in url.lower()):
        ok = _download_direct_http(url, output_path, dl_referer, progress_callback, stopped_by_user)
        if ok:
            return True
        # Some tapecontent hosts timeout from direct urllib path; retry via yt-dlp on original Streamtape URL.
        if streamtape_retry_url:
            _stderr_line("Direct CDN download failed, retrying via yt-dlp on Streamtape URL...")
            url = streamtape_retry_url
        else:
            return False
    # yt-dlp branch: if the output file already exists, try to resume it.
    # (yt-dlp `--continue` uses the existing file state where possible.)
    try:
        if output_path.exists() and output_path.stat().st_size > 0:
            existing_mb = output_path.stat().st_size / (1024 * 1024)
            _stderr_line(f"Existing file found ({existing_mb:.1f} MB); attempting resume...")
    except Exception:
        pass
    if output_path.suffix:
        out_arg = str(output_path)
    else:
        out_arg = str(output_path.with_suffix("")) + ".%(ext)s"
    # Extract Origin from referer for CORS-protected CDNs (VOE/edgeon-bandwidth)
    dl_origin = None
    if dl_referer:
        try:
            from urllib.parse import urlparse
            p = urlparse(dl_referer)
            dl_origin = f"{p.scheme}://{p.netloc}"
        except Exception:
            pass
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--newline",
        "--no-part",
        "--continue",
        "--retries", str(DEFAULT_DOWNLOAD_RETRIES),
        "--fragment-retries", str(DEFAULT_DOWNLOAD_RETRIES),
        "--add-header", f"Referer:{dl_referer}",
        "--user-agent", USER_AGENT,
    ]
    if dl_origin:
        cmd += ["--add-header", f"Origin:{dl_origin}"]
    cmd += ["-o", out_arg, url]
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
            stderr_activity_notified = [False]

            def read_stderr():
                for line in proc.stderr:
                    if _yt_dlp_suppress_log_line(line):
                        continue
                    # Some extractors (notably FST/HLS via ffmpeg) report activity mostly in stderr.
                    # Notify caller once so "waiting for data flow" does not false-timeout.
                    if (
                        not stderr_activity_notified[0]
                        and progress_callback is not None
                        and (
                            "frame=" in line
                            or "Input #0, hls" in line
                            or "Opening '" in line
                            or "Destination:" in line
                        )
                    ):
                        try:
                            progress_callback("0% · starting")
                            stderr_activity_notified[0] = True
                        except Exception:
                            pass
                    print(line, end="", file=sys.stderr)

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            bar_width = 30
            last_line_len = 0

            def _print_one_line(line: str) -> None:
                nonlocal last_line_len
                full = _prefixed_console_progress_line(line)
                pad = " " * max(0, last_line_len - len(full))
                print("\r" + full + pad, end="", flush=True, file=sys.stderr)
                last_line_len = len(full)

            for line in proc.stdout:
                parsed = _parse_ytdlp_progress(line)
                if parsed:
                    # Update UI callback with the parsed yt-dlp info.
                    if progress_callback is not None:
                        try:
                            progress_callback(parsed)
                        except Exception:
                            pass

                    # Render one-line progress bar in console.
                    m = re.search(r"(\d+\.?\d*)%\s*(?:of\s|\s|$)", line)
                    if m:
                        pct_float = float(m.group(1))
                        pct_int = max(0, min(100, int(pct_float)))
                        filled = int(pct_int * bar_width / 100)
                        filled = max(0, min(bar_width, filled))
                        bar = "#" * filled + "-" * (bar_width - filled)
                        # Strip leading "XX.X% · " from parsed to avoid duplication.
                        suffix = parsed
                        m2 = re.match(r"^\s*\d+\.?\d*%\s*·\s*(.*)$", parsed)
                        if m2:
                            suffix = m2.group(1).strip()
                        _print_one_line(f"[{bar}] {pct_int}% {suffix}".rstrip())
                    continue

                # Non-progress yt-dlp output: print normally.
                if _yt_dlp_suppress_log_line(line):
                    continue
                print(line, end="", file=sys.stderr)
            # Ensure the progress bar line ends with newline.
            if last_line_len:
                print(file=sys.stderr)
            stderr_thread.join(timeout=0.5)
            try:
                proc.wait(timeout=600.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                _stderr_line("Download failed (timeout)")
                return False
            if stopped_by_user and stopped_by_user[0]:
                return True
            if proc.returncode != 0:
                _stderr_line(f"Download failed (exit {proc.returncode})")
                return False
            _report_final_file_size(output_path, progress_callback)
            return True
        # Same as above but without progress parsing (still filter noisy [generic] lines).
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc2 = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        def read_stderr2():
            assert proc2.stderr is not None
            for line in proc2.stderr:
                if _yt_dlp_suppress_log_line(line):
                    continue
                print(line, end="", file=sys.stderr)

        t2 = threading.Thread(target=read_stderr2, daemon=True)
        t2.start()
        assert proc2.stdout is not None
        for line in proc2.stdout:
            if _yt_dlp_suppress_log_line(line):
                continue
            print(line, end="", file=sys.stderr)
        t2.join(timeout=0.5)
        try:
            proc2.wait(timeout=600.0)
        except subprocess.TimeoutExpired:
            proc2.kill()
            proc2.wait()
            _stderr_line("Download failed (timeout)")
            return False
        if proc2.returncode != 0:
            _stderr_line(f"Download failed (exit {proc2.returncode})")
            return False
        _report_final_file_size(output_path, progress_callback)
        return True
    except subprocess.CalledProcessError as e:
        _stderr_line(f"Download failed (exit {e.returncode}): {e.stderr or e.stdout or str(e)}")
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _stderr_line(f"Download failed: {e}")
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
        help="Server tab priority (comma-separated): FST,VOE,TV,ST,DS (default: FST,VOE,TV,ST; DS auto-inserted when tab exists), or single tab to stay on that tab",
    )
    parser.add_argument(
        "--skip-st",
        action="store_true",
        help="Skip any ST tab attempts in fallback chain and default VOE order.",
    )
    parser.add_argument(
        "--progress-slug",
        metavar="SLUG",
        default="",
        help='Actress/folder label in progress line: "SLUG: CODE (TAB) …" when used with --progress-code.',
    )
    parser.add_argument(
        "--progress-code",
        metavar="CODE",
        default="",
        help='Movie code in progress line (e.g. JUX-203); default from page title if omitted.',
    )
    parser.add_argument(
        "--progress-tab",
        metavar="TAB",
        default="",
        help='Server tab in parentheses (e.g. FST); default: first -s entry or VOE; refined from URL when ST/VOE.',
    )
    args = parser.parse_args()

    if args.visual:
        ok = run_visual_mode(
            args.url,
            auto_download=not getattr(args, "no_auto_download", False),
            output_filename=args.output,
            server_tab=args.server_tab,
            skip_st=getattr(args, "skip_st", False),
        )
        return 0 if ok else 1

    try:
        urls, video_code = extract_stream_urls(
            args.url,
            server_tabs=[args.server_tab],
            for_download=args.download,
        )
    except Exception as e:
        _stderr_line(f"Error: {e}")
        return 1

    if not urls:
        _stderr_line("Stream URLs not found")
        return 1

    if args.download:
        # VOE default: prefer supremejav/turbovidhls so we do not grab a random doppiocdn playlist.
        # FST/ST/TV/DS: streams are on CDN (e.g. doppiocdn / dood m3u8) — allow those URLs.
        prefer_voe = str(args.server_tab).upper() == "VOE"
        download_url = get_downloadable_url(urls, prefer_voe_player=prefer_voe, video_code=video_code)
        if not download_url:
            _stderr_line("No downloadable URL (only blob: found). Cannot download.")
            return 1
        # If we got a player page (not m3u8), open it and get m3u8 (supremejav or turbovidhls)
        if ".m3u8" not in download_url.lower() and (
            "supremejav" in download_url or "turbovidhls.com/t/" in download_url
        ):
            label = "RBD-764" if video_code else "VOE player"
            _stderr_line(f"Opening VOE player page to get stream URL ({label})...")
            m3u8_url = extract_m3u8_from_player_page(download_url)
            if m3u8_url:
                download_url = m3u8_url
            else:
                _stderr_line("Could not get stream from VOE player.")
                return 1
        _stderr_line(f"Downloading from: {download_url}")
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = DOWNLOAD_DIR / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _set_console_progress_prefix_for_download(
            args.output,
            download_url,
            str(args.server_tab),
            video_code,
            progress_slug=getattr(args, "progress_slug", "") or "",
            progress_code=getattr(args, "progress_code", "") or "",
            progress_tab=getattr(args, "progress_tab", "") or "",
        )
        try:
            if download_video(download_url, out_path, referer="https://supjav.com/"):
                _stderr_line(f"Saved to: {out_path}")
                return 0
            return 1
        finally:
            _clear_console_progress_prefix()

    for u in urls:
        print(u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
