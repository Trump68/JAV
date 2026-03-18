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

| Параметр | Короткий | Описание |
|---|---|---|
| `url` | | URL страницы (по умолчанию тестовый) |
| `--download` | `-d` | Скачать видео в `download/` (headless) |
| `--output FILE` | `-o` | Путь выходного файла (по умолчанию `video.m4v`) |
| `--visual` | `-v` | Визуальный режим с окном браузера |
| `--no-auto-download` | | Не начинать скачивание автоматически (только с `--visual`) |
| `--server-tab TAB` | `-s` | Вкладка сервера: `VOE` (по умолчанию, пробует VOE → TV → ST), `ST`, `TV`, `FST` |

**Только вывести URL потока (страница по умолчанию):**

```bash
python dodnld.py
```

**Указать свой URL страницы:**

```bash
python dodnld.py "https://supjav.com/411204.html"
```

**Скачать видео (headless, в `download/`):**

```bash
python dodnld.py "https://supjav.com/411204.html" --download
python dodnld.py "https://supjav.com/411204.html" -d -o video.m4v
python dodnld.py "https://supjav.com/411204.html" -d -o RBD-764/RBD-764.m4v
```

**Визуальный режим (браузер, кнопка Download, авто-загрузка):**

```bash
python dodnld.py "https://supjav.com/411204.html" --visual
python dodnld.py "https://supjav.com/411204.html" -v -o my_video.m4v
```

**Визуальный режим без авто-загрузки:**

```bash
python dodnld.py "https://supjav.com/411204.html" -v --no-auto-download
```

**Выбор вкладки сервера:**

```bash
python dodnld.py "https://supjav.com/411204.html" -v -s ST
python dodnld.py "https://supjav.com/411204.html" -v -s TV
python dodnld.py "https://supjav.com/411204.html" -v --server-tab FST
```

При VOE — автоматически пробует VOE → TV → ST при неудаче. При указании ST или TV — если скачивание падает, переключается на ST как fallback.

---

### get_title.py — заголовок, постер, пакетная загрузка

| Параметр | Короткий | Описание |
|---|---|---|
| `url` | | URL видео-страницы или страницы актрисы |
| `--cast-list` | | Собрать LIST.TXT со всех страниц актрисы |
| `--process-list SLUG` | | Обработать `download/{SLUG}/LIST.TXT` — скачать фильмы |
| `--visual` | `-v` | Вызывать dodnld.py с окном браузера (по умолчанию вкл.) |
| `--no-visual` | | Headless режим |
| `--redownload` | | С `--process-list`: скачивать даже если фильм уже в БД |
| `--censored` | | С `--process-list`: обрабатывать записи с пустыми labels (censored) |

**Одна страница — получить title/code/cast, скачать, сохранить постер:**

```bash
python get_title.py "https://supjav.com/411204.html"
```

**Собрать список фильмов актрисы (обход всех страниц каста):**

```bash
python get_title.py "https://supjav.com/category/cast/kijima-airi" --cast-list
python get_title.py "https://supjav.com/category/cast/kasumi-risa" --cast-list --visual
```

**Обработать LIST.TXT — скачать все Reducing Mosaic фильмы:**

```bash
python get_title.py --process-list kijima-airi
```

Создаёт папки вида `download/kijima-airi/EBOD-723 UNC [2023.08.09]/EBOD-723_UNCENSORED.m4v`.

**Обработать LIST.TXT — скачать censored фильмы (с пустыми labels):**

```bash
python get_title.py --process-list kijima-airi --censored
```

Создаёт папки вида `download/kijima-airi/EBOD-723 C [2023.08.09]/EBOD-723.m4v`.

**Пере-скачать (даже если уже есть в БД):**

```bash
python get_title.py --process-list kijima-airi --redownload
python get_title.py --process-list kijima-airi --censored --redownload
```

## How it works (dodnld.py)

- Opens the given URL in headless (or visible with `--visual`) Chromium/Chrome.
- Intercepts network requests and keeps any that look like streams (m3u8, mp4, player/embed paths).
- Clicks the chosen server tab (VOE, TV, ST, FST) to load the player, then waits for iframe/video.
- For Streamtape: auto-clicks play inside the iframe, captures `get_video` URL, resolves CDN redirect, downloads via direct HTTP.
- For VOE/TV: downloads via yt-dlp. If download fails (DNS, timeout), automatically falls back to ST.
- Waits for download to confirm data flow before closing the browser.
- Collects URLs from the DOM: `iframe[src]`, `video` / `source[src]`, `[data-src]`, and from HTML/scripts.
- Filters out ad/analytics domains and outputs unique stream URLs.

## How it works (get_title.py)

- **Default mode:** extracts title, code, cast, cover image from a video page, then calls dodnld.py.
- **`--cast-list`:** walks all pagination pages of an actress, saves URLs/codes/dates/labels to LIST.TXT.
- **`--process-list`:** reads LIST.TXT, filters by label (`Reducing Mosaic` or empty for `--censored`), downloads each film, validates with ffprobe, saves to SQLite DB.

## Notes

- The target site may change its layout or script logic; selectors and filters might need updates.
- Respect the site's terms of use and robots.txt when using this tool.
