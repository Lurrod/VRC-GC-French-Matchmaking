"""Tests de la mise a jour ELO post-vote (Phase 6)."""

import pytest

from services import elo_calc, repository
from services.elo_updater import (
    apply_match_validation,
    MatchEloOutcome,
    PlayerEloChange,
)


# ── compute_match_elo_change (formule pure, zero-sum) ─────────────
@pytest.mark.parametrize("avg", [0, 300, 2100, 2400, 2700, 3000, 5000])
def test_compute_match_elo_change(avg):
    """Zero-sum strict : gain == loss == ELO_BASE_CHANGE quelle que soit l'avg."""
    g, l = elo_calc.compute_match_elo_change(avg)
    assert g == elo_calc.ELO_BASE_CHANGE
    assert l == elo_calc.ELO_BASE_CHANGE
    assert g == l


def test_compute_match_elo_change_rejects_negative():
    with pytest.raises(ValueError):
        elo_calc.compute_match_elo_change(-100)


# ── compute_team_avg_elo ──────────────────────────────────────────
def test_team_avg_empty_returns_0():
    assert elo_calc.compute_team_avg_elo([]) == 0


def test_team_avg_normal():
    players = [{"elo": 1000}, {"elo": 2000}, {"elo": 1500}]
    assert elo_calc.compute_team_avg_elo(players) == 1500


def test_team_avg_handles_missing_elo_key():
    players = [{"elo": 1500}, {"name": "no-elo"}]  # 2eme sans elo -> 0
    assert elo_calc.compute_team_avg_elo(players) == 750


# ── apply_match_validation ────────────────────────────────────────
def _make_match(status="validated_a", elo=2400):
    return {
        "team_a": [{"id": i, "name": f"A{i}", "elo": elo} for i in range(5)],
        "team_b": [{"id": 5 + i, "name": f"B{i}", "elo": elo} for i in range(5)],
        "status": status,
    }


def test_invalid_status_raises():
    import bot as bot_module
    with pytest.raises(ValueError):
        apply_match_validation(bot_module.db, 42, _make_match(status="pending"))


def test_validated_a_winners_get_gain():
    import bot as bot_module
    match = _make_match(status="validated_a", elo=2400)
    outcome = apply_match_validation(bot_module.db, 42, match)

    # Sans multipliers (Henrik introuvable) -> fallback flat 16.
    assert outcome.gain == 16
    assert outcome.loss == 16
    assert outcome.avg_elo == 2400

    # Toutes les changes : team_a (0..4) gagne, team_b (5..9) perd
    winners = [c for c in outcome.changes if c.win]
    losers  = [c for c in outcome.changes if not c.win]
    assert len(winners) == 5
    assert len(losers)  == 5
    assert {c.user_id for c in winners} == {"0", "1", "2", "3", "4"}
    assert {c.user_id for c in losers}  == {"5", "6", "7", "8", "9"}


def test_validated_b_swaps_winners_losers():
    import bot as bot_module
    match = _make_match(status="validated_b", elo=2400)
    outcome = apply_match_validation(bot_module.db, 42, match)

    winners_ids = {c.user_id for c in outcome.changes if c.win}
    assert winners_ids == {"5", "6", "7", "8", "9"}


def test_winners_get_plus_gain_in_db():
    import bot as bot_module
    # Seed pre-prod : en prod chaque joueur a au moins LINK_BASE_ELO=2000
    # via /link-riot. Sans seed, la nouvelle distribution zero-sum
    # constate que les perdants (a 0 ELO) ne peuvent rien perdre, et
    # neutralise les gains gagnants pour rester zero-sum.
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=2400)  # avg=2400, sans multipliers -> flat 16
    apply_match_validation(bot_module.db, 42, match)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    for i in range(5):
        doc = elo_col.find_one({"_id": str(i)})
        assert doc["elo"] == 2016  # 2000 + 16 (flat fallback)
        assert doc["wins"] == 1
        assert doc["losses"] == 0


def test_losers_get_minus_loss_in_db():
    import bot as bot_module
    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, 42, match)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    for i in range(5, 10):
        doc = elo_col.find_one({"_id": str(i)})
        # New player : ELO_START=0, max(0, 0-15) = 0
        assert doc["elo"] == 0
        assert doc["losses"] == 1
        assert doc["wins"] == 0


def test_loser_existing_elo_decreases_correctly():
    import bot as bot_module
    elo_col = repository.get_elo_col(bot_module.db, 42)
    elo_col.insert_one({"_id": "5", "name": "B0", "elo": 50, "wins": 0, "losses": 0})

    match = _make_match(elo=2400)  # sans multipliers -> flat loss=16
    apply_match_validation(bot_module.db, 42, match)

    doc = elo_col.find_one({"_id": "5"})
    assert doc["elo"] == 34
    assert doc["losses"] == 1


def test_loser_floored_at_zero():
    import bot as bot_module
    elo_col = repository.get_elo_col(bot_module.db, 42)
    elo_col.insert_one({"_id": "5", "name": "B0", "elo": 5, "wins": 0, "losses": 0})

    match = _make_match(elo=2400)  # loss=15 mais courrant=5 -> 0
    apply_match_validation(bot_module.db, 42, match)

    doc = elo_col.find_one({"_id": "5"})
    assert doc["elo"] == 0


def test_base_is_constant_with_multipliers_high_avg():
    import bot as bot_module
    # Base ELO_BASE_CHANGE=16 quelle que soit l'avg, meme avec multipliers ACS.
    # Le scaling individuel est porte par les multiplicateurs, pas par l'avg.
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=3000)
    multipliers = {str(i): 1.0 for i in range(10)}
    outcome = apply_match_validation(bot_module.db, 42, match, multipliers=multipliers)
    assert outcome.gain == 16
    assert outcome.loss == 16

    elo_col = repository.get_elo_col(bot_module.db, 42)
    assert elo_col.find_one({"_id": "0"})["elo"] == 2016  # 2000 + 16


def test_base_is_constant_with_multipliers_low_avg():
    import bot as bot_module
    # Avec multipliers et avg basse (300) : la base reste 16, pas de scaling.
    match = _make_match(elo=300)
    multipliers = {str(i): 1.0 for i in range(10)}
    outcome = apply_match_validation(bot_module.db, 42, match, multipliers=multipliers)
    assert outcome.gain == 16
    assert outcome.loss == 16


def test_no_multipliers_uses_flat_fallback_regardless_of_avg():
    """Sans multipliers, le base change est toujours 16 (flat), peu importe
    l'avg ELO du match. Verifie le nouveau comportement du fallback."""
    import bot as bot_module
    for avg in (300, 2400, 3000):
        match = _make_match(elo=avg)
        outcome = apply_match_validation(bot_module.db, 42, match)
        assert outcome.gain == 16, f"avg={avg}, gain={outcome.gain}"
        assert outcome.loss == 16, f"avg={avg}, loss={outcome.loss}"


def test_existing_winner_keeps_history_and_adds_gain():
    import bot as bot_module
    elo_col = repository.get_elo_col(bot_module.db, 42)
    elo_col.insert_one({"_id": "0", "name": "A0", "elo": 200, "wins": 5, "losses": 3})
    # Seed les autres joueurs avec assez d'ELO pour que les perdants
    # puissent perdre 15 sans hitter le plancher zero-sum.
    for i in range(1, 5):
        elo_col.insert_one({"_id": str(i), "name": f"A{i}", "elo": 2000, "wins": 0, "losses": 0})
    for i in range(5, 10):
        elo_col.insert_one({"_id": str(i), "name": f"B{i-5}", "elo": 2000, "wins": 0, "losses": 0})

    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, 42, match)

    doc = elo_col.find_one({"_id": "0"})
    assert doc["elo"] == 216       # 200 + 16 (flat fallback sans multipliers)
    assert doc["wins"] == 6        # 5 + 1
    assert doc["losses"] == 3      # inchange


def test_mixed_team_avg_elo():
    """Verifie que la moyenne est bien calculee sur les 10 joueurs."""
    import bot as bot_module
    match = {
        "team_a": [{"id": i, "name": f"A{i}", "elo": 2200} for i in range(5)],     # avg 2200
        "team_b": [{"id": 5 + i, "name": f"B{i}", "elo": 2600} for i in range(5)], # avg 2600
        "status": "validated_a",
    }
    outcome = apply_match_validation(bot_module.db, 42, match)
    # Avg total = (5*2200 + 5*2600) / 10 = 2400. Sans multipliers ->
    # flat fallback 16, mais avg_elo reste informatif dans l'embed.
    assert outcome.avg_elo == 2400
    assert outcome.gain == 16


def test_change_dataclass_fields():
    import bot as bot_module
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=2400)
    outcome = apply_match_validation(bot_module.db, 42, match)

    winner = next(c for c in outcome.changes if c.win)
    assert winner.delta == 16  # flat fallback sans multipliers
    assert winner.old_elo == 2000
    assert winner.new_elo == 2016

    loser = next(c for c in outcome.changes if not c.win)
    assert loser.delta == -16
    assert loser.old_elo == 2000
    assert loser.new_elo == 1984


# ── Zero-sum garanti avec multiplicateurs (fix audit #1) ──────────
def _seed_baseline_elo(db, guild_id: int, ids: range, baseline: int) -> None:
    """Donne a chaque joueur un ELO de depart suffisant pour eviter le floor."""
    col = repository.get_elo_col(db, guild_id)
    col.delete_many({})
    for i in ids:
        col.insert_one({
            "_id": str(i), "name": f"P{i}",
            "elo": baseline, "wins": 0, "losses": 0,
        })


def test_zero_sum_with_uniform_multipliers():
    """Multiplicateurs tous a 1.0 -> comportement plat, sum(deltas)=0."""
    import bot as bot_module
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=10000)
    match = _make_match(status="validated_a", elo=2400)
    multipliers = {str(i): 1.0 for i in range(10)}
    outcome = apply_match_validation(
        bot_module.db, 42, match, multipliers=multipliers,
    )
    assert sum(c.delta for c in outcome.changes) == 0


def test_zero_sum_with_mixed_multipliers():
    """Multiplicateurs heterogenes (extremes inclus) -> sum(deltas)=0 quand
    aucun joueur ne touche le plancher."""
    import bot as bot_module
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=10000)
    match = _make_match(status="validated_a", elo=2400)
    multipliers = {
        # team_a (gagnants)
        "0": 1.3, "1": 1.3, "2": 1.0, "3": 0.7, "4": 0.7,
        # team_b (perdants)
        "5": 1.3, "6": 0.7, "7": 1.0, "8": 1.1, "9": 0.9,
    }
    outcome = apply_match_validation(
        bot_module.db, 42, match, multipliers=multipliers,
    )
    assert sum(c.delta for c in outcome.changes) == 0


def test_zero_sum_with_all_clamped_max():
    """Cas pathologique : tous gagnants clampes a 1.3, tous perdants a 0.7.
    Avec ancrage par-joueur (mult=1.0 -> +/-base), zero-sum tient encore
    par symetrie (sum_w + sum_l = 2n=10), mais le total equipe scale
    avec sum(mults)."""
    import bot as bot_module
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=10000)
    match = _make_match(status="validated_a", elo=2400)
    multipliers = {
        **{str(i): 1.3 for i in range(5)},      # team_a
        **{str(i): 0.7 for i in range(5, 10)},  # team_b
    }
    outcome = apply_match_validation(
        bot_module.db, 42, match, multipliers=multipliers,
    )
    assert sum(c.delta for c in outcome.changes) == 0
    # Chaque gagnant : round(16 * 1.3) = 21. Total = 5 * 21 = 105.
    winner_sum = sum(c.delta for c in outcome.changes if c.win)
    assert winner_sum == 105


def test_winner_with_higher_mult_gains_more():
    """Distribution interne : un gagnant a mult eleve gagne plus que ses
    coequipiers (la garantie zero-sum n'ecrase pas la difference de perf)."""
    import bot as bot_module
    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=10000)
    match = _make_match(status="validated_a", elo=2400)
    multipliers = {
        "0": 1.3, "1": 0.7,
        "2": 1.0, "3": 1.0, "4": 1.0,
        **{str(i): 1.0 for i in range(5, 10)},
    }
    outcome = apply_match_validation(
        bot_module.db, 42, match, multipliers=multipliers,
    )
    by_uid = {c.user_id: c for c in outcome.changes}
    assert by_uid["0"].delta > by_uid["2"].delta > by_uid["1"].delta
