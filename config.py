import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Binance & Trade Settings
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")
TRADE_PAIRS = [p.strip() for p in os.getenv("TRADE_PAIRS", "BTC/USDT").split(",")]
PAPER_MODE = os.getenv("PAPER_MODE", "True").lower() == "true"
TRADE_TIMEFRAME = os.getenv("TRADE_TIMEFRAME", "15m")
TRADE_INTERVAL_SECONDS = int(os.getenv("TRADE_INTERVAL_SECONDS", "900"))
TRADE_QTY = float(os.getenv("TRADE_QTY", "0.001"))  # дефолт, если пара не найдена
TRADE_QTY_MAP = {}
for pair in TRADE_PAIRS:
    base = pair.split("/")[0]  # ETH, SOL, BTC
    qty = os.getenv(f"TRADE_QTY_{base}")
    if qty:
        TRADE_QTY_MAP[pair] = float(qty)
TRADE_STOP_LOSS_PCT = float(os.getenv("TRADE_STOP_LOSS_PCT", "0.015"))
TRADE_TAKE_PROFIT_PCT = float(os.getenv("TRADE_TAKE_PROFIT_PCT", "0.03"))
GEMINI_MIN_CONFIDENCE = float(os.getenv("GEMINI_MIN_CONFIDENCE", "0.25"))
JOB_MIN_SCORE = int(os.getenv("JOB_MIN_SCORE", "7"))
JOB_REQUIRE_WORLDWIDE = os.getenv("JOB_REQUIRE_WORLDWIDE", "True").lower() == "true"

DB_PATH = "bot.db"

GAMING_KEYWORDS = [
    "game", "gaming", "gamer", "games", "игра", "игры", "игровой", "геймер", "геймплей",
    "playstation", "xbox", "nintendo", "steam", "pc game", "switch", "ps5", "ps4", "ps3", "ps2", "ps1",
    "xbox series", "xbox 360", "game boy", "gameboy", "gba", "3ds", "ds lite", "psp", "vita",
    "sega", "genesis", "mega drive", "dreamcast", "atari", "commodore", "amiga", "nes", "snes",
    "n64", "gamecube", "wii", "game pass", "epic games", "gog",
    "retro", "classic game", "vintage game", "old school", "ретро", "классика", "олдскул",
    "emulator", "emulation", "эмулятор", "эмуляция", "romhack", "rom hack",
    "remaster", "remake", "ремастер", "ремейк", "rerelease", "re-release",
    "indie", "indie game", "инди", "инди игра", "kickstarter", "itch.io",
    "esport", "esports", "tournament", "championship", "competitive", "pro player",
    "киберспорт", "турнир", "чемпионат", "соревнование", "лига", "матч",
    "dota", "cs2", "counter-strike", "valorant", "league of legends", "lol", "overwatch",
    "fortnite", "pubg", "apex legends", "rocket league",
    "release", "launch", "update", "patch", "dlc", "expansion", "early access",
    "релиз", "обновление", "патч", "дополнение", "ранний доступ",
    "review", "preview", "gameplay", "trailer", "reveal", "announce",
    "обзор", "превью", "трейлер", "анонс",
    "developer", "studio", "publisher", "разработчик", "студия", "издатель",
]

RSS_FEEDS = [
    "https://www.ign.com/articles.rss",
    "https://kotaku.com/rss",
    "https://www.gamespot.com/feeds/mashup/",
    "https://feeds.feedburner.com/RockPaperShotgun",
    "https://www.eurogamer.net/feed",
    "https://www.pcgamer.com/rss/",
    "https://www.polygon.com/rss/index.xml",
    "https://www.gamesradar.com/rss/",
    "https://www.vg247.com/feed",
    "https://videogameschronicle.com/feed/",
    "https://www.timeextension.com/feeds/articles",
    "https://dotesports.com/feed",
    "https://esportsinsider.com/feed",
    "https://www.theverge.com/games/rss/index.xml",
    "https://www.engadget.com/rss-gaming.xml",
    "https://9to5google.com/feed/",
    "https://www.androidauthority.com/feed/",
    "https://gamerant.com/feed/",
    "https://www.techradar.com/rss/news/gaming",
    "https://www.destructoid.com/feed/",
    "https://www.dualshockers.com/feed/",
    "https://www.digitaltrends.com/gaming/feed/",
]
