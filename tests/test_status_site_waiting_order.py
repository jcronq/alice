import json
import re
import shutil
import subprocess

import pytest

from status_site import server as S


def _cfg_real(tmp_path):
    st = tmp_path / "status"
    st.mkdir()
    cfg = S.Config(
        status_dir=st, surface_dir=tmp_path / "s", upload_dir=tmp_path / "u", key="k"
    )
    return st, cfg


def test_post_round_trips_to_get(tmp_path):
    st, cfg = _cfg_real(tmp_path)
    assert S.read_waiting_order(cfg) == []
    out = S.handle_waiting_order(cfg, {"key": "k", "order": ["c", "a-1", "b"]})
    assert out["order"] == ["c", "a-1", "b"]
    assert S.read_waiting_order(cfg) == ["c", "a-1", "b"]
    assert json.loads((st / ".waiting-order.json").read_text()) == {
        "order": ["c", "a-1", "b"]
    }


def test_duplicates_collapsed(tmp_path):
    _, cfg = _cfg_real(tmp_path)
    S.handle_waiting_order(cfg, {"key": "k", "order": ["a", "b", "a"]})
    assert S.read_waiting_order(cfg) == ["a", "b"]


def test_bad_key_403(tmp_path):
    st, cfg = _cfg_real(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_waiting_order(cfg, {"key": "nope", "order": ["a"]})
    assert e.value.code == 403
    assert not (st / ".waiting-order.json").exists()


@pytest.mark.parametrize(
    "order",
    [["../x"], ["A"], ["a b"], [""], [1], "a", None, ["a"] * 501, ["a" * 61]],
)
def test_bad_order_rejected(tmp_path, order):
    st, cfg = _cfg_real(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_waiting_order(cfg, {"key": "k", "order": order})
    assert e.value.code == 400
    assert not (st / ".waiting-order.json").exists()


def test_atomic_write_leaves_no_tmp(tmp_path):
    st, cfg = _cfg_real(tmp_path)
    S.handle_waiting_order(cfg, {"key": "k", "order": ["a"]})
    S.handle_waiting_order(cfg, {"key": "k", "order": ["b", "a"]})
    assert sorted(p.name for p in st.iterdir()) == [".waiting-order.json"]


def test_read_ignores_corrupt_file(tmp_path):
    st, cfg = _cfg_real(tmp_path)
    (st / ".waiting-order.json").write_text("{nope")
    assert S.read_waiting_order(cfg) == []
    (st / ".waiting-order.json").write_text(json.dumps({"order": ["a", "../x", 3]}))
    assert S.read_waiting_order(cfg) == ["a"]


def test_dotfile_not_served_statically(tmp_path):
    _, cfg = _cfg_real(tmp_path)
    assert not S.CONTENT_NAME_RE.match(cfg.waiting_order.name)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "natural,saved,expected",
    [
        (["a", "b", "c"], ["c", "a"], ["c", "b", "a"]),
        (["a", "b", "c"], [], ["a", "b", "c"]),
        (["a", "b", "c"], ["c", "b", "a"], ["c", "b", "a"]),
        (["a", "b", "c", "d"], ["d", "x", "a"], ["d", "b", "c", "a"]),
        (["a", "b"], ["x", "y"], ["a", "b"]),
    ],
)
def test_merge_waiting_order(natural, saved, expected):
    html = (S.STATIC_DIR / "index.html").read_text()
    m = re.search(r"// <merge-order>(.*?)// </merge-order>", html, re.S)
    assert m
    script = (
        m.group(1)
        + f"console.log(JSON.stringify(mergeWaitingOrder({json.dumps(natural)},{json.dumps(saved)})))"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out) == expected
