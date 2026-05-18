"""One-shot migration : élève elo_<guild>/riot_accounts_<guild>/matches_<guild>
en collections partagées elo/riot/matches.

Usage:
    MONGO_URL=mongodb://... MIGRATE_SOURCE_GUILD_ID=<guild_A_id> \
        python scripts/migrate_shared_collections.py

Idempotent (replace_one upsert). Les anciennes collections sont renommées en
archive_<timestamp>_<source_name> (pas supprimées) pour permettre un rollback.

Notes:
- riot était historiquement nommée `riot_accounts_<guild>`, pas `riot_<guild>`.
- elo et matches suivent le pattern `<name>_<guild>`.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from pymongo import MongoClient


def main() -> int:
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        print("ERROR: MONGO_URL not set", file=sys.stderr)
        return 1
    raw_guild = os.environ.get("MIGRATE_SOURCE_GUILD_ID")
    if not raw_guild:
        print("ERROR: MIGRATE_SOURCE_GUILD_ID not set", file=sys.stderr)
        return 1
    source_guild_id = int(raw_guild)

    client = MongoClient(mongo_url)
    db = client["elobot"]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    mappings = {
        "elo": f"elo_{source_guild_id}",
        "riot": f"riot_accounts_{source_guild_id}",
        "matches": f"matches_{source_guild_id}",
    }

    for dst_name, src_name in mappings.items():
        if src_name not in db.list_collection_names():
            print(f"  skip {src_name} (not present — already migrated?)")
            continue

        src = db[src_name]
        dst = db[dst_name]

        n = 0
        for doc in src.find():
            dst.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            n += 1
        print(f"  copied {n} docs : {src_name} -> {dst_name}")

        archive_name = f"archive_{stamp}_{src_name}"
        db[src_name].rename(archive_name, dropTarget=True)
        print(f"  archived : {src_name} -> {archive_name}")

    print("Migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
