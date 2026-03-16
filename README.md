# Supjav stream URL extractor

Python scripts that open Supjav video pages, load the player (by switching server tab if needed), and extract streaming video URLs (m3u8, mp4, iframe embed URLs, etc.). Optional download via yt-dlp and visual mode with browser.

## Requirements

- Python 3.8+
- Chromium (устанавливается через Playwright) или **Google Chrome** (рекомендуется)

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

3. Браузер:
   - **Рекомендуется:** установить [Google Chrome](https://www.google.com/chrome/) — скрипты автоматически используют его вместо Chromium (меньше палятся антиботы и Cloudflare).
   - Либо только Chromium для Playwright:
   ```bash
   python -m playwright install chromium
   ```

**Почему Chromium «плохо работает»:** встроенный Chromium Playwright легче детектируется сайтами и Cloudflare. Если страница не грузится или постоянно проверка «Verifying you are human» — установите Chrome или запускайте с `--visual` (браузер с окном ведёт себя естественнее).

## Usage

### dodnld.py — извлечение URL и загрузка

**Только вывести URL потока (страница по умолчанию):**

```bash
python dodnld.py
```

**Указать свой URL страницы:**

```bash
python dodnld.py "https://supjav.com/411204.html"
```

**Скачать видео в текущую папку (headless, в `download/`):**

```bash
python dodnld.py "https://supjav.com/411204.html" --download
```

**Скачать в указанный файл:**

```bash
python dodnld.py "https://supjav.com/411204.html" -d -o video.m4v
python dodnld.py "https://supjav.com/411204.html" --download --output RBD-764/RBD-764.m4v
```

**Визуальный режим (открыть браузер, кнопка Download, авто-загрузка при появлении потока):**

```bash
python dodnld.py "https://supjav.com/411204.html" --visual
python dodnld.py "https://supjav.com/411204.html" -v -o my_video.m4v
```

**Визуальный режим без авто-загрузки (только показывать URL, загрузку запускать вручную):**

```bash
python dodnld.py "https://supjav.com/411204.html" --visual --no-auto-download
```

**Выбор вкладки сервера (VOE по умолчанию, можно ST, TV, FST):**

```bash
python dodnld.py "https://supjav.com/411204.html" --server-tab VOE
python dodnld.py "https://supjav.com/411204.html" -s ST --visual
```

Скрипт выводит по одному URL потока на строку. Если URL не найдены — пишет "Stream URLs not found" в stderr и завершается с кодом 1.

---

### get_title.py — заголовок, постер и вызов dodnld

**Одна страница: получить title/code/cast, скачать через dodnld (визуальный режим), сохранить в `download/{CODE}/` и POSTER.jpg:**

```bash
python get_title.py "https://supjav.com/411204.html"
```

**Страница по умолчанию:**

```bash
python get_title.py
```

**Режим списка актрисы: обойти все страницы каста и сохранить `download/{CAST_SLUG}/LIST.TXT`:**

```bash
python get_title.py "https://supjav.com/category/cast/kijima-airi" --cast-list
```

**Обработать LIST.TXT: для каждой записи с меткой "Reducing Mosaic" запустить dodnld и сохранить в папку актрисы:**

```bash
python get_title.py --process-list kijima-airi
```

## How it works (dodnld.py)

- Opens the given URL in headless (or visible with `--visual`) Chromium.
- Intercepts network requests and keeps any that look like streams (m3u8, mp4, player/embed paths).
- Clicks the chosen server tab (VOE, TV, ST, FST) to load the player, then waits for iframe/video.
- Collects URLs from the DOM: `iframe[src]`, `video` / `source[src]`, `[data-src]`, and from HTML/scripts.
- Filters out ad/analytics domains and outputs unique stream URLs. With `--download` uses yt-dlp to save video.

## Notes

- The target site may change its layout or script logic; selectors and filters might need updates.
- Respect the site's terms of use and robots.txt when using this tool.
