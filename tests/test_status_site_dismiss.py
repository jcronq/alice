import json
import threading

import pytest

from status_site import server as S


def test_dismiss_and_restore(tmp_path):
    st = tmp_path / "status"
    st.mkdir()
    (st / "pages.json").write_text(
        json.dumps(
            [
                {"slug": "index", "title": "Overview"},
                {"slug": "wave-a", "title": "Wave A"},
            ]
        )
    )
    cfg = type("C", (), {"status_dir": st, "key": "k", "lock": threading.Lock()})()
    assert (
        S.handle_page_visibility(cfg, {"key": "k", "slug": "wave-a"})["hidden"] is True
    )
    assert json.loads((st / "pages.json").read_text())[1]["hidden"] is True
    S.handle_page_visibility(cfg, {"key": "k", "slug": "wave-a", "action": "restore"})
    assert "hidden" not in json.loads((st / "pages.json").read_text())[1]
    with pytest.raises(S.ReplyError) as e:
        S.handle_page_visibility(cfg, {"key": "nope", "slug": "wave-a"})
    assert e.value.code == 403
    with pytest.raises(S.ReplyError) as e:
        S.handle_page_visibility(cfg, {"key": "k", "slug": "index"})
    assert e.value.code == 400
    with pytest.raises(S.ReplyError) as e:
        S.handle_page_visibility(cfg, {"key": "k", "slug": "../x"})
    assert e.value.code == 404
