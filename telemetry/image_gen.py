"""PNG "results card" generator for the telemetry channel.

Renders a compact top-10 standings graphic to accompany the text race/sprint
results message. Colors are sampled directly from the channel logo
(telemetry/assets/logo_qvp.png) so the card reads as on-brand.
"""
from __future__ import annotations

import io
from pathlib import Path

import matplotlib.font_manager as _fm
from PIL import Image, ImageDraw, ImageFont

from .config import DRIVERS, TEAM_NAMES
from .formatter import _fmt_timedelta

_ASSETS_DIR = Path(__file__).parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "logo_qvp.png"

# ── Palette, sampled from the QVP logo ──────────────────────────────────────
BG           = (0x16, 0x35, 0x54)  # #163554 -- card background (logo navy)
HEADER_BG    = (0x0F, 0x24, 0x40)  # #0F2440 -- header strip, darker than BG
ACCENT       = (0xEA, 0x61, 0x22)  # #EA6122 -- logo orange
TEXT_MAIN    = (0xFF, 0xFF, 0xFF)
TEXT_SECOND  = (0x9F, 0xB0, 0xC7)
ROW_STRIPE   = (0x1E, 0x3F, 0x66)

_WIDTH = 1000
_HEADER_H = 130
_ROW_H = 56
_FOOTER_H = 60
_PAD_X = 40


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    props = _fm.FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal")
    path = _fm.findfont(props, fallback_to_default=True)
    return ImageFont.truetype(path, size)


def render_race_results_card(
    session: dict,
    results: list[dict],
    pit_stats: dict,
) -> bytes:
    """Render a top-10 standings PNG card. Returns PNG bytes."""
    stype = session.get("session_name", "Race")
    gp = session.get("meeting_name", "")
    year = (session.get("date_start", "") or "")[:4]
    is_sprint = "Sprint" in stype and "Qualifying" not in stype
    title = "ИТОГИ СПРИНТА" if is_sprint else "ИТОГИ ГОНКИ"

    rows = results[:10]
    height = _HEADER_H + len(rows) * _ROW_H + (_FOOTER_H if pit_stats else 0) + 20

    img = Image.new("RGB", (_WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    # ── Header ──────────────────────────────────────────────────────────────
    draw.rectangle([0, 0, _WIDTH, _HEADER_H], fill=HEADER_BG)
    draw.rectangle([0, _HEADER_H, _WIDTH, _HEADER_H + 3], fill=ACCENT)

    if _LOGO_PATH.exists():
        logo = Image.open(_LOGO_PATH).convert("RGBA")
        logo_h = 64
        logo_w = int(logo.width * (logo_h / logo.height))
        logo = logo.resize((logo_w, logo_h))
        img.paste(logo, (_PAD_X, (_HEADER_H - logo_h) // 2), logo)
        text_x = _PAD_X + logo_w + 24
    else:
        text_x = _PAD_X

    f_title = _font(30, bold=True)
    f_subtitle = _font(22)
    draw.text((text_x, 32), title, font=f_title, fill=TEXT_MAIN)
    subtitle = f"{gp} {year}".strip()
    draw.text((text_x, 72), subtitle, font=f_subtitle, fill=TEXT_SECOND)

    # ── Rows ────────────────────────────────────────────────────────────────
    f_pos = _font(24, bold=True)
    f_driver = _font(24, bold=True)
    f_team = _font(18)
    f_time = _font(22)

    y = _HEADER_H + 3
    for i, r in enumerate(rows, 1):
        if i % 2 == 0:
            draw.rectangle([0, y, _WIDTH, y + _ROW_H], fill=ROW_STRIPE)

        acr = (r.get("BroadcastName") or r.get("Abbreviation") or "???").upper()
        driver_info = DRIVERS.get(acr, {})
        team_name = TEAM_NAMES.get(driver_info.get("team", ""), "")

        pos_color = ACCENT if i <= 3 else TEXT_SECOND
        draw.text((_PAD_X, y + 14), f"P{i}", font=f_pos, fill=pos_color)
        draw.text((_PAD_X + 70, y + 6), acr, font=f_driver, fill=TEXT_MAIN)
        if team_name:
            draw.text((_PAD_X + 70, y + 32), team_name, font=f_team, fill=TEXT_SECOND)

        time_val = r.get("Time", r.get("gap", ""))
        time_str = "Победитель" if i == 1 else (f"+{_fmt_timedelta(time_val)}" if time_val else "—")
        bbox = draw.textbbox((0, 0), time_str, font=f_time)
        tw = bbox[2] - bbox[0]
        draw.text((_WIDTH - _PAD_X - tw, y + 16), time_str, font=f_time, fill=TEXT_SECOND)

        y += _ROW_H

    # ── Footer: pit summary ────────────────────────────────────────────────
    if pit_stats:
        draw.rectangle([0, y, _WIDTH, y + _FOOTER_H], fill=HEADER_BG)
        f_footer = _font(20)
        parts = []
        fastest = pit_stats.get("fastest")
        if fastest:
            dur = fastest.get("duration")
            parts.append(f"Лучший пит: {fastest.get('acronym', '')} {dur:.1f}с" if dur else "")
        total = pit_stats.get("total")
        if total:
            parts.append(f"Всего стопов: {total}")
        footer_text = "   ·   ".join(p for p in parts if p)
        draw.text((_PAD_X, y + (_FOOTER_H - 20) // 2), footer_text, font=f_footer, fill=ACCENT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
