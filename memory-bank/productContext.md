# Product context

## Problem

Страницы видео на Supjav грузят плеер через iframe и разные хосты (VOE, Streamtape, TV и т.д.). Прямой URL потока не всегда виден в HTML; нужен браузер (Playwright), перехват сети и/или yt-dlp.

## User workflows

1. **Одна страница:** `dodnld.py` — вывести URL или скачать в `download/`; `-v` для окна браузера.
2. **Метаданные + скачивание:** `get_title.py` — title/code/cast, постер, вызов `dodnld.py`.
3. **Каталог актрисы:** `get_title.py --cast-list` → `LIST.TXT`; затем `--process-list SLUG` с фильтрами по label (Reducing Mosaic / censored).
4. **Постобработка:** `cut_video.py` — фрагмент по времени через ffmpeg (`reencode` / `copy`).

## UX goals

- Понятные флаги в CLI (см. таблицы в README).
- При сбоях DNS/таймаутах — осмысленные fallback (например ST после VOE/TV).
- Локальная SQLite БД (`downloads.db`) для отслеживания уже скачанного при `--process-list`.
