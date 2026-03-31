# Tech context

## Stack

- Python 3.8+
- Playwright (Chromium или **Google Chrome** — рекомендуется)
- yt-dlp (для части хостов)
- ffmpeg + ffprobe (`cut_video.py`, валидация в `get_title.py`)
- SQLite (`downloads.db`)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Браузер: установить Chrome **или** `python -m playwright install chromium`.

## Key paths

- Загрузки по умолчанию: `download/`
- База: `downloads.db` (в корне репозитория)
- Правила Cursor: `.cursor/rules/*.mdc`

## Agent terminal note

Для долгих команд (в т.ч. `python dodnld.py ... -v`) в этой среде задано правило: у вызова Shell указывать `block_until_ms: 60000` или выше — см. `.cursor/rules/agent-terminal-timeout.mdc`.
