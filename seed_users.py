"""
Genere N faux joueurs dans MongoDB pour tester le bot avec de vraies donnees.

Utile pour tester /leaderboard, /stats, /resetelo etc. dans Discord
avec un classement bien rempli, sans avoir a creer 30 vrais joueurs.

Prerequis:
    pip install faker pymongo

Variables d'environnement:
    MONGO_URL       (defaut: mongodb://localhost:27017)
    TEST_GUILD_ID   (obligatoire : l'ID de ton serveur Discord de test)
    N_USERS         (defaut: 30)

Usage:
    set TEST_GUILD_ID=123456789012345678
    python seed_users.py

    # Pour reset les faux joueurs ensuite :
    python seed_users.py --clean
"""

import os
import sys
import random

try:
    from pymongo import MongoClient
    from faker import Faker
except ImportError:
    print("[ERREUR] Installe les dependances: pip install pymongo faker")
    sys.exit(1)

MONGO_URL     = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
TEST_GUILD_ID = os.environ.get("TEST_GUILD_ID")
N_USERS       = int(os.environ.get("N_USERS", "30"))

if not TEST_GUILD_ID:
    print("[ERREUR] Defini la variable TEST_GUILD_ID (ID de ton serveur Discord de test).")
    print("        ex: set TEST_GUILD_ID=123456789012345678")
    sys.exit(1)

# Faux IDs Discord : on prend des snowflakes a partir d'une base reservee aux tests
# (les vrais IDs Discord ont ~18 chiffres, on prefixe par 9999 pour les distinguer)
FAKE_ID_PREFIX = 9999

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
try:
    client.admin.command("ping")
except Exception as e:
    print(f"[ERREUR] MongoDB inaccessible a {MONGO_URL}: {e}")
    sys.exit(1)

db  = client["elobot"]
col = db[f"elo_{TEST_GUILD_ID}"]

# Mode --clean : supprime uniquement les faux joueurs (preserve les vrais)
if "--clean" in sys.argv:
    res = col.delete_many({"_id": {"$regex": f"^{FAKE_ID_PREFIX}"}})
    print(f"[ok] {res.deleted_count} faux joueurs supprimes de elo_{TEST_GUILD_ID}")
    sys.exit(0)

# ── Generation ────────────────────────────────────────────────────
fake = Faker(["fr_FR", "en_US"])
random.seed(42)

inserted = 0
for i in range(N_USERS):
    fake_id = f"{FAKE_ID_PREFIX}{i:014d}"  # ex: 9999000000000000000
    wins   = random.randint(0, 50)
    losses = random.randint(0, 50)
    kills  = random.randint(wins * 5, wins * 25 + losses * 10)
    deaths = random.randint(losses * 5, losses * 20 + wins * 8)
    elo    = max(0, wins * 17 - losses * 12 + random.randint(-30, 30))
    col.update_one(
        {"_id": fake_id},
        {"$set": {
            "name":   fake.user_name()[:20],
            "elo":    elo,
            "wins":   wins,
            "losses": losses,
            "kills":  kills,
            "deaths": deaths,
        }},
        upsert=True,
    )
    inserted += 1

print(f"[ok] {inserted} faux joueurs inseres dans elo_{TEST_GUILD_ID}")
print(f"     /leaderboard devrait afficher au moins {inserted} entrees.")
print(f"     Pour les nettoyer plus tard : python seed_users.py --clean")
print()
print("[!] Les avatars ne s'afficheront pas dans Discord (faux IDs).")
print("    Pour un test visuel complet, utilise plutot preview_leaderboard.py")
