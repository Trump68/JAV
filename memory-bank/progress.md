# Progress

## Working

- Извлечение URL и загрузка через `dodnld.py` (headless / visual, выбор сервера).
- Метаданные, списки актрис, пакетная обработка через `get_title.py` с SQLite и ffprobe.
- Нарезка видео через `cut_video.py` и ffmpeg.

## Documentation

- `README.md` описывает CLI и принцип работы.
- `memory-bank/` — краткий контекст проекта для агента Cursor.

## Known issues / risks

- Целевой сайт может менять вёрстку и логику плеера — потребуются правки селекторов и фильтров.
- Headless Chromium проще детектировать антиботом; при проблемах — Chrome и визуальный режим.

## Not tracked here

- Содержимое `downloads/` и сами медиафайлы — не часть описания прогресса кода.
