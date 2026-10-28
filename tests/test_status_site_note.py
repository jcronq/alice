import threading

from status_site import server as S


def test_read_note(tmp_path):
    st = tmp_path / "status"
    (st / "notes").mkdir(parents=True)
    (st / "notes" / "design-agreed.md").write_text("Agreed with Jason at 21:24.")
    cfg = type("C", (), {"status_dir": st, "key": "k", "lock": threading.Lock()})()
    n = S.read_note(cfg, "design-agreed")
    assert n is not None and "21:24" in n["markdown"]
    assert S.read_note(cfg, "missing") is None
    assert S.read_note(cfg, "../../etc/passwd") is None
    assert S.read_note(cfg, "") is None
