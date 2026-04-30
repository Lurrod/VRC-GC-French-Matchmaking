"""Tests de la mise a jour ELO post-vote (Phase 6)."""

import pytest

from services import elo_calc, repository
from services.elo_updater import (
    apply_match_validation,
    MatchEloOutcome,
    PlayerEloChange,
)


# ── compute_match_elo_change (formule pure, zero-sum) ─────────────
@pytest.mark.parametrize("avg,change", [
    (0,    0),
    (300,  2),
    (2100, 13),     # sous le floor (Immortal-)
    (2400, 15),    # Immortal 1 = baseline
    (2700, 17),    # Immortal 3
    (3000, 19),    # Radiant
])
def test_compute_match_elo_change(avg, change):
    """Zero-sum strict : gain == loss."""
    g, l = elo_calc.compute_match_elo_change(avg)
    assert g == change
    assert l == change
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

    assert outcome.gain == 15      # zero-sum a avg=2400
    assert outcome.loss == 15
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
    match = _make_match(elo=2400)  # avg=2400 -> change=15
    apply_match_validation(bot_module.db, 42, match)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    for i in range(5):
        doc = elo_col.find_one({"_id": str(i)})
        assert doc["elo"] == 15
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

    match = _make_match(elo=2400)  # loss=15
    apply_match_validation(bot_module.db, 42, match)

    doc = elo_col.find_one({"_id": "5"})
    assert doc["elo"] == 35
    assert doc["losses"] == 1


def test_loser_floored_at_zero():
    import bot as bot_module
    elo_col = repository.get_elo_col(bot_module.db, 42)
    elo_col.insert_one({"_id": "5", "name": "B0", "elo": 5, "wins": 0, "losses": 0})

    match = _make_match(elo=2400)  # loss=15 mais courrant=5 -> 0
    apply_match_validation(bot_module.db, 42, match)

    doc = elo_col.find_one({"_id": "5"})
    assert doc["elo"] == 0


def test_high_elo_match_bigger_swings():
    import bot as bot_module
    # Radiant (avg=3000) zero-sum -> change=19
    match = _make_match(elo=3000)
    outcome = apply_match_validation(bot_module.db, 42, match)
    assert outcome.gain == 19
    assert outcome.loss == 19

    elo_col = repository.get_elo_col(bot_module.db, 42)
    assert elo_col.find_one({"_id": "0"})["elo"] == 19


def test_low_elo_match_smaller_swings():
    import bot as bot_module
    # avg sous le floor (300) zero-sum -> change=2
    match = _make_match(elo=300)
    outcome = apply_match_validation(bot_module.db, 42, match)
    assert outcome.gain == 2
    assert outcome.loss == 2


def test_existing_winner_keeps_history_and_adds_gain():
    import bot as bot_module
    elo_col = repository.get_elo_col(bot_module.db, 42)
    elo_col.insert_one({"_id": "0", "name": "A0", "elo": 200, "wins": 5, "losses": 3})

    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, 42, match)

    doc = elo_col.find_one({"_id": "0"})
    assert doc["elo"] == 215       # 200 + 15
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
    # Avg total = (5*2200 + 5*2600) / 10 = 2400 -> change 15
    assert outcome.avg_elo == 2400
    assert outcome.gain == 15


def test_change_dataclass_fields():
    import bot as bot_module
    match = _make_match(elo=2400)
    outcome = apply_match_validation(bot_module.db, 42, match)

    winner = next(c for c in outcome.changes if c.win)
    assert winner.delta == 15
    assert winner.old_elo == 0
    assert winner.new_elo == 15

    loser = next(c for c in outcome.changes if not c.win)
    assert loser.delta == 0   # 0 -> max(0, -15) -> 0, donc delta = 0
    assert loser.old_elo == 0
    assert loser.new_elo == 0


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
    Sans le fix, sum_w=6.5, sum_l=3.5 -> deficit. Avec le fix : sum=0."""
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
    # Total gagnants = 5 * 15 = 75 (zero-sum strict)
    winner_sum = sum(c.delta for c in outcome.changes if c.win)
    assert winner_sum == 75


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
