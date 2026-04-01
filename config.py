import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "67044176"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

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
