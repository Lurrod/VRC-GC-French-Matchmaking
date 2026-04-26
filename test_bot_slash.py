"""
Tests d'integration des SLASH commands et boutons (LeaderboardView).

dpytest ne supporte pas pleinement les slash commands ni les composants.
On utilise donc des mocks Discord directs : on instancie une fausse
discord.Interaction, on appelle le callback de la commande, et on verifie
les appels aux methodes Discord.

Pour lancer :
    pytest test_bot_slash.py -v
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock
import pytest


def _fake_member(member_id: int, name: str, *, manage_guild: bool = False):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.guild_permissions.manage_guild = manage_guild
    m.roles = []
    avatar = MagicMock()
    avatar.url = f"https://cdn.discordapp.com/embed/avatars/{member_id % 6}.png"
    avatar.replace.return_value = avatar
    m.display_avatar = avatar
    return m


def _fake_guild(guild_id: int, name: str = "TestGuild", members=None):
    g = MagicMock()
    g.id = guild_id
    g.name = name
    g.members = members or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == mid), None)
    return g


def _fake_interaction(user, guild):
    inter = MagicMock()
    inter.user = user
    inter.guild = guild
    inter.guild_id = guild.id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.followup.edit_message = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.message = MagicMock()
    inter.message.id = 999
    return inter


# ── /stats ────────────────────────────────────────────────────────
async def test_slash_stats_unknown_player():
    import bot as bot_module

    user = _fake_member(1, "Alice")
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    await bot_module.stats.callback(inter, joueur=user)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "n'a pas encore joue" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_slash_stats_known_player():
    import bot as bot_module

    user = _fake_member(1, "Alice")
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    col = bot_module.get_elo_col(42)
    col.insert_one({
        "_id": "1", "name": "Alice", "elo": 200, "wins": 8, "losses": 2,
    })

    await bot_module.stats.callback(inter, joueur=user)

    inter.response.send_message.assert_awaited_once()
    embed = inter.response.send_message.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert "200" in fields["🏅 ELO"]
    assert "80" in fields["📈 Winrate"]  # 80%


# ── /win ──────────────────────────────────────────────────────────
async def test_slash_win_no_permission():
    import bot as bot_module

    user = _fake_member(1, "Alice", manage_guild=False)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[user, target])
    inter = _fake_interaction(user, guild)

    await bot_module.win.callback(inter, joueur1=target)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "Pas la permission" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_slash_win_5_players_distributes_elo_v2():
    """V2 : tous les gagnants prennent le meme gain, base sur l'avg ELO."""
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    targets = [_fake_member(10 + i, f"P{i}") for i in range(5)]
    guild = _fake_guild(42, members=[admin] + targets)
    inter = _fake_interaction(admin, guild)

    await bot_module.win.callback(
        inter,
        joueur1=targets[0], joueur2=targets[1], joueur3=targets[2],
        joueur4=targets[3], joueur5=targets[4],
    )

    # Sans Riot link : fallback ELO_REFERENCE=1500 -> gain=20 pour chacun
    col = bot_module.get_elo_col(42)
    for t in targets:
        doc = col.find_one({"_id": str(t.id)})
        assert doc["elo"] == 20, f"{t.display_name}: attendu 20, recu {doc['elo']}"
        assert doc["wins"] == 1


async def test_slash_win_uses_server_avg_when_seeded():
    """Le gain est proportionnel a l'avg de l'ELO serveur (elo_<guild>.elo)."""
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    targets = [_fake_member(20 + i, f"R{i}") for i in range(2)]
    guild = _fake_guild(42, members=[admin] + targets)
    inter = _fake_interaction(admin, guild)

    # Seed une ELO serveur de 3000 (Radiant) -> gain = 25
    col = bot_module.get_elo_col(42)
    for t in targets:
        col.insert_one({
            "_id": str(t.id), "name": t.display_name,
            "elo": 3000, "wins": 0, "losses": 0, "linked_once": True,
        })

    await bot_module.win.callback(inter, joueur1=targets[0], joueur2=targets[1])

    for t in targets:
        doc = col.find_one({"_id": str(t.id)})
        assert doc["elo"] == 3025, f"{t.display_name}: attendu 3025 (3000 + 25), recu {doc['elo']}"


# ── /lose ─────────────────────────────────────────────────────────
async def test_slash_lose_floors_at_zero():
    import bot as bot_module

    admin   = _fake_member(1, "Admin", manage_guild=True)
    target  = _fake_member(2, "Bob")
    partner = _fake_member(3, "Boost")  # tire l'avg vers le haut
    guild = _fake_guild(42, members=[admin, target, partner])
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    col.insert_one({"_id": "2", "name": "Bob",   "elo": 5,    "wins": 0, "losses": 0})
    col.insert_one({"_id": "3", "name": "Boost", "elo": 2995, "wins": 0, "losses": 0})

    # avg(5, 2995) = 1500 -> loss = round(10 * 1500/2400) = 6. Bob: max(0, 5 - 6) = 0
    await bot_module.lose.callback(inter, joueur1=target, joueur2=partner)

    assert col.find_one({"_id": "2"})["elo"] == 0
    assert col.find_one({"_id": "3"})["elo"] == 2989


# ── /leaderboard + LeaderboardView (le bug initial) ───────────────
async def test_slash_leaderboard_creates_view_with_pagination():
    """
    Cas-cle : 30 joueurs -> 2 pages.
    Verifie que la commande envoie bien un fichier ET une view avec 3 boutons.
    """
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    members = [_fake_member(100 + i, f"User{i}") for i in range(30)]
    guild = _fake_guild(42, members=[admin] + members)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    for i, m in enumerate(members):
        col.insert_one({
            "_id": str(m.id), "name": m.display_name,
            "elo": 100 + i, "wins": i, "losses": 0,
        })

    await bot_module.leaderboard.callback(inter)

    # La 1ere page passe par interaction.followup.send (apres defer)
    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()

    kwargs = inter.followup.send.call_args.kwargs
    assert "file" in kwargs, "Aucun fichier envoye"
    assert "view" in kwargs, "Aucune view envoyee"

    view = kwargs["view"]
    assert view.page == 0
    # 3 boutons : prev, page_btn (label), next
    assert len(view.children) == 3
    # prev disabled sur page 0, next active
    assert view.children[0].disabled is True
    assert view.children[2].disabled is False


async def test_slash_leaderboard_next_button_navigates_to_page_2():
    """
    LE TEST DU BUG : on simule un clic sur le bouton next et on verifie
    que la page change et qu'un nouveau fichier est envoye.
    """
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    members = [_fake_member(100 + i, f"User{i}") for i in range(30)]
    guild = _fake_guild(42, members=[admin] + members)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    for i, m in enumerate(members):
        col.insert_one({
            "_id": str(m.id), "name": m.display_name,
            "elo": 100 + i, "wins": 0, "losses": 0,
        })

    # 1) Lance la commande pour obtenir la view
    await bot_module.leaderboard.callback(inter)
    view = inter.followup.send.call_args.kwargs["view"]
    assert view.page == 0

    # 2) Simule un clic sur "next" : on appelle directement le helper _go
    btn_inter = _fake_interaction(admin, guild)
    await view._go(btn_inter, view.page + 1)

    # 3) Verifie : page = 1, defer + edit appeles
    assert view.page == 1, f"La page n'a pas change : {view.page}"
    btn_inter.response.defer.assert_awaited_once()
    btn_inter.followup.edit_message.assert_awaited_once()

    edit_kwargs = btn_inter.followup.edit_message.call_args.kwargs
    assert edit_kwargs["message_id"] == btn_inter.message.id
    assert "attachments" in edit_kwargs and len(edit_kwargs["attachments"]) == 1
    assert "view" in edit_kwargs

    # 4) Boutons mis a jour : sur page 1 (=derniere), next desactive, prev actif
    assert view.children[0].disabled is False  # prev
    assert view.children[2].disabled is True   # next


async def test_slash_leaderboard_clicking_next_past_last_page_is_noop():
    """Garde de bornes : aller au-dela de la derniere page ne casse rien."""
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    # 16 joueurs -> 2 pages (page 0 et page 1)
    members = [_fake_member(100 + i, f"User{i}") for i in range(16)]
    guild = _fake_guild(42, members=[admin] + members)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    for i, m in enumerate(members):
        col.insert_one({
            "_id": str(m.id), "name": m.display_name,
            "elo": 100 + i, "wins": 0, "losses": 0,
        })

    await bot_module.leaderboard.callback(inter)
    view = inter.followup.send.call_args.kwargs["view"]

    # Aller en page 1 (derniere)
    btn_inter = _fake_interaction(admin, guild)
    await view._go(btn_inter, 1)
    assert view.page == 1

    # Tenter d'aller en page 2 (out of bounds) : la page ne change pas
    btn_inter2 = _fake_interaction(admin, guild)
    await view._go(btn_inter2, 2)
    assert view.page == 1, "La page ne doit PAS changer quand on depasse total_pages"
    btn_inter2.followup.edit_message.assert_not_awaited()


# ── /elomodify ────────────────────────────────────────────────────
async def test_slash_elomodify_add():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[admin, target])
    inter = _fake_interaction(admin, guild)

    await bot_module.elomodify.callback(inter, joueur=target, action="add", montant=50)

    col = bot_module.get_elo_col(42)
    doc = col.find_one({"_id": "2"})
    assert doc["elo"] == 50


async def test_slash_elomodify_remove_floors_at_zero():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[admin, target])
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    col.insert_one({"_id": "2", "name": "Bob", "elo": 30, "wins": 0, "losses": 0})

    await bot_module.elomodify.callback(inter, joueur=target, action="remove", montant=100)

    doc = col.find_one({"_id": "2"})
    assert doc["elo"] == 0  # max(0, 30 - 100)


# ── /resetelo ─────────────────────────────────────────────────────
async def test_slash_resetelo_single_player():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[admin, target])
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    col.insert_one({"_id": "2", "name": "Bob", "elo": 999, "wins": 50, "losses": 5})

    await bot_module.resetelo.callback(inter, joueur=target, all=False)

    doc = col.find_one({"_id": "2"})
    assert doc["elo"] == 0
    assert doc["wins"] == 0
    assert doc["losses"] == 0


async def test_slash_resetelo_all_players():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    targets = [_fake_member(10 + i, f"P{i}") for i in range(5)]
    guild = _fake_guild(42, members=[admin] + targets)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col(42)
    for t in targets:
        col.insert_one({"_id": str(t.id), "name": t.display_name, "elo": 100, "wins": 5, "losses": 1})

    await bot_module.resetelo.callback(inter, joueur=None, all=True)

    for t in targets:
        doc = col.find_one({"_id": str(t.id)})
        assert doc["elo"] == 0
        assert doc["wins"] == 0


# ── /map ──────────────────────────────────────────────────────────
async def test_slash_map_returns_known_map():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild(42, members=[admin])
    inter = _fake_interaction(admin, guild)

    await bot_module.map_pick.callback(inter)

    inter.response.send_message.assert_awaited_once()
    embed = inter.response.send_message.call_args.kwargs["embed"]
    # Le titre contient le nom de la map
    assert any(m in embed.description for m in bot_module.MAPS), \
        f"Map non reconnue : {embed.description}"


# ── has_access ────────────────────────────────────────────────────
def test_has_access_admin_returns_true():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild(42, members=[admin])
    inter = _fake_interaction(admin, guild)

    assert bot_module.has_access(inter) is True


def test_has_access_non_admin_no_bypass_returns_false():
    import bot as bot_module

    user = _fake_member(1, "User", manage_guild=False)
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    assert bot_module.has_access(inter) is False


# ── /setup ────────────────────────────────────────────────────────
def _fake_guild_with_setup(guild_id: int = 42):
    g = _fake_guild(guild_id)
    g.categories = []
    g.text_channels = []

    async def _create_category(name):
        cat = MagicMock()
        cat.name = name
        g.categories.append(cat)
        return cat

    async def _create_text_channel(name, category=None):
        chan = MagicMock()
        chan.name = name
        chan.id = 100 + len(g.text_channels)
        chan.category = category
        chan.mention = f"#{name}"
        chan.send = AsyncMock(return_value=MagicMock(id=999))
        g.text_channels.append(chan)
        return chan

    g.create_category = AsyncMock(side_effect=_create_category)
    g.create_text_channel = AsyncMock(side_effect=_create_text_channel)
    return g


async def test_slash_setup_creates_category_and_channels():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild_with_setup(42)
    inter = _fake_interaction(admin, guild)

    # bot.get_cog renvoie None ici (pas de cog charge en test)
    bot_module.bot.get_cog = MagicMock(return_value=None)

    await bot_module.setup_bot.callback(inter)

    # Categorie creee
    assert any(c.name == bot_module.SETUP_CATEGORY_NAME for c in guild.categories)
    # Tous les salons crees
    names = [c.name for c in guild.text_channels]
    for expected in bot_module.SETUP_CHANNELS:
        assert expected in names

    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "Créés" in msg
    assert inter.followup.send.call_args.kwargs.get("ephemeral") is True


async def test_slash_setup_idempotent_when_channels_exist():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild_with_setup(42)

    # Pre-cree categorie + salons
    cat = MagicMock()
    cat.name = bot_module.SETUP_CATEGORY_NAME
    guild.categories.append(cat)
    for n in bot_module.SETUP_CHANNELS:
        chan = MagicMock()
        chan.name = n
        chan.id = 555
        chan.send = AsyncMock(return_value=MagicMock(id=999))
        chan.mention = f"#{n}"
        guild.text_channels.append(chan)

    inter = _fake_interaction(admin, guild)
    bot_module.bot.get_cog = MagicMock(return_value=None)

    await bot_module.setup_bot.callback(inter)

    # Aucune creation
    guild.create_category.assert_not_awaited()
    guild.create_text_channel.assert_not_awaited()

    msg = inter.followup.send.call_args.args[0]
    assert "Déjà présents" in msg


def test_has_access_non_admin_with_bypass_role_returns_true():
    import bot as bot_module

    role = MagicMock()
    role.id = 555
    user = _fake_member(1, "User", manage_guild=False)
    user.roles = [role]
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    bot_module.set_bypass_role(42, 555)
    assert bot_module.has_access(inter) is True
