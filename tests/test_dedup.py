"""The HA integration's cross-room grouping (custom_components/whistle/dedup.py, which has no HA imports)."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "dedup", Path(__file__).parents[1] / "custom_components" / "whistle" / "dedup.py"
)
dedup = sys.modules["dedup"] = importlib.util.module_from_spec(spec)  # dataclasses look it up
spec.loader.exec_module(dedup)


def det(node, level, t=1000.0, received=0.0, etype="hlh", id_=None):
    return dedup.Detection(node, etype, id_ or f"{node}-1", t, received, level, {})


def grouper():
    return dedup.Grouper(match_s=0.4, collect_s=1.5, confident_db=3.0)


def test_loudest_wins_and_confidence():
    g = grouper()
    grp, new = g.add(det("a", 20))
    assert new
    assert g.add(det("b", 30, t=1000.2)) == (grp, False)
    assert g.add(det("c", 28, t=999.9)) == (grp, False)
    v = g.close(grp)
    assert [d.node for d in v.ranked] == ["b", "c", "a"]
    assert v.winner.node == "b" and not v.confident  # 2 dB margin
    assert g.close(grp) is None


def test_single_node_is_confident():
    g = grouper()
    grp, _ = g.add(det("a", 20))
    assert g.close(grp).confident


def test_separate_whistles_split():
    g = grouper()
    g1, _ = g.add(det("a", 20, t=1000.0))
    g2, new = g.add(det("b", 20, t=1001.0))  # too far apart in time
    assert new and g2 is not g1
    g3, new = g.add(det("a", 20, t=1000.1, id_="a-2"))  # same node twice: a second whistle
    assert new and g3 is not g1
    g4, new = g.add(det("c", 20, etype="other"))
    assert new


def test_unsynced_clock_falls_back_to_arrival():
    g = grouper()
    grp, _ = g.add(det("a", 20, t=0, received=10.0))
    assert g.add(det("b", 25, t=0, received=11.0)) == (grp, False)
    _, new = g.add(det("c", 25, t=0, received=12.0))
    assert new
