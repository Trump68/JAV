# System patterns

## Components

| Script | Role |
|--------|------|
| `dodnld.py` | Playwright: открытие URL, вкладка сервера, перехват запросов, извлечение stream URL из DOM/сети; загрузка (HTTP, yt-dlp). |
| `get_title.py` | Парсинг страницы видео/каста; оркестрация списков и вызовов `dodnld.py`; ffprobe-валидация; SQLite. |
| `cut_video.py` | Обёртка над ffmpeg для нарезки по `--start` / `--end`. |

## Technical patterns

- **Браузер:** Playwright; предпочтение системного Chrome над bundled Chromium (меньше детекта).
- **Потоки:** комбинация network interception + DOM (`iframe`, `video`, `source`, `data-src`, скрипты); фильтрация рекламных/аналитических доменов.
- **Серверы:** VOE (по умолчанию с цепочкой VOE→TV→ST), явные `ST` / `TV` / `FST`.
- **Загрузки:** Streamtape — play в iframe, `get_video`, редирект CDN, прямой HTTP; VOE/TV — yt-dlp с fallback на ST.
- **Данные:** `downloads.db` для учёта загрузок при пакетной обработке.

## Failure modes

- Смена вёрстки/плеера на сайте → обновление селекторов и логики в скриптах.
- Cloudflare / «human verification» → Chrome, `--visual`, меньше headless-агрессии.
