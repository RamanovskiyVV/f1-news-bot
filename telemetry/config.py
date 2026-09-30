import os
from dotenv import load_dotenv

load_dotenv()

# Telegram — отдельный бот и канал
TELEMETRY_BOT_TOKEN = os.getenv("TELEMETRY_BOT_TOKEN")
TELEMETRY_CHANNEL_ID = os.getenv("TELEMETRY_CHANNEL_ID")

# OpenAI (shared key from main bot)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# High-accuracy file transcription with F1 context/keyword hints. Keep the env
# override so the model can be changed without another deploy.
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe")
OPENAI_TRANSLATION_MODEL = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-6.1-sol")

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
    "ANT": {"flag": "🇮🇹", "name": "Antonelli",    "team": "mercedes"},
    "ALO": {"flag": "🇪🇸", "name": "Alonso",       "team": "aston_martin"},
    "STR": {"flag": "🇨🇦", "name": "Stroll",       "team": "aston_martin"},
    "GAS": {"flag": "🇫🇷", "name": "Gasly",        "team": "alpine"},
    "COL": {"flag": "🇦🇷", "name": "Colapinto",    "team": "alpine"},
    "LAW": {"flag": "🇳🇿", "name": "Lawson",       "team": "racing_bulls"},
    "LIN": {"flag": "🇬🇧", "name": "Lindblad",     "team": "racing_bulls"},
    "HAD": {"flag": "🇫🇷", "name": "Hadjar",       "team": "red_bull"},
    "HUL": {"flag": "🇩🇪", "name": "Hulkenberg",   "team": "audi"},
    "BOR": {"flag": "🇧🇷", "name": "Bortoleto",    "team": "audi"},
    "ALB": {"flag": "🇹🇭", "name": "Albon",        "team": "williams"},
    "OCO": {"flag": "🇫🇷", "name": "Ocon",         "team": "haas"},
    "BEA": {"flag": "🇬🇧", "name": "Bearman",      "team": "haas"},
    "PER": {"flag": "🇲🇽", "name": "Perez",         "team": "cadillac"},
    "BOT": {"flag": "🇫🇮", "name": "Bottas",       "team": "cadillac"},
    # 2026 substitute / reserve driver who has started races.
    "TSU": {"flag": "🇯🇵", "name": "Tsunoda",      "team": "racing_bulls"},
    # Historical / test drivers retained for old sessions and FP1 feeds.
    "DOO": {"flag": "🇦🇺", "name": "Doohan",       "team": "alpine"},
    "FIT": {"flag": "🇧🇷", "name": "Fittipaldi",   "team": "haas"},
    "MAZ": {"flag": "🇷🇺", "name": "Mazepin",      "team": "sauber"},
    "ZHO": {"flag": "🇨🇳", "name": "Zhou",         "team": "sauber"},
    "SAR": {"flag": "🇺🇸", "name": "Sargeant",     "team": "williams"},
    "DEV": {"flag": "🇳🇱", "name": "De Vries",     "team": "racing_bulls"},
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
    "audi":         "Audi",
    "sauber":       "Kick Sauber",  # historical sessions through 2025
    "williams":     "Williams",
    "haas":         "Haas",
    "cadillac":     "Cadillac",
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
    3:  "VER",
    1:  "NOR",
    16: "LEC",
    81: "PIA",
    55: "SAI",
    44: "HAM",
    63: "RUS",
    12: "ANT",
    14: "ALO",
    18: "STR",
    10: "GAS",
    43: "COL",
    30: "LAW",
    41: "LIN",
    6:  "HAD",
    27: "HUL",
    5:  "BOR",
    23: "ALB",
    31: "OCO",
    87: "BEA",
    11: "PER",
    77: "BOT",
    22: "TSU",
}


# Short vocabulary hint passed to the transcription prompt to bias recognition
# toward F1 team-radio jargon and away from noise-driven mishearing.
RADIO_GLOSSARY_PROMPT = (
    "Box box box, pit confirm, push now, gap is, DRS, undercut, overcut, "
    "safety car, virtual safety car, VSC, box this lap, box next lap, "
    "tyre degradation, understeer, oversteer, hydraulics, brake bias, "
    "P1, P2, P3, delta, sector."
)

# English radio phrase -> preferred Russian equivalent, given to the translation
# model as a terminology reference so common F1 jargon is rendered consistently
# instead of literally.
RADIO_TERMS_RU: dict[str, str] = {
    "box, box, box":  "на пит-стоп, живо",
    "box this lap":   "заезжай на этом круге",
    "box next lap":   "заезжай на следующем круге",
    "push":           "дави / жми",
    "gap":            "отрыв",
    "undercut":       "андеркат",
    "overcut":        "оверкат",
    "safety car":     "машина безопасности",
    "virtual safety car": "виртуальная машина безопасности",
    "copy":           "принял",
    "understood":     "понял",
    "box, confirm":   "подтверди заезд на пит-стоп",
    "delta":          "дельта (разница по времени)",
    "pit release":    "выпуск с пит-лейна",
    "unsafe release": "небезопасный выпуск с пит-лейна",
    "crying on the radio": "ныть / плакаться по радио",
}


def driver_label(acronym: str, *, with_flag: bool = True) -> str:
    """Return e.g. '🇳🇱 VER' or just 'VER'."""
    d = DRIVERS.get(acronym.upper())
    if d and with_flag:
        return f"{d['flag']} {acronym.upper()}"
    return acronym.upper()
