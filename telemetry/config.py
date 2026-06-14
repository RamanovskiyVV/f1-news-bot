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

# F1TV session cookies for TeamRadio MP3 downloads (F1TV Pro required)
# Get them via: python get_cf_cookies.py  (F12 → Application → Cookies → formula1.com)
CF_POLICY      = os.getenv("CF_POLICY", "")
CF_SIGNATURE   = os.getenv("CF_SIGNATURE", "")
CF_KEY_PAIR_ID = os.getenv("CF_KEY_PAIR_ID", "")
F1_COOKIE_LOGIN_SESSION    = os.getenv("F1_COOKIE_LOGIN_SESSION", "")
F1_COOKIE_ENTITLEMENT_TOKEN = os.getenv("F1_COOKIE_ENTITLEMENT_TOKEN", "")

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
    # Reserve / test / FP1 drivers
    "PER": {"flag": "🇲🇽", "name": "Perez",         "team": "red_bull"},
    "BOT": {"flag": "🇫🇮", "name": "Bottas",       "team": "sauber"},
    "LIN": {"flag": "🇬🇧", "name": "Lindblad",     "team": "red_bull"},
    "COL": {"flag": "🇦🇷", "name": "Colapinto",    "team": "alpine"},
    "BER": {"flag": "🇩🇪", "name": "Bearman",      "team": "haas"},
    "FIT": {"flag": "🇧🇷", "name": "Fittipaldi",   "team": "haas"},
    "MAZ": {"flag": "🇷🇺", "name": "Mazepin",      "team": "sauber"},
    "ZHO": {"flag": "🇨🇳", "name": "Zhou",         "team": "sauber"},
    "SAR": {"flag": "🇺🇸", "name": "Sargeant",     "team": "williams"},
    "DEV": {"flag": "🇫🇷", "name": "De Vries",     "team": "racing_bulls"},
    "RIC": {"flag": "🇦🇺", "name": "Ricciardo",    "team": "racing_bulls"},
    "MAG": {"flag": "🇩🇰", "name": "Magnussen",    "team": "haas"},
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

# Racing number → acronym fallback (used when DriverList not yet received)
RACING_NUMBER_TO_ACR: dict[int, str] = {
    1:  "VER",
    4:  "NOR",
    16: "LEC",
    81: "PIA",
    55: "SAI",
    44: "HAM",
    63: "RUS",
    12: "ANT",
    14: "ALO",
    18: "STR",
    10: "GAS",
    7:  "DOO",
    22: "TSU",
    30: "LAW",
    6:  "HAD",
    27: "HUL",
    5:  "BOR",
    23: "ALB",
    31: "OCO",
    87: "BEA",
    11: "PER",
    77: "BOT",
    43: "COL",
}


def driver_label(acronym: str, *, with_flag: bool = True) -> str:
    """Return e.g. '🇳🇱 VER' or just 'VER'."""
    d = DRIVERS.get(acronym.upper())
    if d and with_flag:
        return f"{d['flag']} {acronym.upper()}"
    return acronym.upper()
