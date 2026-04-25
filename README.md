# VRC Bot

Bot Discord pour la gestion de matchs Valorant 10mans (5v5 customs) avec système ELO, file d'attente, vote MVP et leaderboard graphique.

Réservé aux comptes **Immortal+** sur la région **EU**.

## Fonctionnalités

- File d'attente 10 joueurs avec équilibrage automatique des équipes (par ELO)
- Vote MVP / résultat de match
- Liaison de compte Riot via l'API HenrikDev (vérification du peak rank Immortal+)
- Système ELO proportionnel à la moyenne du match
- Leaderboard généré en image (Pillow) à la VRC/GC French Matchmaking
- Stockage MongoDB (joueurs, matchs, ELO)

## Prérequis

- Python 3.11+
- MongoDB (local ou distant)
- Un bot Discord avec **Server Members Intent** activé
- (Optionnel) Une clé API HenrikDev pour relever la rate limit

## Installation

```bash
# Cloner le repo
git clone <url-du-repo>
cd "vrc bot"

# Installer les dépendances
pip install -r requirements.txt

# Copier la config et la remplir
cp .env.example .env
# Editer .env avec ton DISCORD_TOKEN, MONGO_URL, etc.
```

## Lancement

```bash
python bot.py
```

## Tests

```bash
pip install -r requirements-test.txt
pytest
```

Plus de 230 tests couvrent l'ELO, le balancing, l'API Riot, les cogs, les votes et la file d'attente.

## Structure

```
.
├── bot.py                  # Entrée principale du bot
├── cogs/                   # Cogs Discord (match, queue_v2, riot_link)
├── services/               # Logique métier (elo_calc, riot_api, repository, ...)
├── leaderboard_img.py      # Génération du leaderboard PIL
├── seed_users.py           # Script pour seeder des utilisateurs de test
├── preview_leaderboard.py  # Aperçu local du leaderboard
└── test_*.py               # Suite de tests pytest
```

## Variables d'environnement

| Nom              | Obligatoire | Description                                       |
|------------------|:-----------:|---------------------------------------------------|
| `DISCORD_TOKEN`  | oui         | Token du bot Discord                              |
| `MONGO_URL`      | oui         | URL de connexion MongoDB                          |
| `HENRIK_API_KEY` | non         | Clé API HenrikDev (rate limit étendue)            |

## Licence

MIT — voir [LICENSE](LICENSE).
