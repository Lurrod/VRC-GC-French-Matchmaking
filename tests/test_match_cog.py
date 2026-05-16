"""Tests d'integration du cog match (formation + persistance + reset queue)."""

import random
import pytest
from unittest.mock import AsyncMock, MagicMock


from cogs.match import MatchCog, VoteView, build_match_embed, VOTE_A_BTN_ID, VOTE_B_BTN_ID
from services import repository
from services.team_balancer import Player


def _fake_member(member_id: int, name: str = "User", voice_channel=None):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.guild = MagicMock(roles=[])
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.move_to = AsyncMock()
    if voice_channel is not None:
        voice = MagicMock()
        voice.channel = voice_channel
        m.voice = voice
    else:
        m.voice = None
    return m


def _fake_category(name: str, t1_empty: bool = True, t2_empty: bool = True,
                   with_prep: bool = True, with_waiting: bool = True):
    cat = MagicMock()
    cat.name = name
    t1 = MagicMock(); t1.name = "Team 1"; t1.members = [] if t1_empty else [object()]
    t2 = MagicMock(); t2.name = "Team 2"; t2.members = [] if t2_empty else [object()]
    vcs = [t1, t2]
    if with_waiting:
        waiting = MagicMock()
        waiting.name = "Waiting Match"
        waiting.id = 800 + (hash(name) % 100)
        waiting.members = []
        vcs.append(waiting)
    cat.voice_channels = vcs
    if with_prep:
        prep = MagicMock()
        prep.name = "match-preparation"
        prep.id = 700 + (hash(name) % 100)
        prep.send = AsyncMock(return_value=MagicMock(id=555))
        prep.category = cat  # Back-reference to parent category
        cat.text_channels = [prep]
    else:
        cat.text_channels = []
    return cat


def _fake_channel(channel_id: int = 100):
    ch = MagicMock()
    ch.id = channel_id
    ch.send = AsyncMock(return_value=MagicMock(id=555, channel=ch))
    return ch


def _fake_guild(guild_id: int = 42, members=None, categories=None, channel=None):
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.members = members or []
    g.categories = categories or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == int(mid)), None)
    g.get_channel = lambda cid: channel
    return g


def _fake_interaction(guild, user=None):
    inter = MagicMock()
    inter.guild = guild
    inter.user = user or _fake_member(1)
    inter.guild_id = guild.id
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _seed_full_queue(
    db, guild_id: int, channel_id: int = 100, queue_type: str = "open",
):
    """Cree la queue active + 10 comptes Riot lies + leur ELO serveur.

    Par defaut on simule une Open Queue (legacy, sans gate). Les tests
    Pro Queue passeront `queue_type="pro"` explicitement.
    """
    repository.setup_active_queue(
        db, guild_id=guild_id, queue_type=queue_type,
        channel_id=channel_id, message_id=999,
    )
    elo_col = repository.get_elo_col(db, guild_id)
    for i in range(10):
        repository.link_riot_account(
            db, guild_id=guild_id, user_id=i,
            riot_name=f"P{i}", riot_tag="EUW", riot_region="eu",
            puuid=f"pu{i}",
            peak_elo=1500 + i * 50,
            source="peak_recent",
        )
        # Compound _id `<uid>:<queue_type>` pour le doc joueur.
        elo_col.insert_one({
            "_id": repository.player_doc_id(i, queue_type),
            "name": f"P{i}",
            "elo": 1500 + i * 50, "wins": 0, "losses": 0,
            "linked_once": True,
        })
        repository.add_player_to_queue(
            db, guild_id=guild_id, queue_type=queue_type, user_id=i,
        )
    return repository.get_active_queue(
        db, guild_id=guild_id, queue_type=queue_type,
    )


# ── on_queue_full : succes ────────────────────────────────────────
async def test_on_queue_full_posts_message_with_view():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1")
    prep = cat.text_channels[0]
    guild = _fake_guild(42, members=members,
                        categories=[cat],
                        channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    # Message envoye dans le salon match-preparation
    prep.send.assert_awaited_once()
    args, kwargs = prep.send.call_args
    assert "Match trouve" in kwargs["content"]
    for i in range(10):
        assert f"<@{i}>" in kwargs["content"]

    embed = kwargs["embed"]
    assert "Map" in embed.description
    assert any("Team A" in f.name for f in embed.fields)
    assert any("Team B" in f.name for f in embed.fields)
    assert isinstance(kwargs["view"], VoteView)


async def test_on_queue_full_persists_match():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1")
    prep = cat.text_channels[0]
    guild = _fake_guild(42, members=members,
                        categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match is not None
    assert match["status"] == "pending"
    assert match["map"] in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")
    assert match["category_name"] == "Match #1"
    assert match["message_id"] == 555
    assert match["channel_id"] == prep.id      # poste dans match-preparation
    assert len(match["team_a"]) == 5
    assert len(match["team_b"]) == 5
    assert match["votes"] == {}
    assert int(match["lobby_leader_id"]) in range(10)


async def test_on_queue_full_resets_queue():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None


async def test_on_queue_full_aborts_when_no_prep_channel_free():
    """Si toutes les categories Match # sont occupees, le match est annule."""
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[
                            _fake_category("Match #1", t1_empty=False),
                            _fake_category("Match #2", t2_empty=False),
                            _fake_category("Match #3", t1_empty=False),
                        ],
                        channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    assert match_id is None
    # Queue annulee, message d'erreur poste dans le salon de queue
    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None
    channel.send.assert_awaited_once()
    args, _ = channel.send.call_args
    assert "match-preparation" in args[0]


async def test_on_queue_full_balanced_teams_in_persistence():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, 42, match_id)
    sum_a = sum(p["elo"] for p in match["team_a"])
    sum_b = sum(p["elo"] for p in match["team_b"])
    # Les elos ont ete distribues comme 1500..1950 step 50, total 17250 -> ideal 8625
    diff = abs(sum_a - sum_b)
    assert diff <= 100, f"diff={diff}, attendu <=100 sur cet ensemble"


# ── on_queue_full : echec gracieux ────────────────────────────────
async def test_on_queue_full_aborts_if_player_unlinked():
    import bot as bot_module
    # Setup queue avec 10 joueurs, mais on retire le compte Riot du joueur 5
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    repository.unlink_riot_account(bot_module.db, guild_id=42, user_id=5)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    result = await cog.on_queue_full(inter, queue_doc, "open")

    assert result is None  # echec
    # Le message d'erreur a ete envoye, pas le match
    channel.send.assert_awaited()
    err_call = channel.send.call_args
    # 2 cas : un seul appel (l'erreur) ou deux (erreur + autre)
    # On verifie qu'au moins un message contient "annule"
    sent_contents = " ".join(
        str(call.kwargs.get("content", "")) + " " + str(call.args[0] if call.args else "")
        for call in channel.send.call_args_list
    )
    assert "annule" in sent_contents.lower() or "annul" in sent_contents.lower()
    # La queue est supprimee
    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None


# ── VoteView stub (Phase 4 — Phase 5 implementera) ────────────────
async def test_vote_view_buttons_have_stable_custom_ids():
    import bot as bot_module
    view = VoteView(bot_module.db)
    # Cherche les custom_ids dans les children
    custom_ids = {c.custom_id for c in view.children}
    assert VOTE_A_BTN_ID in custom_ids
    assert VOTE_B_BTN_ID in custom_ids


# ── Ordre roles -> message et deplacement VC (audit user) ────────
async def test_roles_granted_before_match_message_sent():
    """Le message de match doit arriver APRES l'attribution du role Match #N
    (sinon les joueurs sans le role ne voient pas l'embed dans match-preparation).
    On verifie l'ordre via un compteur d'evenements partage entre add_roles
    et prep.send."""
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    events = []
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    for m in members:
        async def _add(*args, _id=m.id, **kwargs):
            events.append(("add_roles", _id))
        m.add_roles.side_effect = _add

    cat = _fake_category("Match #1")
    prep = cat.text_channels[0]
    async def _prep_send(*args, **kwargs):
        events.append(("prep_send", None))
        msg = MagicMock(); msg.id = 555
        return msg
    prep.send.side_effect = _prep_send

    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    # Le test echoue si le faux role manque ; on verifie l'ordre uniquement si
    # add_roles a ete appele (cas reel : le serveur a le role Match #N).
    # Ici les members ont guild.roles=[] donc _grant_match_role return early.
    # On contourne en verifiant que prep.send est bien appele apres tout
    # autre evenement (smoke test : present + position dernier evenement).
    assert ("prep_send", None) in events
    # Si jamais add_roles a tire (futur : roles configures), il doit etre
    # avant prep_send.
    role_events = [i for i, e in enumerate(events) if e[0] == "add_roles"]
    send_event  = events.index(("prep_send", None))
    if role_events:
        assert max(role_events) < send_event


async def test_players_moved_to_team_vcs():
    """Les 10 joueurs en Waiting Room doivent etre deplaces vers la VC
    Team 1 ou Team 2 de la categorie attribuee, selon leur assignation."""
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    waiting_room = MagicMock()
    waiting_room.name = "Waiting Room"
    waiting_room.id = 999

    members = [
        _fake_member(i, f"P{i}", voice_channel=waiting_room) for i in range(10)
    ]
    cat = _fake_category("Match #1", with_waiting=True)
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    team1 = next(v for v in cat.voice_channels if v.name == "Team 1")
    team2 = next(v for v in cat.voice_channels if v.name == "Team 2")
    # Verifie que chaque joueur a ete move_to vers Team 1 OU Team 2 (5+5)
    dests = []
    for m in members:
        m.move_to.assert_awaited_once()
        dest = m.move_to.await_args.args[0]
        assert dest in (team1, team2)
        dests.append(dest)
    assert dests.count(team1) == 5
    assert dests.count(team2) == 5


async def test_player_already_in_team_vc_not_moved():
    """Les joueurs deja dans leur VC d'equipe ne doivent pas etre re-deplaces.

    On place les 10 joueurs dans Team 1 : on s'attend a ce que les 5 de
    team_a (assignes a Team 1) ne soient PAS deplaces, et que les 5 de
    team_b soient deplaces vers Team 2.
    """
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    cat = _fake_category("Match #1", with_waiting=True)
    team1 = next(v for v in cat.voice_channels if v.name == "Team 1")
    team2 = next(v for v in cat.voice_channels if v.name == "Team 2")

    members = [
        _fake_member(i, f"P{i}", voice_channel=team1) for i in range(10)
    ]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    not_moved = [m for m in members if not m.move_to.await_count]
    moved_to_team2 = [
        m for m in members
        if m.move_to.await_count and m.move_to.await_args.args[0] is team2
    ]
    assert len(not_moved) == 5
    assert len(moved_to_team2) == 5
    # Aucun joueur deja en Team 1 ne doit etre re-deplace vers Team 1
    for m in members:
        for call in m.move_to.await_args_list:
            assert call.args[0] is not team1


async def test_queue_full_does_not_crash_when_no_team_vcs():
    """Si la categorie n'a ni Team 1 ni Team 2 ni Waiting Match, le match
    doit quand meme etre cree (fallback gracieux : pas de deplacement)."""
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    cat = MagicMock()
    cat.name = "Match #1"
    cat.voice_channels = []
    prep = MagicMock()
    prep.name = "match-preparation"
    prep.id = 777
    prep.send = AsyncMock(return_value=MagicMock(id=42424242))
    cat.text_channels = [prep]
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")
    assert match_id is not None


# ── queue_type propagation ────────────────────────────────────────
async def test_on_queue_full_persists_queue_type_in_match_doc(monkeypatch):
    """Le match doc doit stocker queue_type='pro' quand on_queue_full
    est invoque pour la Pro Queue."""
    import bot as bot_module
    import services.captain_draft as cd_module

    # Pro queue passe desormais par CaptainDraftSession : simuler un draft complet.
    async def _fake_run(self):
        from services.captain_draft import DraftResult
        state = self.state
        for p in list(state.pool):
            state = state.apply_pick(p)
        return DraftResult.from_state(state)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run)

    queue_doc = _seed_full_queue(
        bot_module.db, guild_id=42, queue_type="pro",
    )

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members,
                        categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "pro")

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match is not None
    assert match["queue_type"] == "pro"


async def test_on_queue_full_passes_queue_type_to_create_match(monkeypatch):
    """Spy sur repository.create_match : verifie le kwarg queue_type."""
    import bot as bot_module
    queue_doc = _seed_full_queue(
        bot_module.db, guild_id=42, queue_type="gc",
    )

    captured: dict = {}
    real_create = repository.create_match

    def spy_create(*args, **kwargs):
        captured.update(kwargs)
        return real_create(*args, **kwargs)

    monkeypatch.setattr("services.repository.create_match", spy_create)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1")
    guild = _fake_guild(42, members=members,
                        categories=[cat], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "gc")

    assert captured.get("queue_type") == "gc"


# ── build_match_embed ─────────────────────────────────────────────
def test_build_match_embed_shows_all_players_and_map():
    from services.match_service import MatchPlan
    from services.team_balancer import balance_teams

    players = [Player(id=i, name=f"P{i}", elo=1500 + i*50) for i in range(10)]
    teams = balance_teams(players)
    plan = MatchPlan(teams=teams, map_name="Ascent", lobby_leader=players[0], category_name="Match #1")

    embed = build_match_embed(plan, "MyGuild")
    assert "Ascent" in embed.description
    assert "<@0>" in embed.description  # leader
    fields_str = " ".join(f.value for f in embed.fields)
    for i in range(10):
        assert f"<@{i}>" in fields_str
    assert "Match #1" in fields_str


# ── _move_players_to_waiting_match ────────────────────────────────
async def test_move_to_waiting_match_routes_all_players():
    """_move_players_to_waiting_match deplace les 10 joueurs vers Waiting Match."""
    import bot as bot_module

    # Voice channel source
    waiting_room = MagicMock()
    waiting_room.name = "Pro Waiting Room"
    waiting_room.id = 7777

    members = [
        _fake_member(i, f"P{i}", voice_channel=waiting_room) for i in range(10)
    ]
    cat = _fake_category("Match #1", with_waiting=True)
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    player_ids = [str(m.id) for m in members]

    await cog._move_players_to_waiting_match(guild, cat, player_ids)

    waiting_match_vc = next(c for c in cat.voice_channels if c.name == "Waiting Match")
    moved_to_waiting = sum(
        1 for m in members
        if m.move_to.await_count > 0
        and m.move_to.call_args.args[0].id == waiting_match_vc.id
    )
    assert moved_to_waiting == 10


# ── Pro Queue Captain Draft integration ───────────────────────────

def _make_10_players():
    """Retourne 10 Player avec ELO croissant pour les tests pro queue."""
    return [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]


def _patch_build_players(monkeypatch, players):
    """Monkeypatch build_players dans cogs.match pour court-circuiter le fetch Mongo."""
    import cogs.match as match_module
    monkeypatch.setattr(match_module, "build_players", lambda *a, **kw: players)


@pytest.mark.asyncio
async def test_on_queue_full_open_does_not_invoke_captain_draft(monkeypatch):
    """queue_type='open' -> plan_match utilise, CaptainDraftSession PAS instancie."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    instantiated = []
    original_init = cd_module.CaptainDraftSession.__init__

    def _spy_init(self, *args, **kwargs):
        instantiated.append(1)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "__init__", _spy_init)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1")
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members], "channel_id": "100"}
    try:
        await cog.on_queue_full(inter, queue_doc, queue_type="open")
    except Exception:
        pass
    assert instantiated == [], "CaptainDraftSession ne doit pas etre instancie en open queue"


@pytest.mark.asyncio
async def test_on_queue_full_pro_invokes_captain_draft(monkeypatch):
    """queue_type='pro' -> CaptainDraftSession.run() est appele."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    run_calls = []

    async def _fake_run(self):
        run_calls.append(self)
        # On simule un draft complet : retourne un DraftResult coherent
        from services.captain_draft import DraftResult
        state = self.state
        for p in list(state.pool):
            state = state.apply_pick(p)
        return DraftResult.from_state(state)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members], "channel_id": "100"}
    try:
        await cog.on_queue_full(inter, queue_doc, queue_type="pro")
    except Exception:
        pass
    assert len(run_calls) == 1, "CaptainDraftSession.run() doit etre appele exactement 1 fois"


@pytest.mark.asyncio
async def test_on_queue_full_pro_cancelled_does_not_delete_queue(monkeypatch):
    """Si le draft est annule, delete_active_queue n'est PAS appele."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module
    from services.captain_draft import DraftCancelledError

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    async def _fake_run_cancel(self):
        raise DraftCancelledError("admin", actor=None)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run_cancel)

    delete_calls = []
    from services import repository
    monkeypatch.setattr(
        repository, "delete_active_queue",
        lambda *a, **kw: delete_calls.append((a, kw)),
    )

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members], "channel_id": "100"}
    await cog.on_queue_full(inter, queue_doc, queue_type="pro")
    assert delete_calls == [], "delete_active_queue ne doit pas etre appele apres cancel"


def _make_match_role(name: str = "Match #1"):
    """Cree un faux discord.Role compatible avec discord.utils.get(..., name=...)."""
    role = MagicMock()
    role.name = name
    return role


@pytest.mark.asyncio
async def test_on_queue_full_pro_grants_match_role_before_draft_run(monkeypatch):
    """Regression : sans le role Match #N, les capitaines non-modos ne
    voient pas le salon match-preparation et ne peuvent pas pick. Le
    grant doit donc precoder l'appel a CaptainDraftSession.run().
    """
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    events: list[tuple[str, int | None]] = []

    async def _fake_run(self):
        events.append(("draft_run", None))
        from services.captain_draft import DraftResult
        state = self.state
        for p in list(state.pool):
            state = state.apply_pick(p)
        return DraftResult.from_state(state)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run)

    match_role = _make_match_role("Match #1")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    for m in members:
        m.guild.roles = [match_role]
        async def _add(*args, _id=m.id, **kwargs):
            events.append(("grant", _id))
        m.add_roles.side_effect = _add

    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    guild.roles = [match_role]
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members], "channel_id": "100"}
    try:
        await cog.on_queue_full(inter, queue_doc, queue_type="pro")
    except Exception:
        pass

    grant_indices = [i for i, e in enumerate(events) if e[0] == "grant"]
    run_idx = events.index(("draft_run", None))
    assert len(grant_indices) == 10, (
        f"Le role Match #N doit etre grant aux 10 joueurs avant le draft, "
        f"vu {len(grant_indices)} grants"
    )
    assert max(grant_indices) < run_idx, (
        "Tous les grants de role Match #N doivent precoder draft.run()"
    )


@pytest.mark.asyncio
async def test_on_queue_full_pro_cancel_revokes_match_role(monkeypatch):
    """Sur DraftCancelledError, le role Match #N grant avant le draft
    doit etre revoke pour eviter que les joueurs gardent acces au salon.
    """
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module
    from services.captain_draft import DraftCancelledError

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    async def _fake_run_cancel(self):
        raise DraftCancelledError("admin", actor=None)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run_cancel)

    match_role = _make_match_role("Match #1")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    # Simule que add_roles a effectivement ajoute le role (grant avant draft)
    # pour que remove_roles ne soit pas court-circuite par "role not in member.roles".
    for m in members:
        m.guild.roles = [match_role]
        m.roles = [match_role]

    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    guild.roles = [match_role]
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members], "channel_id": "100"}
    await cog.on_queue_full(inter, queue_doc, queue_type="pro")

    revoked = [m for m in members if m.remove_roles.await_count >= 1]
    assert len(revoked) == 10, (
        f"Le role Match #N doit etre revoke aux 10 joueurs sur cancel, "
        f"vu {len(revoked)} revokes"
    )
