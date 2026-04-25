"""
Test visuel du leaderboard SANS Discord.

Genere N faux joueurs et rend chaque page du leaderboard en PNG dans ./leaderboard_preview/.
Si le rendu PNG plante sur la page 2+, c'est que le bug est dans generate_leaderboard,
pas dans la pagination Discord.

Prerequis:
    pip install faker pillow requests

Usage:
    python preview_leaderboard.py            # 30 joueurs (2 pages)
    python preview_leaderboard.py 16         # 16 joueurs (2 pages, derniere page = 1 joueur)
    python preview_leaderboard.py 100        # 100 joueurs (7 pages)
    python preview_leaderboard.py 15         # 15 joueurs (1 page)
"""

import os
import sys
import random
from pathlib import Path

try:
    from faker import Faker
except ImportError:
    print("[ERREUR] Installe faker: pip install faker")
    sys.exit(1)

try:
    from leaderboard_img import generate_leaderboard
except ImportError:
    print("[ERREUR] leaderboard_img.py introuvable.")
    print("Place-le dans le meme dossier que ce script ou dans le PYTHONPATH.")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────
PAGE_SIZE = 15
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
OUT_DIR = Path(__file__).parent / "leaderboard_preview"
OUT_DIR.mkdir(exist_ok=True)

# Avatars Discord par defaut (publics, pas besoin de login)
DEFAULT_AVATARS = [
    f"https://cdn.discordapp.com/embed/avatars/{i}.png" for i in range(6)
]

# ── Generation des faux joueurs ───────────────────────────────────
fake = Faker(["fr_FR", "en_US"])
random.seed(42)  # reproductible

players = []
for _ in range(N):
    wins   = random.randint(0, 50)
    losses = random.randint(0, 50)
    kills  = random.randint(wins * 5, wins * 25 + losses * 10)
    deaths = random.randint(losses * 5, losses * 20 + wins * 8)
    elo    = max(0, wins * 17 - losses * 12 + random.randint(-30, 30))
    players.append({
        "name":       fake.user_name()[:20],
        "elo":        elo,
        "wins":       wins,
        "losses":     losses,
        "kills":      kills,
        "deaths":     deaths,
        "avatar_url": random.choice(DEFAULT_AVATARS),
    })

# Tri par ELO decroissant + assignation des rangs (comme dans le bot)
players.sort(key=lambda p: -p["elo"])
for rank, p in enumerate(players, start=1):
    p["rank"] = rank

# ── Rendu de chaque page ──────────────────────────────────────────
total_pages = max(1, (N + PAGE_SIZE - 1) // PAGE_SIZE)
print(f"[info] {N} joueurs -> {total_pages} page(s) de {PAGE_SIZE} max")
print(f"[info] Sortie : {OUT_DIR}\n")

errors = 0
for page in range(total_pages):
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    out_path = OUT_DIR / f"page_{page + 1}.png"
    try:
        buf = generate_leaderboard(chunk, server_name="Serveur Test")
        if hasattr(buf, "read"):
            buf.seek(0)
            out_path.write_bytes(buf.read())
        elif isinstance(buf, (bytes, bytearray)):
            out_path.write_bytes(bytes(buf))
        elif isinstance(buf, str) and os.path.isfile(buf):
            out_path.write_bytes(Path(buf).read_bytes())
        else:
            print(f"  [warn] Page {page + 1}: type inconnu ({type(buf).__name__})")
            errors += 1
            continue
        size_kb = out_path.stat().st_size // 1024
        print(f"  [ok]   Page {page + 1}/{total_pages} -> {out_path.name} "
              f"({len(chunk)} joueurs, {size_kb} Ko)")
    except Exception as e:
        import traceback
        print(f"  [FAIL] Page {page + 1}: {type(e).__name__}: {e}")
        traceback.print_exc()
        errors += 1

print()
if errors:
    print(f"[!] {errors} page(s) en erreur. C'est probablement le bug que tu cherches.")
    sys.exit(1)
else:
    print(f"[ok] {total_pages} page(s) rendues. Ouvre-les pour verifier visuellement.")
