"""
Tests d'integration du cog cogs/applications.py.

Couvre :
  - _parse_application_embed : parse l'ID + pseudo + flag staff depuis l'embed
  - _try_acquire_candidature_cooldown : CAS atomique cooldown 1h
  - ApplicationReviewView.accept : happy path + edge cases (no perm, embed
    corrompu, member absent, double-claim via CAS)
  - RefuseReasonModal.on_submit : graceful skip si member absent
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import mongomock

from cogs.applications import (
    ApplicationReviewView,
    RefuseReasonModal,
    _parse_application_embed,
    _try_acquire_candidature_cooldown,
)


# ── _parse_application_embed ─────────────────────────────────────
def _embed_with(*, title: str = "📋 Nouvelle candidature", footer_id: int | str | None = 42,
                fields: list[tuple[str, str]] | None = None) -> MagicMock:
    embed = MagicMock()
    embed.title = title
    footer = MagicMock()
    footer.text = f"ID: {footer_id}" if footer_id is not None else None
    embed.footer = footer
    embed.fields = []
    if fields:
        for name, value in fields:
            f = MagicMock()
            f.name = name
            f.value = value
            embed.fields.append(f)
    return embed


def _message_with_embeds(embeds: list) -> MagicMock:
    msg = MagicMock()
    msg.embeds = embeds
    return msg


def test_parse_embed_returns_id_pseudo_player():
    embed = _embed_with(
        title="📋 Nouvelle candidature",
        footer_id=42,
        fields=[("🎮 Pseudo en jeu", "Alice")],
    )
    msg = _message_with_embeds([embed])
    applicant_id, pseudo, is_staff = _parse_application_embed(msg)
    assert applicant_id == 42
    assert pseudo == "Alice"
    assert is_staff is False


def test_parse_embed_detects_staff_in_title():
    embed = _embed_with(
        title="📋 Nouvelle candidature Staff",
        footer_id=99,
        fields=[("🎮 Pseudo", "Bob")],
    )
    msg = _message_with_embeds([embed])
    _, _, is_staff = _parse_application_embed(msg)
    assert is_staff is True


def test_parse_embed_returns_none_when_no_embeds():
    msg = _message_with_embeds([])
    applicant_id, pseudo, is_staff = _parse_application_embed(msg)
    assert applicant_id is None
    assert pseudo == ""
    assert is_staff is False


def test_parse_embed_returns_none_on_invalid_footer():
    embed = _embed_with(
        title="📋 Nouvelle candidature",
        footer_id=None,
        fields=[("🎮 Pseudo en jeu", "Alice")],
    )
    msg = _message_with_embeds([embed])
    applicant_id, _, _ = _parse_application_embed(msg)
    assert applicant_id is None


def test_parse_embed_returns_none_on_non_numeric_footer():
    embed = _embed_with(title="X", footer_id="abc", fields=[("🎮 Pseudo", "A")])
    msg = _message_with_embeds([embed])
    applicant_id, _, _ = _parse_application_embed(msg)
    assert applicant_id is None


# ── _try_acquire_candidature_cooldown ─────────────────────────────
def test_cooldown_first_apply_returns_allowed():
    db = mongomock.MongoClient(tz_aware=True).db
    allowed, remaining = _try_acquire_candidature_cooldown(db, "user-1")
    assert allowed is True
    assert remaining == 0.0
    # Doc cree
    doc = db["candidature_cooldowns"].find_one({"_id": "user-1"})
    assert doc is not None


def test_cooldown_within_window_returns_blocked():
    db = mongomock.MongoClient(tz_aware=True).db
    # Pre-insert : il y a 30 minutes
    recent = datetime.now(UTC) - timedelta(minutes=30)
    db["candidature_cooldowns"].insert_one({"_id": "user-1", "last_apply": recent})
    allowed, remaining = _try_acquire_candidature_cooldown(db, "user-1")
    assert allowed is False
    assert remaining > 0
    # Approximativement 30 minutes restantes
    assert 1700 < remaining < 1850


def test_cooldown_after_window_returns_allowed():
    db = mongomock.MongoClient(tz_aware=True).db
    # Il y a 2 heures (au-dela des 60min)
    old = datetime.now(UTC) - timedelta(hours=2)
    db["candidature_cooldowns"].insert_one({"_id": "user-1", "last_apply": old})
    allowed, remaining = _try_acquire_candidature_cooldown(db, "user-1")
    assert allowed is True
    assert remaining == 0.0
    # Doc mis a jour
    doc = db["candidature_cooldowns"].find_one({"_id": "user-1"})
    assert doc["last_apply"] > old


# ── ApplicationReviewView.accept ──────────────────────────────────
def _fake_member(member_id: int, name: str = "Alice", *, manage_guild: bool = True) -> MagicMock:
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.guild_permissions.manage_guild = manage_guild
    m.roles = []
    avatar = MagicMock()
    avatar.url = "https://cdn.test/avatar.png"
    m.display_avatar = avatar
    m.send = AsyncMock()
    m.edit = AsyncMock()
    m.add_roles = AsyncMock()
    return m


def _fake_interaction(user, guild, message) -> MagicMock:
    inter = MagicMock()
    inter.user = user
    inter.guild = guild
    inter.guild_id = guild.id
    inter.message = message
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _fake_guild(guild_id: int, members: list[MagicMock] | None = None) -> MagicMock:
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.members = members or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == mid), None)
    g.roles = []
    return g


async def test_accept_happy_path_grants_role_and_validates():
    from services import repository
    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    applicant = _fake_member(42, "Alice", manage_guild=False)
    guild = _fake_guild(99, members=[admin, applicant])

    # Role "Members"
    members_role = MagicMock()
    members_role.name = "Members"
    guild.roles = [members_role]

    embed = _embed_with(
        title="📋 Nouvelle candidature",
        footer_id=42,
        fields=[("🎮 Pseudo en jeu", "Alice")],
    )
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    message.edit = AsyncMock()

    inter = _fake_interaction(admin, guild, message)
    # Pre-enregistrement obligatoire : claim_application_decision est un CAS
    # sur status=pending qui requiert un doc existant.
    repository.register_application(db, guild.id, message.id, applicant.id, is_staff=False)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # CAS DB : status passe a "accepted"
    app = repository.get_applications_col(db, guild.id).find_one({"_id": str(message.id)})
    assert app is not None and app.get("status") == "accepted"
    # Role grant + DM ont ete tentes
    applicant.add_roles.assert_awaited()
    inter.followup.send.assert_awaited()


async def test_accept_refuses_when_no_permission():
    db = mongomock.MongoClient(tz_aware=True).db
    non_admin = _fake_member(1, "User", manage_guild=False)
    applicant = _fake_member(42, "Alice", manage_guild=False)
    guild = _fake_guild(99, members=[non_admin, applicant])

    embed = _embed_with(footer_id=42, fields=[("🎮 Pseudo en jeu", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    inter = _fake_interaction(non_admin, guild, message)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "permission" in args[0].lower()
    # Aucun side-effect : pas de role grant.
    applicant.add_roles.assert_not_awaited()


async def test_accept_bails_on_corrupted_embed_without_cas():
    """Bug critique audit : le CAS doit etre apres validation pour eviter
    l'etat coince. Verifie qu'un embed sans applicant_id ne consomme PAS
    le CAS DB."""
    from services import repository
    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild(99, members=[admin])

    # Embed sans footer ID -> applicant_id = None
    embed = _embed_with(footer_id=None, fields=[])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    inter = _fake_interaction(admin, guild, message)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # Le followup doit dire "embed corrompu"
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "illisibles" in msg or "corrompu" in msg

    # CRITIQUE : la candidature ne doit PAS etre marquee accepted en DB
    # (sinon le candidat reste coince).
    apps_col = repository.get_applications_col(db, guild.id)
    app_doc = apps_col.find_one({"_id": str(message.id)})
    assert app_doc is None, (
        "Bug audit : CAS execute alors que validation a echoue. "
        "Le candidat est maintenant coince en etat 'deja traite'."
    )


async def test_accept_bails_on_missing_member_without_cas():
    """Meme principe : si get_member renvoie None, pas de CAS consume."""
    from services import repository
    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    # Pas de membre 42 dans la guild -> applicant manque
    guild = _fake_guild(99, members=[admin])

    embed = _embed_with(footer_id=42, fields=[("🎮 Pseudo en jeu", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    inter = _fake_interaction(admin, guild, message)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # Followup : Membre introuvable
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "introuvable" in msg.lower()

    # CAS NON consume -> retry possible
    apps_col = repository.get_applications_col(db, guild.id)
    assert apps_col.find_one({"_id": str(message.id)}) is None


async def test_accept_rejects_double_claim_via_cas():
    """Deux admins cliquent en concurrence : seul un wins le CAS."""
    from services import repository
    db = mongomock.MongoClient(tz_aware=True).db
    admin1 = _fake_member(1, "Admin1", manage_guild=True)
    admin2 = _fake_member(2, "Admin2", manage_guild=True)
    applicant = _fake_member(42, "Alice", manage_guild=False)
    guild = _fake_guild(99, members=[admin1, admin2, applicant])

    members_role = MagicMock()
    members_role.name = "Members"
    guild.roles = [members_role]

    embed = _embed_with(footer_id=42, fields=[("🎮 Pseudo en jeu", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    message.edit = AsyncMock()

    # Pre-claim par admin2 : la candidature est deja "refused" en DB.
    repository.register_application(db, guild.id, message.id, applicant.id, is_staff=False)
    claimed = repository.claim_application_decision(
        db, guild.id, message.id,
        status="refused", decided_by=admin2.id,
    )
    assert claimed is not None

    # Admin1 tente d'accepter en seconde -> doit echouer proprement
    inter = _fake_interaction(admin1, guild, message)
    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # Le followup doit dire "deja traitee"
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "deja" in msg.lower() or "déjà" in msg.lower()
    # Aucun role grant sur l'applicant
    applicant.add_roles.assert_not_awaited()


# ── RefuseReasonModal : member None graceful skip ─────────────────
async def test_refuse_modal_skips_dm_kick_when_member_gone():
    """Si le candidat a quitte le serveur entre le clic 'Refuser' et la
    soumission du modal, le DM/kick sont gracieusement skip et l'embed
    est quand meme update (etat DB + message coherents)."""
    from services import repository
    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    # Pas de candidat 42 dans la guild
    guild = _fake_guild(99, members=[admin])

    embed = _embed_with(footer_id=42, fields=[("🎮 Pseudo en jeu", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    message.edit = AsyncMock()

    inter = _fake_interaction(admin, guild, message)
    # Pre-register la candidature (sinon claim retourne None)
    repository.register_application(db, guild.id, message.id, 42, is_staff=False)

    modal = RefuseReasonModal(db=db, applicant_id=42)
    modal.reason = MagicMock()
    modal.reason.value = "Pas convaincu"

    await modal.on_submit(inter)

    # CAS consumed - candidature marquee refused
    apps_col = repository.get_applications_col(db, guild.id)
    app = apps_col.find_one({"_id": str(message.id)})
    assert app is not None
    assert app.get("status") == "refused"

    # Embed update tente meme sans membre
    message.edit.assert_awaited()
    # Followup affiche succes
    inter.followup.send.assert_awaited()
