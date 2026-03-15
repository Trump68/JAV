# Supjav stream URL extractor

Python script that opens a Supjav video page, loads the player (by switching server tab if needed), and outputs all discovered streaming video URLs (m3u8, mp4, iframe embed URLs, etc.).

## Requirements

- Python 3.8+
- Chromium (installed via Playwright)

## Setup

1. Create a virtual environment (recommended):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   # or: source .venv/bin/activate   # Linux/macOS
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Install Playwright's Chromium browser:

   ```bash
   python -m playwright install chromium
   ```

## Usage

Run with the default page (https://supjav.com/403831.html):

```bash
python main.py
```

Run with a custom page URL:

```bash
python main.py "https://supjav.com/123456.html"
```

The script prints one streaming URL per line. If no URLs are found, it prints "Stream URLs not found" to stderr and exits with code 1.

## How it works

- Opens the given URL in a headless Chromium window.
- Intercepts network requests and keeps any that look like streams (m3u8, mp4, player/embed paths).
- Tries to click the first server tab (TV, FST, ST, or VOE) to load the player, then waits for an iframe or video element.
- Collects URLs from the DOM: `iframe[src]`, `video` / `source[src]`, and `[data-src]`.
- Filters out common ad/analytics domains and outputs unique stream URLs.

## Notes

- The target site may change its layout or script logic; selectors and filters might need updates.
- Respect the site's terms of use and robots.txt when using this tool.
