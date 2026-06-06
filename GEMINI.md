# Project Deep Hunter: Gemini Rules & Conventions

## Project Overview
**Deep Hunter** is an elite AI-powered bot for job hunting (Frontend/Fullstack) and crypto trading. It prioritizes speed, automation, and high-quality AI analysis.

## Core Technology Stack
- **Language:** Python 3.13+ (Asyncio)
- **Bot Framework:** `python-telegram-bot`
- **AI:** Google Gemini (Generative AI)
- **Trading:** `ccxt` (Binance Testnet/Spot)
- **Database:** `aiosqlite` (SQLite)
- **Scraping:** `Playwright` (Chromium), `BeautifulSoup`, `httpx`
- **Parsing:** `feedparser` (RSS)

## Development Principles & Rules

### 1. Concurrency & Performance
- **Parallel Execution:** Always use `asyncio.gather` for fetching jobs and scoring them. Use `asyncio.Semaphore` (limit 4-6) to prevent overwhelming APIs or the CPU.
- **Async First:** Avoid blocking I/O. Use `aiosqlite` for DB and `httpx` for network.

### 2. Job Search (Project "Deep Hunter")
- **Zero-Config Policy:** Prefer open sources (RSS, Scraping, Public APIs) over those requiring personal API keys. Currently supports: HN, Habr, Djinni, Remote.co, Himalayas, etc.
- **AI Query Expansion:** Always use `expand_search_query` in `ai/jobs.py` to generate multiple search variations before fetching.
- **Scoring Preferences:**
    1. **Priority #1:** Vue 3 + TypeScript / Composition API.
    2. **Priority #2:** React + TypeScript + Next.js.
    3. **Roles:** Middle/Senior Frontend, Middle/Senior Fullstack (if FE > 50%).
- **Playwright Usage:** Always use the `PlaywrightManager` singleton in `job_fetcher.py` to manage browser instances and ensure they are closed properly after each cycle.

### 3. Trading Engine
- **Technical Analysis:** Combined usage of EMA (13/34/50), RSI, and ADX indicators.
- **AI Decision Making:** Gemini acts as a filter, analyzing technical data and news sentiment to confirm or veto technical signals.
- **Risk Management:** Strictly follow `PAPER_MODE` defaults unless explicitly switched by user.

### 4. Database & Persistence
- **Thread Safety:** Use the global `db_lock` in `database.py` for all write operations to prevent `database is locked` errors.
- **Deduplication:** Always deduplicate vacancies by both URL and a hash of `Company + Title`.

## File Structure Conventions
- `bot.py`: Main entry point and service orchestration.
- `job_fetcher.py`: All logic for gathering raw vacancy data.
- `job_handlers.py`: Telegram command handlers for job features.
- `ai/`: Specialized AI prompts (keep them concise and objective).
- `database.py`: All SQL queries and migrations.

## Prompt Engineering
- Keep AI prompts focused on **objective analysis**.
- When editing `ai/jobs.py` scoring prompt, do not use "biased" or "extreme" language; focus on matching the candidate's actual stack.
