# The Hub — Bot Discord Valorant 10mans

Bot Discord de matchmaking Valorant 5v5 customs (10mans) pour communauté **EU Immortal+**.
Gère **3 queues parallèles** (Pro / Open / GC), équilibrage automatique des équipes par ELO,
vote de résultat, vérification HenrikDev avec pondération ACS, et leaderboard généré en image.

---

## Sommaire

- [Aperçu](#aperçu)
- [Fonctionnalités principales](#fonctionnalités-principales)
- [Architecture](#architecture)
- [Installation locale](#installation-locale)
- [Variables d'environnement](#variables-denvironnement)
- [Configuration Discord](#configuration-discord)
- [Commandes](#commandes)
- [Système ELO](#système-elo)
- [Vérification HenrikDev (ACS)](#vérification-henrikdev-acs)
- [Déploiement Kimsufi + PM2](#déploiement-kimsufi--pm2)
- [Tests](#tests)
- [Stack technique](#stack-technique)
- [Licence](#licence)

---

## Aperçu

Cycle complet d'une partie :

1. 10 joueurs cliquent **Rejoindre** sur le message persistant de leur queue (Pro / Open / GC).
2. À 10/10, le bot ferme la queue, **équilibre les équipes** (brute-force optimal sur les
   126 partitions 5+5), assigne une catégorie `Match #1..#5` libre, choisit map + lobby host.
3. Les rôles Discord `Match #N` sont attribués **en parallèle**, puis le message d'annonce
   est posté avec 2 boutons de vote.
4. Les joueurs sont déplacés vocalement vers `Waiting Match` de leur catégorie.
5. Après la partie, **7/10 votes** suffisent pour valider (cliquer "Team A" / "Team B").
6. 5 min plus tard, le bot interroge **HenrikDev** pour récupérer les stats du custom et
   appliquer un multiplicateur **ACS** sur les gains/pertes ELO. Si Henrik ne trouve pas
   le match dans les 30 min, ELO plat appliqué.
7. Le leaderboard est régénéré automatiquement (image PIL) dans `#leaderboard`.

---

## Fonctionnalités principales

### 3 queues simultanées avec gates de rôle

| Queue  | Salon dédié   | Rôle requis              | Vocal d'attente        |
|--------|---------------|--------------------------|------------------------|
| Pro    | `#pro-queue`  | `Rank S \| Pro Queue`    | `Waiting Room Pro`     |
| Open   | `#open-queue` | aucun                    | `Waiting Room Open`    |
| GC     | `#gc-queue`   | `GC`                     | `Waiting Room GC`      |

- Un joueur ne peut être que dans **une seule queue à la fois**.
- Les boutons Rejoindre / Quitter sont **persistants** (survivent au restart du bot).
- Refusé si pas de compte Riot lié, déjà en queue, en match en cours, ou rôle gate manquant.
- Le joueur qui quitte le serveur est automatiquement retiré des queues (`on_member_remove`).

### Équilibrage des équipes

- Algo **brute-force optimal** : itère les 126 partitions 5+5 uniques parmi 10.
- Minimise `|sum(team_a) - sum(team_b)|`.
- Tie-breakers : peak diff puis ordre des IDs (déterministe).
- Source d'ELO utilisée : **ELO serveur** (`elo_<guild>.elo`), seedée au `/link-riot`.

### Formation de match — 5 catégories en parallèle

- Catégories supportées : `Match #1` à `Match #5`.
- Une catégorie est "libre" si `Team 1`, `Team 2` et `Waiting Match` sont vides
  et que le salon `match-preparation` existe.
- **Parallélisation Discord API** (depuis v3) : les 10 grants de rôles + 10 voice moves
  sont exécutés en `asyncio.gather`, réduisant le temps de formation de ~7-10 s à ~1.5-2 s.
- Invariants préservés : rôles `Match #N` attribués **avant** l'envoi du message d'annonce
  (sinon les joueurs ne voient pas le salon).

### Vote de résultat

- 2 boutons attachés au message du match : `Team A a gagné` / `Team B a gagné`.
- **Seuls les 10 participants** peuvent voter. Vote modifiable.
- **Majorité 7/10** → match validé automatiquement (transition CAS atomique : pas de double validation).
- Timeout **60 min** sans majorité → status `contested`, ping du rôle admin avec score actuel.
- Le rôle `Match #N` est révoqué immédiatement après validation pour libérer la nouvelle queue.

### Vérification HenrikDev + pondération ACS

- ~5 min après validation, le bot interroge l'API HenrikDev pour retrouver le custom joué.
- Si trouvé → calcule un **multiplicateur ACS** par joueur (perf individuelle).
- Si pas trouvé après 30 min → ELO plat appliqué (gain/loss = 16 chacun).
- **Circuit breaker** : si 3 appels Henrik consécutifs échouent, on suspend les
  tentatives pendant 5 min (évite de saturer les threads et de polluer les logs).

### Leaderboard

- 3 leaderboards distincts cohabitent dans `#leaderboard` (un par queue_type).
- Image PNG générée via Pillow (`leaderboard_img.py`), 15 joueurs par page.
- Pagination par boutons `<` / `>` **persistante après restart**.
- Auto-refresh après chaque modification ELO (debounced per-guild).

### Système de candidatures (héritage)

- `/welcome` pose un bouton **Postuler** persistant dans `#verify`.
- Modale Joueur (pseudo, tracker, expérience) ou Staff (poste, expérience).
- Cooldown 1h entre deux candidatures par utilisateur.
- Admin accepte → rôle `Members` (ou `Coach/Analyst/Manager`) + rename + DM.
- Admin refuse → DM avec raison + kick.

---

## Architecture

```
bot.py                     # Entry point : tree slash commands + prefix commands + candidatures
cogs/
  ├── queue_v2.py          # QueueCog + QueueView (Rejoindre/Quitter, 3 queues)
  ├── match.py             # MatchCog + VoteView (formation, vote, Henrik, ELO update, cleanup roles)
  └── riot_link.py         # /link-riot, /unlink-riot
services/
  ├── elo_calc.py          # Constantes (ELO_START=2000, BASE=16) + helpers purs
  ├── elo_mapping.py       # Conversion tier numérique <-> nom (Iron 1 → Radiant)
  ├── elo_updater.py       # apply_match_validation : distribue gains/pertes avec multiplicateurs ACS
  ├── leaderboard_refresh.py  # LeaderboardView paginée + refresh debounced
  ├── match_service.py     # Logique pure : build_players, plan_match, find_free_match_prep
  ├── match_verifier.py    # find_henrik_custom_match + compute_acs_multipliers
  ├── repository.py        # Accès MongoDB centralisé (toutes les collections)
  ├── riot_api.py          # Client HenrikDev (cache 1h, retry, gestion 404/429)
  ├── riot_id.py           # Parsing Riot ID (Pseudo#TAG)
  └── team_balancer.py     # Brute-force optimal sur 126 partitions
leaderboard_img.py         # Génération PNG du leaderboard (Pillow)
preview_leaderboard.py     # Outil dev : aperçu local du leaderboard
seed_users.py              # Outil dev : peuple Mongo de faux joueurs
test_*.py                  # 255 tests pytest
```

### Couches

- **`services/`** = logique pure, testable sans Discord ni Mongo (sauf `repository.py`).
- **`cogs/`** = wiring Discord : reçoit les interactions, appelle `services/`, applique les side-effects.
- **`bot.py`** = entry point + commandes legacy (candidatures, `/win` manuel, `/setup`).

### Collections MongoDB (`elobot` database)

| Collection                  | Contenu                                                                 |
|-----------------------------|-------------------------------------------------------------------------|
| `elo_<guild_id>`            | ELO serveur par joueur+queue (`_id = "<uid>:<queue_type>"`)             |
| `riot_accounts_<guild_id>`  | Lien Discord ↔ Riot (puuid, effective_elo, peak, source)                |
| `queue_<guild_id>`          | Queues actives (1 doc par queue_type, `_id = "active:<qt>"`)            |
| `matches_<guild_id>`        | Historique des matchs (teams, votes, status, ACS, cleanup flags)        |
| `bypass`                    | Rôles ayant accès aux commandes admin (per-guild)                       |
| `candidature_cooldowns`     | Cooldowns 1h pour le système de candidatures                            |

---

## Installation locale

### Prérequis

- Python **3.11+** (testé sur 3.12 et 3.13)
- MongoDB (local ou Atlas)
- Un bot Discord avec **Server Members Intent** activé

### Setup

```bash
git clone <url-du-repo>
cd "The Hub"

python -m venv venv
source venv/bin/activate         # Windows : venv\Scripts\activate

pip install -r requirements.txt

# Copier le template et remplir les valeurs
cp .env.example .env
# DISCORD_TOKEN, MONGO_URI, HENRIK_API_KEY (optionnel)

python bot.py
```

---

## Variables d'environnement

| Nom              | Obligatoire | Description                                                  |
|------------------|:-----------:|--------------------------------------------------------------|
| `DISCORD_TOKEN`  | oui         | Token du bot Discord                                         |
| `MONGO_URI`      | oui         | URI MongoDB (Atlas ou local : `mongodb://localhost:27017`)   |
| `HENRIK_API_KEY` | non         | Clé HenrikDev (augmente la rate limit, recommandé en prod)   |

Le `.env` n'est **jamais** déployé via CI (exclu du rsync et `.gitignore`).

---

## Configuration Discord

### Setup automatique

```
/setup
```

Crée la catégorie `🎮 Valorant 10mans` et tous les salons textuels nécessaires
(`leaderboard`, `pro-queue`, `open-queue`, `gc-queue`, `matchs`), pose les 3 messages
de queue, et pré-poste les 3 leaderboards. **Idempotent**, ré-exécutable sans risque.

### Manuel (à créer si nécessaire)

**Catégories de matchs** (obligatoires pour que la formation fonctionne) :
- `Match #1`, `Match #2`, `Match #3`, `Match #4`, `Match #5`
- Chaque catégorie doit contenir :
  - `Team 1` (vocal)
  - `Team 2` (vocal)
  - `Waiting Match` (vocal)
  - `match-preparation` (texte)

**Rôles** :
- `En Queue` (donné aux joueurs en queue)
- `Match #1`..`Match #5` (donnés pendant la durée d'un match — gate de visibilité)
- `Match Host` (donné au lobby leader, retiré après 10 min)
- `Rank S | Pro Queue` (gate de la queue Pro)
- `GC` (gate de la queue GC)
- `Admin` / `Match Staff` / `Administrateur` (ping si vote en timeout)
- `Members`, `Coach/Analyst/Manager` (système de candidatures, optionnel)

**Salons annexes** (optionnels) :
- `verify` : pour `/welcome` (bouton candidature)
- `candidatures` : pour les modales soumises
- `elo-adding` : annonces de vérification Henrik

### Permissions Discord requises

Le bot doit avoir : `Voir les salons`, `Envoyer des messages`, `Intégrer des liens`,
`Joindre des fichiers`, `Gérer les salons` (pour `/setup`), `Gérer les messages` (pour `/clear`),
`Déplacer les membres` (pour les voice moves), `Gérer les rôles`, `Utiliser les commandes slash`.

---

## Commandes

### Joueurs

| Commande              | Description                                                              |
|-----------------------|--------------------------------------------------------------------------|
| `/link-riot riot_id:` | Lie le compte Discord à un compte Valorant (EU, Immortal+ requis)        |
| `/unlink-riot`        | Supprime le lien Riot                                                    |
| `/leaderboard queue:` | Affiche le classement de la queue choisie (Pro/Open/GC)                  |
| `/stats queue: @joueur` | Stats ELO d'un joueur dans la queue choisie (éphémère)                 |
| `/coinflip`           | Pile ou face                                                             |

### Admin — Setup

| Commande                       | Description                                              |
|--------------------------------|----------------------------------------------------------|
| `/setup`                       | Crée catégorie + salons + pose les 3 messages de queue   |
| `/setup-queue queue:`          | Re-pose le message d'une queue manuellement              |
| `/close-queue queue:`          | Ferme la queue active d'un type                          |
| `/welcome`                     | Pose le bouton **Postuler** dans `#verify`               |
| `/report`                      | Pose le message de report dans le salon courant          |
| `/bypass role:`                | Donne accès aux commandes admin à un rôle                |

### Admin — Match

| Commande                                       | Description                                          |
|------------------------------------------------|------------------------------------------------------|
| `/match-cancel`                                | Annule le match en cours dans ce salon               |
| `/match-replace quitter: remplacant:`          | Remplace un joueur (ELO diff < 500 requis)           |

### Admin — ELO manuel (par queue)

| Commande                                              | Description                                  |
|-------------------------------------------------------|----------------------------------------------|
| `/win queue: @j1..@j5`                                | Enregistre une victoire manuelle             |
| `/lose queue: @j1..@j5`                               | Enregistre une défaite manuelle              |
| `/elomodify queue: @joueur action: montant:`          | Ajoute/retire de l'ELO                       |
| `/winmodify queue: @joueur action: montant:`          | Ajoute/retire des victoires                  |
| `/losemodify queue: @joueur action: montant:`         | Ajoute/retire des défaites                   |
| `/resetelo queue: joueur: \| all:True`                | Reset ELO à 2000 (joueur ou tous)            |
| `/reset-queue queue:`                                 | Drop toutes les données d'une queue          |

### Admin — Utilitaires

| Commande           | Description                                  |
|--------------------|----------------------------------------------|
| `/map`             | Map aléatoire parmi les 7 maps               |
| `/clear nombre:`   | Supprime jusqu'à 100 messages                |
| `/help type:`      | Liste des commandes (membres ou admin)       |

### Commandes prefix (legacy)

`!leaderboard`, `!stats`, `!win`, `!lose`, `!map` — comportement équivalent
aux slash mais avec syntaxe prefix. Conservées pour compat ascendante.

---

## Système ELO

- **ELO de départ** : `2000`
- **Base zero-sum** : gain = loss = `16` par match (formule constante quelle que soit la moyenne).
- **Pondération ACS** : multiplicateur calculé par joueur via stats HenrikDev.
- **Plancher** : ELO d'un perdant ne descend jamais sous `0`.
- **Wins / losses** : incrémentés automatiquement à chaque validation.
- **Per-queue** : chaque joueur a un ELO indépendant par queue (Pro / Open / GC), via
  un compound `_id = "<uid>:<queue_type>"` dans la collection `elo_<guild_id>`.

### Restriction Immortal+

Le bot refuse de lier un compte dont le `max(peak_elo, current_mmr) < 2400`.
Calcul de l'effective ELO :
- Si peak < 6 mois → peak utilisé.
- Sinon → moyenne des MMR sur les 6 derniers mois.
- Fallback peak si pas de matchs récents.

---

## Vérification HenrikDev (ACS)

| Constante                            | Valeur  | Effet                                              |
|--------------------------------------|---------|----------------------------------------------------|
| `HENRIK_VERIFY_DELAY_MINUTES`        | 5 min   | Premier essai de récupération du custom            |
| `HENRIK_VERIFY_TIMEOUT_MINUTES`      | 30 min  | Abandon → ELO plat (16/16)                         |
| `HENRIK_CIRCUIT_FAIL_THRESHOLD`      | 3       | Échecs consécutifs avant ouverture du circuit      |
| `HENRIK_CIRCUIT_OPEN_MINUTES`        | 5 min   | Durée de suspension des appels Henrik              |

Le multiplicateur ACS récompense les top frags et pénalise les bottom frags **à l'intérieur
de leur propre équipe**, en gardant la somme zero-sum.

---

## Déploiement Kimsufi + PM2

Le bot tourne sur un **serveur Kimsufi** (OVH) via PM2, déployé automatiquement par
**GitHub Actions** à chaque push sur `main`. Détails complets dans `DEPLOY.md`.

### Pipeline CI/CD

- `.github/workflows/ci.yml` : `pytest` sur chaque PR + chaque push (sauf `main`).
- `.github/workflows/deploy.yml` : sur push `main` → tests → rsync vers Kimsufi →
  `pip install` → `pm2 reload vrc-bot`.

### Commandes PM2 utiles

```bash
pm2 status
pm2 logs vrc-bot --lines 100
pm2 restart vrc-bot
pm2 restart vrc-bot --update-env   # après édition du .env
pm2 monit
```

### Modifier le `.env` en prod

```bash
ssh ubuntu@<kimsufi-host>
nano /home/ubuntu/vrc-bot/.env
pm2 restart vrc-bot --update-env
```

---

## Tests

```bash
pip install -r requirements-test.txt
pytest
```

**255 tests** automatisés, exécutés en ~10 s. Couverture :

| Module                         | Aspects testés                                                 |
|--------------------------------|----------------------------------------------------------------|
| `test_elo_calc.py`             | Formules ELO + ACS                                             |
| `test_elo_updater.py`          | Distribution gains/pertes en base                              |
| `test_team_balancer.py`        | Algo brute-force et tie-breakers                               |
| `test_match_service.py`        | `build_players`, `plan_match`, `find_free_match_prep`          |
| `test_match_cog.py`            | Intégration formation de match (Discord mocké)                 |
| `test_vote.py`                 | Vote, transitions CAS, timeout, MAJ ELO                        |
| `test_queue_v2.py`             | Repository + QueueView + confirmation éphémère                 |
| `test_riot_api.py`             | Client HenrikDev (mocks HTTP, cache, 404/429)                  |
| `test_riot_link.py`            | Cog `/link-riot` + check Immortal+                             |
| `test_riot_id.py`              | Parsing Riot ID                                                |
| `test_pagination.py`           | Logique de pagination du leaderboard                           |
| `test_repository_helpers.py`   | Helpers Mongo (compound id, CAS)                               |
| `test_bot_slash.py`            | Slash commands + `/setup` + `/win` (Discord mocké)             |
| `test_bot_prefix.py`           | Commandes prefix legacy (dpytest)                              |

---

## Stack technique

- **Python** 3.11+
- **discord.py** 2.3.2
- **pymongo** 4.6+ (avec `retryWrites`, `retryReads`, `serverSelectionTimeoutMS=5000`)
- **Pillow** 10+ (rendu PNG du leaderboard)
- **requests** 2.31+ (client HenrikDev)
- **python-dotenv** 1.0+
- **pytest** 8+ / **pytest-asyncio** / **mongomock** / **dpytest** / **faker** (tests)

---

## Licence

MIT — voir [LICENSE](LICENSE).
