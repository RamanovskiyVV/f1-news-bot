"""Phone-first PNG results card for the telemetry channel."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .config import DRIVERS, TEAM_NAMES, TELEMETRY_CHANNEL_ID
from .formatter import _fmt_timedelta

_ASSETS_DIR = Path(__file__).parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "logo_qvp.png"

# High-contrast palette tuned for Telegram's phone-sized image preview.
BG = (8, 13, 22)
SURFACE = (18, 26, 39)
SURFACE_ALT = (23, 33, 48)
ACCENT = (255, 92, 36)
ACCENT_SOFT = (71, 35, 29)
TEXT_MAIN = (248, 250, 252)
TEXT_SECOND = (174, 187, 204)
TEXT_MUTED = (112, 128, 149)
DIVIDER = (42, 54, 71)

TEAM_COLORS = {
    "red_bull": (54, 113, 198),
    "mclaren": (255, 128, 0),
    "ferrari": (232, 0, 45),
    "mercedes": (0, 210, 190),
    "aston_martin": (34, 153, 113),
    "alpine": (255, 116, 180),
    "racing_bulls": (102, 146, 255),
    "audi": (190, 30, 45),
    "sauber": (82, 226, 82),
    "williams": (0, 144, 255),
    "haas": (190, 198, 207),
    "cadillac": (196, 164, 105),
}

TYRE_COLORS = {
    "SOFT": (239, 51, 64),
    "MEDIUM": (255, 211, 45),
    "HARD": (238, 241, 245),
    "INTERMEDIATE": (45, 194, 107),
    "WET": (53, 132, 228),
    "UNKNOWN": TEXT_MUTED,
}

TYRE_LABELS = {
    "SOFT": "SOFT",
    "MEDIUM": "MED",
    "HARD": "HARD",
    "INTERMEDIATE": "INTER",
    "WET": "WET",
    "UNKNOWN": "—",
}

WIDTH = 1080
HEIGHT = 1350
PAD_X = 48
HEADER_H = 224
COLUMN_H = 52
ROW_H = 92


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = (
        _ASSETS_DIR / filename,
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu") / filename,
        Path("/usr/share/fonts/dejavu") / filename,
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    # Pillow distributions commonly bundle DejaVu and resolve it by filename.
    # Minimal production images may not ship any system fonts, so keep card
    # rendering alive with Pillow's embedded font instead of crashing the bot.
    try:
        return ImageFont.truetype(filename, size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1 has no scalable default font.
            return ImageFont.load_default()


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    size: int,
    *,
    bold: bool = False,
    min_size: int = 20,
) -> ImageFont.ImageFont:
    """Choose the largest font that fits the available width."""
    while size > min_size:
        font = _font(size, bold=bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
        size -= 2
    return _font(min_size, bold=bold)


def _brand_label() -> str:
    channel = str(TELEMETRY_CHANNEL_ID or "").strip()
    return channel if channel.startswith("@") else "QP TELEMETRY"


def _driver_data(row: dict) -> tuple[str, str, str, str]:
    acr = str(row.get("Abbreviation") or row.get("BroadcastName") or "???").upper()
    info = DRIVERS.get(acr, {})
    driver_name = str(info.get("name") or row.get("BroadcastName") or acr)
    team_key = str(info.get("team") or "")
    team_name = str(row.get("TeamName") or TEAM_NAMES.get(team_key, ""))
    return acr, driver_name, team_key, team_name


def _result_label(row: dict, index: int) -> str:
    if index == 0:
        return "ПОБЕДИТЕЛЬ"
    value = row.get("Time", row.get("gap"))
    if value not in (None, "", "NaT"):
        return f"+{_fmt_timedelta(value)}"
    status = str(row.get("Status") or "").strip()
    if status and status.lower() not in {"finished", "lapped"}:
        return status.upper()
    return "—"


def _paste_logo(img: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    if not _LOGO_PATH.exists():
        return
    with Image.open(_LOGO_PATH) as source:
        logo = source.convert("RGBA")
    logo.thumbnail((190, 154), Image.Resampling.LANCZOS)
    x = WIDTH - PAD_X - logo.width
    img.paste(logo, (x, 18), logo)

    label = _brand_label()
    font = _font(21, bold=True)
    box = draw.textbbox((0, 0), label, font=font)
    label_w = box[2] - box[0]
    pill_x = WIDTH - PAD_X - label_w - 28
    draw.rounded_rectangle(
        (pill_x, 166, WIDTH - PAD_X, 202), radius=18, fill=ACCENT_SOFT
    )
    draw.text((pill_x + 14, 171), label, font=font, fill=ACCENT)


def render_race_results_card(
    session: dict,
    results: list[dict],
    pit_stats: dict,
) -> bytes:
    """Render a high-contrast 4:5 top-10 card and return its PNG bytes."""
    stype = str(session.get("session_name", "Race"))
    gp = str(session.get("meeting_name", "")).strip()
    year = str(session.get("date_start", "") or "")[:4]
    is_sprint = "Sprint" in stype and "Qualifying" not in stype
    title = "ИТОГИ СПРИНТА" if is_sprint else "ИТОГИ ГОНКИ"

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, 13, HEADER_H), fill=ACCENT)
    eyebrow = f"ФИНАЛ  •  {year}" if year else "ФИНАЛ"
    draw.text((PAD_X, 30), eyebrow, font=_font(23, bold=True), fill=ACCENT)
    draw.text((PAD_X, 68), title, font=_font(52, bold=True), fill=TEXT_MAIN)
    gp_font = _fit_text(draw, gp, 720, 32, min_size=23)
    draw.text((PAD_X, 143), gp, font=gp_font, fill=TEXT_SECOND)
    _paste_logo(img, draw)

    column_y = HEADER_H
    draw.rectangle((0, column_y, WIDTH, column_y + COLUMN_H), fill=SURFACE)
    col_font = _font(20, bold=True)
    draw.text((PAD_X, column_y + 14), "ПОЗ.", font=col_font, fill=TEXT_MUTED)
    draw.text((151, column_y + 14), "ПИЛОТ", font=col_font, fill=TEXT_MUTED)
    right_label = "РЕЗУЛЬТАТ"
    right_box = draw.textbbox((0, 0), right_label, font=col_font)
    draw.text(
        (WIDTH - PAD_X - (right_box[2] - right_box[0]), column_y + 14),
        right_label,
        font=col_font,
        fill=TEXT_MUTED,
    )

    rows = results[:10]
    y = HEADER_H + COLUMN_H
    for index in range(10):
        row = rows[index] if index < len(rows) else None
        fill = SURFACE_ALT if index % 2 == 0 else BG
        draw.rectangle((0, y, WIDTH, y + ROW_H), fill=fill)
        draw.line(
            (PAD_X, y + ROW_H - 1, WIDTH - PAD_X, y + ROW_H - 1),
            fill=DIVIDER,
        )

        if row is not None:
            acr, driver_name, team_key, team_name = _driver_data(row)
            try:
                position = int(row.get("Position") or index + 1)
            except (TypeError, ValueError):
                position = index + 1

            team_color = TEAM_COLORS.get(team_key, ACCENT)
            draw.rounded_rectangle(
                (PAD_X, y + 18, PAD_X + 62, y + 74), radius=12, fill=SURFACE
            )
            pos_font = _font(31, bold=True)
            pos_text = str(position)
            pos_box = draw.textbbox((0, 0), pos_text, font=pos_font)
            pos_x = PAD_X + 31 - (pos_box[2] - pos_box[0]) // 2
            pos_color = ACCENT if position == 1 else TEXT_MAIN
            draw.text((pos_x, y + 25), pos_text, font=pos_font, fill=pos_color)

            draw.rounded_rectangle(
                (122, y + 18, 130, y + 74), radius=4, fill=team_color
            )
            name_font = _fit_text(
                draw, driver_name.upper(), 430, 34, bold=True, min_size=27
            )
            draw.text((151, y + 13), driver_name.upper(), font=name_font, fill=TEXT_MAIN)
            secondary = f"{acr}  •  {team_name}" if team_name else acr
            draw.text((151, y + 54), secondary, font=_font(21), fill=TEXT_SECOND)

            result = _result_label(row, index)
            result_font = _fit_text(
                draw, result, 275, 29, bold=True, min_size=21
            )
            result_box = draw.textbbox((0, 0), result, font=result_font)
            result_x = WIDTH - PAD_X - (result_box[2] - result_box[0])
            result_color = ACCENT if index == 0 else TEXT_MAIN
            draw.text(
                (result_x, y + 29), result, font=result_font, fill=result_color
            )
        y += ROW_H

    draw.rectangle((0, y, WIDTH, HEIGHT), fill=SURFACE)
    draw.rectangle((PAD_X, y + 24, PAD_X + 5, HEIGHT - 24), fill=ACCENT)
    footer_font = _font(22, bold=True)
    footer_text = "ОФИЦИАЛЬНЫЕ РЕЗУЛЬТАТЫ"
    fastest: Any = pit_stats.get("fastest") if pit_stats else None
    total = pit_stats.get("total") if pit_stats else None
    if fastest:
        duration = fastest.get("duration")
        if duration:
            footer_text = (
                f"ЛУЧШИЙ ПИТ  {fastest.get('acronym', '')}  •  {duration:.1f} С"
            )
    elif total:
        footer_text = f"ПИТ-СТОПОВ  •  {total}"
    draw.text((PAD_X + 22, y + 34), footer_text, font=footer_font, fill=TEXT_SECOND)

    brand = _brand_label()
    brand_font = _font(24, bold=True)
    brand_box = draw.textbbox((0, 0), brand, font=brand_font)
    draw.text(
        (WIDTH - PAD_X - (brand_box[2] - brand_box[0]), y + 32),
        brand,
        font=brand_font,
        fill=ACCENT,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _gap_label(value: Any, position: int) -> str:
    if position == 1:
        return "ЛИДЕР"
    gap = str(value or "").strip()
    if not gap:
        return "—"
    upper = gap.upper()
    if upper in {"LEADER", "LIDER"}:
        return "ЛИДЕР"
    return gap if gap.startswith(("+", "-")) else f"+{gap}"


def render_race_summary_card(
    current_lap: int,
    total_laps: int,
    rows: list[dict],
    meeting_name: str = "",
) -> bytes:
    """Render a branded live race snapshot for Telegram's mobile feed."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    header_h = 224
    column_h = 52
    row_h = 92

    draw.rectangle((0, 0, 13, header_h), fill=ACCENT)
    draw.text((PAD_X, 28), "LIVE  •  СВОДКА ГОНКИ", font=_font(24, True), fill=ACCENT)
    lap_text = f"КРУГ {current_lap}/{total_laps}" if total_laps else f"КРУГ {current_lap}"
    draw.text((PAD_X, 68), lap_text, font=_font(52, True), fill=TEXT_MAIN)
    gp_font = _fit_text(draw, meeting_name, 690, 29, min_size=22)
    draw.text((PAD_X, 139), meeting_name, font=gp_font, fill=TEXT_SECOND)
    _paste_logo(img, draw)

    # Distance progress remains readable even in Telegram's compact preview.
    progress_x1, progress_x2 = PAD_X, WIDTH - PAD_X
    progress_y = 202
    draw.rounded_rectangle(
        (progress_x1, progress_y, progress_x2, progress_y + 8),
        radius=4,
        fill=DIVIDER,
    )
    if total_laps > 0:
        ratio = max(0.0, min(1.0, current_lap / total_laps))
        progress_end = progress_x1 + int((progress_x2 - progress_x1) * ratio)
        if progress_end > progress_x1:
            draw.rounded_rectangle(
                (progress_x1, progress_y, progress_end, progress_y + 8),
                radius=4,
                fill=ACCENT,
            )

    column_y = header_h
    draw.rectangle((0, column_y, WIDTH, column_y + column_h), fill=SURFACE)
    col_font = _font(19, True)
    draw.text((PAD_X, column_y + 15), "ПОЗ.", font=col_font, fill=TEXT_MUTED)
    draw.text((151, column_y + 15), "ПИЛОТ", font=col_font, fill=TEXT_MUTED)
    draw.text((600, column_y + 15), "ОТРЫВ", font=col_font, fill=TEXT_MUTED)
    draw.text((760, column_y + 15), "ШИНЫ", font=col_font, fill=TEXT_MUTED)
    draw.text((987, column_y + 15), "ПИТ", font=col_font, fill=TEXT_MUTED)

    y = header_h + column_h
    live_rows = rows[:10]
    for index in range(10):
        row = live_rows[index] if index < len(live_rows) else None
        fill = SURFACE_ALT if index % 2 == 0 else BG
        draw.rectangle((0, y, WIDTH, y + row_h), fill=fill)
        draw.line((PAD_X, y + row_h - 1, WIDTH - PAD_X, y + row_h - 1), fill=DIVIDER)

        if row is not None:
            try:
                position = int(row.get("position") or index + 1)
            except (TypeError, ValueError):
                position = index + 1
            acr = str(row.get("acronym") or "???").upper()
            info = DRIVERS.get(acr, {})
            name = str(info.get("name") or acr).upper()
            team_key = str(info.get("team") or "")
            team = TEAM_NAMES.get(team_key, "")

            pos_font = _font(31, True)
            pos_text = str(position)
            pos_box = draw.textbbox((0, 0), pos_text, font=pos_font)
            draw.text(
                (PAD_X + 27 - (pos_box[2] - pos_box[0]) // 2, y + 27),
                pos_text,
                font=pos_font,
                fill=ACCENT if position == 1 else TEXT_MAIN,
            )
            draw.rounded_rectangle(
                (122, y + 18, 130, y + 74),
                radius=4,
                fill=TEAM_COLORS.get(team_key, ACCENT),
            )
            name_font = _fit_text(draw, name, 380, 32, bold=True, min_size=25)
            draw.text((151, y + 14), name, font=name_font, fill=TEXT_MAIN)
            sub = f"{acr}  •  {team}" if team else acr
            draw.text((151, y + 54), sub, font=_font(20), fill=TEXT_SECOND)

            gap = _gap_label(row.get("gap"), position)
            gap_font = _fit_text(draw, gap, 135, 25, bold=True, min_size=19)
            gap_box = draw.textbbox((0, 0), gap, font=gap_font)
            draw.text(
                (718 - (gap_box[2] - gap_box[0]), y + 32),
                gap,
                font=gap_font,
                fill=ACCENT if position == 1 else TEXT_MAIN,
            )

            compound = str(row.get("compound") or "UNKNOWN").upper()
            tyre_color = TYRE_COLORS.get(compound, TEXT_MUTED)
            tyre_label = TYRE_LABELS.get(compound, compound[:5])
            draw.ellipse((756, y + 32, 774, y + 50), fill=tyre_color)
            age = row.get("tyre_age")
            age_text = f" • {age} КР" if age is not None else ""
            tyre_text = f"{tyre_label}{age_text}"
            tyre_font = _fit_text(draw, tyre_text, 174, 21, bold=True, min_size=17)
            draw.text((786, y + 30), tyre_text, font=tyre_font, fill=TEXT_MAIN)

            pit_count = int(row.get("pit_count") or 0)
            pit_text = str(pit_count)
            pit_font = _font(28, True)
            pit_box = draw.textbbox((0, 0), pit_text, font=pit_font)
            draw.text(
                (1004 - (pit_box[2] - pit_box[0]) // 2, y + 28),
                pit_text,
                font=pit_font,
                fill=TEXT_SECOND,
            )
        y += row_h

    draw.rectangle((0, y, WIDTH, HEIGHT), fill=SURFACE)
    note = "ОТРЫВ • СОСТАВ И ВОЗРАСТ ШИН • ПИТ-СТОПЫ"
    draw.text((PAD_X, y + 35), note, font=_font(19, True), fill=TEXT_SECOND)
    brand = _brand_label()
    brand_font = _font(24, True)
    brand_box = draw.textbbox((0, 0), brand, font=brand_font)
    draw.text(
        (WIDTH - PAD_X - (brand_box[2] - brand_box[0]), y + 31),
        brand,
        font=brand_font,
        fill=ACCENT,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
