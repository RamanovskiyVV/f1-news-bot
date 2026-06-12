import os
from dotenv import load_dotenv

load_dotenv()

# Telegram — отдельный бот и канал
TELEMETRY_BOT_TOKEN = os.getenv("TELEMETRY_BOT_TOKEN")
TELEMETRY_CHANNEL_ID = os.getenv("TELEMETRY_CHANNEL_ID")

# OpenAI (shared key from main bot)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_WHISPER_MODEL = "whisper-1"
OPENAI_FILTER_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Polling
TELEMETRY_POLL_INTERVAL = int(os.getenv("TELEMETRY_POLL_INTERVAL", "15"))

# OpenF1 base URL
OPENF1_BASE_URL = "https://api.openf1.org/v1"

# F1TV subscription token for live timing SignalR stream (free F1TV Access account)
# Get it once via: python -c "from fastf1.internals.f1auth import get_auth_token; get_auth_token()"
F1_SUBSCRIPTION_TOKEN = os.getenv("F1_SUBSCRIPTION_TOKEN", "")

# ── Driver metadata ────────────────────────────────────────────────────────────
# flag emoji, full name, team key
DRIVERS: dict[str, dict] = {
    "VER": {"flag": "🇳🇱", "name": "Verstappen",  "team": "red_bull"},
    "NOR": {"flag": "🇬🇧", "name": "Norris",       "team": "mclaren"},
    "LEC": {"flag": "🇲🇨", "name": "Leclerc",      "team": "ferrari"},
    "PIA": {"flag": "🇦🇺", "name": "Piastri",      "team": "mclaren"},
    "SAI": {"flag": "🇪🇸", "name": "Sainz",        "team": "williams"},
    "HAM": {"flag": "🇬🇧", "name": "Hamilton",     "team": "ferrari"},
    "RUS": {"flag": "🇬🇧", "name": "Russell",      "team": "mercedes"},
    "ANT": {"flag": "🇬🇧", "name": "Antonelli",    "team": "mercedes"},
    "ALO": {"flag": "🇪🇸", "name": "Alonso",       "team": "aston_martin"},
    "STR": {"flag": "🇨🇦", "name": "Stroll",       "team": "aston_martin"},
    "GAS": {"flag": "🇫🇷", "name": "Gasly",        "team": "alpine"},
    "DOO": {"flag": "🇦🇺", "name": "Doohan",       "team": "alpine"},
    "TSU": {"flag": "🇯🇵", "name": "Tsunoda",      "team": "red_bull"},
    "LAW": {"flag": "🇳🇿", "name": "Lawson",       "team": "racing_bulls"},
    "HAD": {"flag": "🇺🇸", "name": "Hadjar",       "team": "racing_bulls"},
    "HUL": {"flag": "🇩🇪", "name": "Hulkenberg",   "team": "sauber"},
    "BOR": {"flag": "🇩🇪", "name": "Bortoleto",    "team": "sauber"},
    "ALB": {"flag": "🇹🇭", "name": "Albon",        "team": "williams"},
    "OCO": {"flag": "🇫🇷", "name": "Ocon",         "team": "haas"},
    "BEA": {"flag": "🇫🇷", "name": "Bearman",      "team": "haas"},
}

TEAM_NAMES: dict[str, str] = {
    "red_bull":     "Red Bull",
    "mclaren":      "McLaren",
    "ferrari":      "Ferrari",
    "mercedes":     "Mercedes",
    "aston_martin": "Aston Martin",
    "alpine":       "Alpine",
    "racing_bulls": "Racing Bulls",
    "sauber":       "Kick Sauber",
    "williams":     "Williams",
    "haas":         "Haas",
}

# Tyre compound colours for display
TYRE_EMOJI: dict[str, str] = {
    "SOFT":        "🔴",
    "MEDIUM":      "🟡",
    "HARD":        "⚪",
    "INTERMEDIATE":"🟢",
    "WET":         "🔵",
    "UNKNOWN":     "⚫",
}

# Race control message categories
RC_KEYWORDS: dict[str, str] = {
    "SAFETY CAR":          "🚗",
    "VIRTUAL SAFETY CAR":  "🟡",
    "RED FLAG":            "🚩",
    "YELLOW FLAG":         "🟡",
    "GREEN FLAG":          "🟢",
    "CHEQUERED":           "🏁",
    "DRS":                 "💨",
    "INVESTIGATION":       "🔎",
    "PENALTY":             "⚠️",
    "RETIRED":             "🛑",
    "INCIDENT":            "💥",
    "BLACK AND WHITE":     "⬛",
}

POSITION_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def driver_label(acronym: str, *, with_flag: bool = True) -> str:
    """Return e.g. '🇳🇱 VER' or just 'VER'."""
    d = DRIVERS.get(acronym.upper())
    if d and with_flag:
        return f"{d['flag']} {acronym.upper()}"
    return acronym.upper()
