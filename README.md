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

**Режим одной видео-страницы:** `url` — страница ролика; скрипт берёт title/code/cast, постер и вызывает `dodnld.py`.

**Режим `--cast-list`:** `url` — страница актрисы (`https://supjav.com/category/cast/{SLUG}`). Обход пагинации; результат **`download/{SLUG}/LIST.TXT`** (последний сегмент пути = `SLUG`).

**Режим `--process-list SLUG`:** читает **`download/{SLUG}/LIST.TXT`**. Без `--censored` обрабатываются строки, где в labels есть **Reducing Mosaic** (папка `CODE UNC [дата]/`, файл `CODE_UNCENSORED.m4v`) или **Uncensored Leak** (папка `CODE LKD [дата]/`, файл `CODE_LEAKED.m4v`, например `WANZ-377 LKD [2023.06.24]` / `WANZ-377_LEAKED.m4v`). С `--censored` — строки с пустым label (`… C [дата]/`, `CODE.m4v`). Уже скачанное — в SQLite **`downloads.db`**; `--redownload` заставляет качать снова. `--no-visual` — вызывать `dodnld.py` без окна браузера.

Примеры с **`--cast-list`** (создать `download/{SLUG}/LIST.TXT`):

```bash
python get_title.py "https://supjav.com/category/cast/kijima-airi" --cast-list
```

```bash
python get_title.py "https://supjav.com/category/cast/kijima-airi" --cast-list --visual
```

```bash
python get_title.py "https://supjav.com/category/cast/kasumi-risa" --cast-list --visual
```

Примеры с **`--process-list`** (скачать из `download/kijima-airi/LIST.TXT`):

```bash
python get_title.py --process-list kijima-airi
```

```bash
python get_title.py --process-list kijima-airi --visual
```

```bash
python get_title.py --process-list kijima-airi --no-visual
```

```bash
python get_title.py --process-list kijima-airi --censored
```

```bash
python get_title.py --process-list kijima-airi --redownload
```

```bash
python get_title.py --process-list kijima-airi --censored --redownload
```

Сначала список, затем загрузка (тот же `SLUG`):

```bash
python get_title.py "https://supjav.com/category/cast/kijima-airi" --cast-list --visual
```

```bash
python get_title.py --process-list kijima-airi --visual
```

Одна видео-страница (без `--cast-list` / `--process-list`):

```bash
python get_title.py "https://supjav.com/411204.html"
```

---

### cut_video.py — вырезка куска по времени

Режет входной видеофайл и сохраняет фрагмент между `--start` и `--end`.

Требуется `ffmpeg` (обычно достаточно установить один раз).

Параметры:

- `--input/-i` — входной файл
- `--output/-o` — выходной файл
- `--start` — время начала (например `00:02:10` или `130.5`)
- `--end` — время конца (например `00:05:30` или `330`)
- `--mode`:
  - `reencode` (по умолчанию) — более точная нарезка (перекодирование)
  - `copy` — без перекодирования (быстрее, но рез может “съехать” на keyframe)
- `--ffmpeg-path` — если `ffmpeg.exe` не в `PATH`, укажите полный путь

Примеры:

```bash
python c:/projects/JAV/cut_video.py --input "NSFS-061_UNCENSORED.m4v" --output "out.m4v" --start "00:00:00" --end "02:02:30" --mode copy
```

```bash
python c:/projects/JAV/cut_video.py -i "clip.m4v" -o "fragment.m4v" --start "00:02:10" --end "00:05:30"
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
- **`--process-list`:** reads LIST.TXT, filters by label (`Reducing Mosaic`, `Uncensored Leak`, or empty for `--censored`), downloads each film, validates with ffprobe, saves to SQLite DB.

## Notes

- The target site may change its layout or script logic; selectors and filters might need updates.
- Respect the site's terms of use and robots.txt when using this tool.
