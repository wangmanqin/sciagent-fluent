"""tests/test_geometry_inspector.py - W4 几何感知 (mock session)."""
from tools.geometry_inspector import (
    discover_zones,
    find_chip_zone,
    _MockSession,
)


def test_manifold_solid_up_detected():
    session = _MockSession(["fluid1"], ["solid_up"])
    result = discover_zones(session)
    assert "solid_up" in result["chip"]


def test_pcb_die_detected():
    session = _MockSession(["air"], ["die", "pcb"])
    result = discover_zones(session)
    assert "die" in result["chip"]


def test_silicon_chip_detected():
    session = _MockSession(["coolant"], ["silicon_chip", "heatsink_fin"])
    result = discover_zones(session)
    assert "silicon_chip" in result["chip"]
    assert "heatsink_fin" in result["heatsink"]


def test_fluid_zone_classified():
    session = _MockSession(["air", "water"], ["chip"])
    result = discover_zones(session)
    assert "air" in result["fluid"]
    assert "water" in result["fluid"]


def test_ambiguous_solid_falls_to_chip():
    """没关键词但是 solid 类型的 zone, 应该被推到 chip 候选."""
    session = _MockSession(["fluid"], ["body_a"])
    result = discover_zones(session)
    assert "body_a" in result["chip"]


def test_find_chip_zone_returns_string():
    session = _MockSession(["fluid1"], ["solid_up"])
    z = find_chip_zone(session)
    assert isinstance(z, str)
    assert z == "solid_up"


def test_find_chip_zone_fallback():
    """完全空的 session, 用 fallback."""
    session = _MockSession([], [])
    z = find_chip_zone(session, fallback="default_chip")
    assert z == "default_chip"


def test_empty_session_no_crash():
    session = _MockSession([], [])
    result = discover_zones(session)
    assert result == {"chip": [], "fluid": [], "heatsink": []}
