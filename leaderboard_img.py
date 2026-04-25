"""
Generation d'image du leaderboard via Pillow.

Style inspire du leaderboard "VRC/GC French Matchmaking" :
fond sombre, top 3 avec badges or/argent/bronze, valeurs en vert,
W-L colorise, footer "Play'IT Matchmaking Bot".

Format d'entree (par joueur) :
    {
        "rank":       int,
        "name":       str,
        "elo":        int,
        "wins":       int,
        "losses":     int,
        "avatar_url": str | None,
    }
"""

from __future__ import annotations

from io import BytesIO
from typing import Iterable, Mapping, Any, Sequence

import requests
from PIL import Image, ImageDraw, ImageFont


# ── Layout ────────────────────────────────────────────────────────
WIDTH            = 1700
TITLE_BAND       = 100
COL_HEADER_BAND  = 50
ROW_HEIGHT       = 75
FOOTER_BAND      = 55

# Centres / x des colonnes
X_POS         = 95
X_AVATAR_LEFT = 165
X_NAME_LEFT   = 235
X_ELO         = 870
X_WL          = 1170
X_WINPCT      = 1410
X_MATCHES     = 1640

AVATAR = 50
BADGE  = 38

# ── Couleurs ──────────────────────────────────────────────────────
BG          = (12, 16, 22)
ROW_BG_A    = (19, 24, 30)
ROW_BG_B    = (24, 30, 38)
SEPARATOR   = (35, 41, 50)

WHITE       = (245, 245, 250)
SOFT_GRAY   = (140, 145, 158)
DIM_GRAY    = (100, 105, 118)
GREEN       = (96, 220, 134)
RED         = (228, 88, 88)

GOLD        = (243, 195, 60)
SILVER      = (200, 205, 215)
BRONZE      = (210, 130, 65)


# ── Cache avatars ─────────────────────────────────────────────────
_AVATAR_CACHE: dict[str, Image.Image] = {}


def _font(size: int, bold: bool = True):
    """Charge une police TTF du systeme. Bold par defaut."""
    bold_paths = [
        "C:\\Windows\\Fonts\\segoeuib.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    regular_paths = [
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    candidates = bold_paths if bold else regular_paths
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except AttributeError:
        return font.getsize(text)[0]


def _text_h(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[3] - bbox[1]
    except AttributeError:
        return font.getsize(text)[1]


def _draw_v_center(draw, text, x_left, y_center, font, color):
    """Dessine `text` avec son MILIEU VERTICAL aligne sur y_center."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        y_arg = y_center - (bbox[1] + bbox[3]) // 2
    except AttributeError:
        _, h = font.getsize(text)
        y_arg = y_center - h // 2
    draw.text((x_left, y_arg), text, fill=color, font=font)


def _draw_xy_center(draw, text, x_center, y_center, font, color):
    """Dessine `text` centre horizontalement ET verticalement sur (x_center, y_center)."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        y_arg = y_center - (bbox[1] + bbox[3]) // 2
        x_arg = x_center - w // 2 - bbox[0]
    except AttributeError:
        w, h = font.getsize(text)
        x_arg = x_center - w // 2
        y_arg = y_center - h // 2
    draw.text((x_arg, y_arg), text, fill=color, font=font)


def _fetch_avatar(url: str | None) -> Image.Image | None:
    if not url:
        return None
    if url in _AVATAR_CACHE:
        return _AVATAR_CACHE[url]
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return None
        a = Image.open(BytesIO(resp.content)).convert("RGBA")
        a = a.resize((AVATAR, AVATAR), Image.LANCZOS)
        mask = Image.new("L", (AVATAR, AVATAR), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, AVATAR, AVATAR), fill=255)
        a.putalpha(mask)
        _AVATAR_CACHE[url] = a
        return a
    except Exception:
        return None


def _draw_centered(draw, text, x_center, y, font, color):
    w = _text_w(draw, text, font)
    draw.text((x_center - w // 2, y), text, fill=color, font=font)


def _badge_color(rank: int) -> tuple[int, int, int] | None:
    return {1: GOLD, 2: SILVER, 3: BRONZE}.get(rank)


def generate_leaderboard(
    players: Iterable[Mapping[str, Any]],
    server_name: str = "",
) -> BytesIO:
    """Genere une image PNG du leaderboard, style "VRC/GC"."""
    plist: Sequence[Mapping[str, Any]] = list(players)
    n = len(plist)
    rows = max(1, n)
    height = TITLE_BAND + COL_HEADER_BAND + rows * ROW_HEIGHT + FOOTER_BAND

    img  = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    title_font   = _font(38, bold=True)
    hdr_font     = _font(18, bold=True)
    pos_font     = _font(28, bold=True)
    badge_font   = _font(22, bold=True)
    name_font    = _font(28, bold=True)
    val_font     = _font(28, bold=True)
    matches_font = _font(24, bold=False)
    footer_font  = _font(16, bold=False)

    # ── Title ───────────────────────────────────────────────────
    title_text = f"Top {n}"
    if server_name:
        title_text += f"  -  {server_name}"
    title_text += "  -  ELO"
    draw.text((50, 30), title_text, fill=WHITE, font=title_font)

    # ── Column headers (centres verticalement dans la bande) ─────
    y_hdr_c = TITLE_BAND + COL_HEADER_BAND // 2
    _draw_xy_center(draw, "POS",     X_POS,            y_hdr_c, hdr_font, DIM_GRAY)
    _draw_v_center(draw,  "PLAYER",  X_NAME_LEFT + 130, y_hdr_c, hdr_font, DIM_GRAY)
    _draw_xy_center(draw, "ELO",     X_ELO,            y_hdr_c, hdr_font, DIM_GRAY)
    _draw_xy_center(draw, "W - L",   X_WL,             y_hdr_c, hdr_font, DIM_GRAY)
    _draw_xy_center(draw, "WIN%",    X_WINPCT,         y_hdr_c, hdr_font, DIM_GRAY)
    _draw_xy_center(draw, "MATCHES", X_MATCHES,        y_hdr_c, hdr_font, DIM_GRAY)

    # ── Rows ────────────────────────────────────────────────────
    if n == 0:
        y = TITLE_BAND + COL_HEADER_BAND
        draw.text((50, y + 25), "Aucun joueur enregistre.",
                  fill=SOFT_GRAY, font=name_font)

    for i, p in enumerate(plist):
        y     = TITLE_BAND + COL_HEADER_BAND + i * ROW_HEIGHT
        bg    = ROW_BG_A if i % 2 == 0 else ROW_BG_B
        draw.rectangle((0, y, WIDTH, y + ROW_HEIGHT), fill=bg)
        draw.line((40, y + ROW_HEIGHT - 1, WIDTH - 40, y + ROW_HEIGHT - 1),
                  fill=SEPARATOR, width=1)

        rank    = int(p.get("rank", i + 1))
        name    = str(p.get("name", "?"))
        elo     = int(p.get("elo", 0))
        wins    = int(p.get("wins", 0))
        losses  = int(p.get("losses", 0))
        matches = wins + losses
        winpct  = round(wins / matches * 100) if matches > 0 else 0

        y_c = y + ROW_HEIGHT // 2

        # POS / badge
        bcolor = _badge_color(rank)
        if bcolor:
            r = BADGE // 2
            draw.ellipse((X_POS - r, y_c - r, X_POS + r, y_c + r), fill=bcolor)
            _draw_xy_center(draw, str(rank), X_POS, y_c, badge_font, (20, 20, 25))
        else:
            _draw_xy_center(draw, str(rank), X_POS, y_c, pos_font, SOFT_GRAY)

        # Avatar
        avatar = _fetch_avatar(p.get("avatar_url"))
        ay = y_c - AVATAR // 2
        if avatar is not None:
            img.paste(avatar, (X_AVATAR_LEFT, ay), avatar)
        else:
            cx, cy = X_AVATAR_LEFT + AVATAR // 2, y_c
            r = AVATAR // 2
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(45, 50, 60))
            initial = (name[:1] or "?").upper()
            _draw_xy_center(draw, initial, cx, cy, name_font, WHITE)

        # Player name
        name_color = bcolor or WHITE
        max_chars = 22
        shown = name if len(name) <= max_chars else name[:max_chars - 1] + "…"
        _draw_v_center(draw, shown, X_NAME_LEFT, y_c, name_font, name_color)

        # ELO (vert, centre)
        _draw_xy_center(draw, str(elo), X_ELO, y_c, val_font, GREEN)

        # W - L : wins vert / dash gris / losses rouge
        wins_str   = str(wins)
        losses_str = str(losses)
        dash       = " - "
        ww = _text_w(draw, wins_str,   val_font)
        dw = _text_w(draw, dash,       val_font)
        lw = _text_w(draw, losses_str, val_font)
        total_w = ww + dw + lw
        x_start = X_WL - total_w // 2
        _draw_v_center(draw, wins_str,   x_start,           y_c, val_font, GREEN)
        _draw_v_center(draw, dash,       x_start + ww,      y_c, val_font, SOFT_GRAY)
        _draw_v_center(draw, losses_str, x_start + ww + dw, y_c, val_font, RED)

        # Win% (toujours vert)
        _draw_xy_center(draw, f"{winpct}%", X_WINPCT, y_c, val_font, GREEN)

        # Matches (gris doux)
        _draw_xy_center(draw, str(matches), X_MATCHES, y_c, matches_font, SOFT_GRAY)

    # ── Footer ──────────────────────────────────────────────────
    footer_y_c = height - FOOTER_BAND // 2
    _draw_xy_center(draw, "Play'IT Matchmaking Bot",
                    WIDTH // 2, footer_y_c, footer_font, DIM_GRAY)

    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf
