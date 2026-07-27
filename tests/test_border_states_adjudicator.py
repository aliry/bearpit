"""Border States adjudicator — the Full-core ruleset contract.

The adjudicator lives as a seeded resource (examples/border-states/.../adjudicator.py), not a
package module, so we load it by path. `_resolve` is pure (units, orders, adj, homes) -> outcome;
`resolve_season` is the file-backed entry the Cartographer runs.
"""

import importlib.util
import json
import pathlib

_ADJ_PATH = (pathlib.Path(__file__).parent.parent
             / "examples/border-states/agents/cartographer/resources/adjudicator.py")
_spec = importlib.util.spec_from_file_location("bs_adjudicator", _ADJ_PATH)
adj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adj)

# a small test map:  A—B—D—E line, with C bridging A/B/D
ADJ = {"A": ["B", "C"], "B": ["A", "C", "D"], "C": ["A", "B", "D"],
       "D": ["B", "C", "E"], "E": ["D"]}
HOMES = {"x": ["A"], "y": ["B"], "z": ["D"], "w": ["E"]}


def _r(units, orders):
    return adj._resolve(units, orders, ADJ, HOMES)


def test_simple_move_into_empty():
    land, dis, _ = _r({"A": "x"}, {"A": ("MOVE", "B")})
    assert land["A"] == "B" and not dis


def test_two_movers_bounce():
    land, dis, _ = _r({"A": "x", "C": "y"}, {"A": ("MOVE", "B"), "C": ("MOVE", "B")})
    assert land["A"] == "A" and land["C"] == "C" and not dis


def test_supported_move_dislodges_and_retreats():
    # A->B with C's support (str 2) beats B holding (str 1); B is dislodged and retreats to D
    land, dis, _ = _r(
        {"A": "x", "C": "x", "B": "y"},
        {"A": ("MOVE", "B"), "C": ("SMOVE", "A", "B"), "B": ("HOLD",)},
    )
    assert land["A"] == "B"
    assert land["B"] == "D" and not dis  # A is attacker-origin, C occupied -> D is the retreat


def test_support_is_cut():
    # D attacks C, cutting C's support of A->B; A (now str 1) bounces off B (hold 1)
    land, _, _ = _r(
        {"A": "x", "C": "x", "B": "y", "D": "z"},
        {"A": ("MOVE", "B"), "C": ("SMOVE", "A", "B"), "B": ("HOLD",), "D": ("MOVE", "C")},
    )
    assert land["A"] == "A"


def test_head_to_head_equal_bounces():
    land, dis, _ = _r({"A": "x", "B": "y"}, {"A": ("MOVE", "B"), "B": ("MOVE", "A")})
    assert land["A"] == "A" and land["B"] == "B" and not dis


def test_head_to_head_unequal_dislodges():
    land, dis, _ = _r(
        {"A": "x", "B": "y", "C": "x"},
        {"A": ("MOVE", "B"), "B": ("MOVE", "A"), "C": ("SMOVE", "A", "B")},
    )
    assert land["A"] == "B"
    assert land["B"] == "D"  # dislodged, retreats (A is attacker origin, C occupied)


def test_no_self_dislodgement():
    # A can't take B even with support, because B holds a unit of A's own power
    land, dis, _ = _r(
        {"A": "x", "B": "x", "C": "x"},
        {"A": ("MOVE", "B"), "B": ("HOLD",), "C": ("SMOVE", "A", "B")},
    )
    assert land["A"] == "A" and land["B"] == "B" and not dis


def test_dislodged_with_no_retreat_disbands():
    # B is dislodged; every neighbour is the attacker-origin or occupied -> disband
    land, dis, _ = _r(
        {"A": "x", "C": "x", "B": "y", "D": "w"},
        {"A": ("MOVE", "B"), "C": ("SMOVE", "A", "B"), "B": ("HOLD",), "D": ("HOLD",)},
    )
    assert "B" in dis and land["A"] == "B"


def test_rotation_all_move():
    # A->B, B->D, D->... a chain into an empty end: all shift
    land, dis, _ = _r(
        {"A": "x", "B": "y", "D": "z"},
        {"A": ("MOVE", "C"), "B": ("MOVE", "A"), "D": ("MOVE", "B")},
    )
    # C empty <- A ; A empty <- B ; B empty <- D ; none opposed
    assert land["A"] == "C" and land["B"] == "A" and land["D"] == "B" and not dis


def test_parse_rejects_foreign_and_illegal():
    o, notes = adj._parse_orders("A MOVE E", "x", {"A": "x"}, ADJ)  # E not adjacent to A
    assert o == {} and notes
    o, notes = adj._parse_orders("B HOLD", "x", {"A": "x"}, ADJ)    # B is not x's unit
    assert o == {} and notes
    o, _ = adj._parse_orders("A MOVE B; A SUPPORT B HOLD", "x", {"A": "x", "B": "x"}, ADJ)
    assert o["A"] == ("MOVE", "B")  # first order wins; duplicate dropped


def test_fall_capture_and_build(tmp_path, monkeypatch):
    board = {
        "adjacency": {"A": ["H", "N"], "H": ["A"], "N": ["A", "B"], "B": ["N"]},
        "centers": ["A", "N", "B"],
        "homes": {"x": ["A", "H"]},
        "units": {"A": "x"},
        "owners": {"A": "x"},
        "season": "Y1-Fall",
    }
    p = tmp_path / "board.json"
    p.write_text(json.dumps(board))
    monkeypatch.setattr(adj, "_BOARD", str(p))
    adj.resolve_season("Y1-Fall", {"x": "A MOVE N"})
    b = json.loads(p.read_text())
    assert b["owners"]["N"] == "x"                       # neutral center captured in Fall
    assert set(b["units"]) == {"N", "A"}                 # moved to N, then built back on home A
    assert b["season"] == "Y2-Spring"


def test_real_board_is_valid():
    b = json.loads((_ADJ_PATH.parent / "board.json").read_text())
    a = b["adjacency"]
    assert len(b["centers"]) == 11
    for prov, ns in a.items():
        for n in ns:
            assert prov in a[n], f"adjacency not symmetric: {prov}-{n}"
    reachable = adj._dist_to(a, {"COR"})
    assert set(reachable) == set(a), "some province is unreachable"
