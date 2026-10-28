import json
import threading

from status_site import server as S


def test_open_adds_detail_page_as_tab(tmp_path):
    st = tmp_path / "status"
    st.mkdir()
    (st / "pages.json").write_text(json.dumps([{"slug": "index", "title": "Overview"}]))
    (st / "bug-reports.md").write_text("# In-game bug reports\n\n- [ ] x\n")
    cfg = type("C", (), {"status_dir": st, "key": "k", "lock": threading.Lock()})()
    assert S.list_pages(cfg) == ["bug-reports"]
    out = S.handle_page_visibility(
        cfg, {"key": "k", "slug": "bug-reports", "action": "open"}
    )
    assert {"slug": "bug-reports", "title": "In-game bug reports"} in out["pages"]
    # opening again is idempotent and unhides
    S.handle_page_visibility(cfg, {"key": "k", "slug": "bug-reports"})
    out = S.handle_page_visibility(
        cfg, {"key": "k", "slug": "bug-reports", "action": "open"}
    )
    assert sum(p["slug"] == "bug-reports" for p in out["pages"]) == 1
    assert not any(p.get("hidden") for p in out["pages"])
