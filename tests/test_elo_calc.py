"""Tests unitaires de services.elo_calc — logique pure."""

from services import elo_calc


# ── Constantes ────────────────────────────────────────────────────
def test_maps_list_not_empty():
    assert len(elo_calc.MAPS) >= 5
    assert "Ascent" in elo_calc.MAPS


def test_elo_start_is_2000():
    """Default starting ELO is 2000 (was 0). Players are seeded at 2000
    when they first appear in any queue."""
    from services.elo_calc import ELO_START
    assert ELO_START == 2000
