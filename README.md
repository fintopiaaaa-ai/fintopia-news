# Fintopia News Bot (GitHub Actions — 24/7, hands-free)

Runs on GitHub's servers every ~15 minutes (7:30 AM–8:30 PM IST, weekdays). Fetches
Indian-market news from many sources (Google News aggregator + CNBC-TV18 + Moneycontrol +
ET + LiveMint + BusinessLine + Investing.com), filters out tips/targets (SEBI-compliant),
uses Google Gemini (free) to pick the single most MEANINGFUL item and write a compliant
educational caption, then posts a branded card to Telegram with source attribution.
No laptop, no Claude app needed.

## One-time setup (~10 min)
1. Create a free GitHub account → create a new **public** repo (e.g. `fintopia-news`).
2. Upload everything in this folder to the repo (keep the `.github/workflows/` path).
3. Get a FREE Gemini API key: https://aistudio.google.com/apikey  → "Create API key".
4. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN`  = your BotFather token
   - `TELEGRAM_CHANNEL`    = @fintopiaaaa
   - `GEMINI_API_KEY`      = your free Gemini key
5. **Actions** tab → enable workflows → open "Fintopia News Bot" → **Run workflow** to test.

That's it. It then runs itself forever, free.

## Tuning (optional, via repo Secrets/vars or edit news_bot.py defaults)
- FRESH_MIN (75) — only post items newer than this many minutes
- MIN_SCORE (4) — significance threshold for the keyword pre-filter
- GEMINI_MODEL (gemini-2.0-flash) — change if you prefer another free Gemini model

## Important
Once this is live, DISABLE the other two news posters so you don't triple-post:
- the Claude "fintopia-news-bot" scheduled task, and
- the laptop "Fintopia News" Task Scheduler entry.
