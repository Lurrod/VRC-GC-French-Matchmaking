"""Tests unitaires de services.elo_calc — logique pure."""

from services import elo_calc


# ── Constantes ────────────────────────────────────────────────────
def test_maps_list_not_empty():
    assert len(elo_calc.MAPS) >= 5
    assert "Ascent" in elo_calc.MAPS
