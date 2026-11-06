"""Admission control + confinement tests (#641).

Plants symlinks, hard links and FIFOs in a managed tree and asserts
refusal — the status-site analogue of agentry-dashboard's
test_confinement.py.
"""

from __future__ import annotations

import os

import pytest

from status_site import admission

MANAGED = ("out", os.path.join("out", ".last-good"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(admission.SCRATCH_ENV, raising=False)


def _tree(tmp_path):
    root = tmp_path / "root"
    (root / "out" / ".last-good").mkdir(parents=True)
    return root


def test_refuses_without_opt_in(tmp_path):
    status, why, root = admission.admit(str(_tree(tmp_path)), managed=MANAGED)
    assert status == admission.ADMIT_NO_OPT_IN and root is None and "opt-in" in why


def test_production_clean_tree_returns_canonical_root(tmp_path):
    r = _tree(tmp_path)
    (r / "out" / "index.html").write_text("x")
    status, why, root = admission.admit(str(r), production=True, managed=MANAGED)
    assert (status, why) == (0, "")
    assert root == os.path.realpath(r)


def test_root_symlink_refused(tmp_path):
    r = _tree(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(r)
    status, why, root = admission.admit(str(link), production=True, managed=MANAGED)
    assert status == admission.ADMIT_REFUSED and "symbolic link" in why and root is None


def test_managed_dir_symlink_refused(tmp_path):
    r = tmp_path / "root"
    r.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (r / "out").symlink_to(elsewhere)
    status, why, _ = admission.admit(str(r), production=True, managed=MANAGED)
    assert status == admission.ADMIT_REFUSED and "symbolic link" in why


def test_entry_symlink_refused(tmp_path):
    r = _tree(tmp_path)
    target = tmp_path / "secret"
    target.write_text("s")
    (r / "out" / "index.html").symlink_to(target)
    status, why, _ = admission.admit(str(r), production=True, managed=MANAGED)
    assert status == admission.ADMIT_REFUSED and "symbolic link" in why


def test_fifo_entry_refused(tmp_path):
    r = _tree(tmp_path)
    os.mkfifo(r / "out" / "page.html")
    status, why, _ = admission.admit(str(r), production=True, managed=MANAGED)
    assert status == admission.ADMIT_REFUSED and "FIFO" in why


def test_hardlinked_file_refused(tmp_path):
    r = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("o")
    os.link(outside, r / "out" / "page.html")
    status, why, _ = admission.admit(str(r), production=True, managed=MANAGED)
    assert status == admission.ADMIT_REFUSED and "hard links" in why


def test_scratch_confinement(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    inside = scratch / "run"
    (inside / "out" / ".last-good").mkdir(parents=True)
    outside = _tree(tmp_path)
    monkeypatch.setenv(admission.SCRATCH_ENV, str(scratch))
    assert admission.admit(str(inside), managed=MANAGED)[0] == 0
    status, why, _ = admission.admit(str(outside), managed=MANAGED)
    assert status == admission.ADMIT_REFUSED and "not under the scratch root" in why
    # the scratch root itself is not an allowed output
    assert admission.admit(str(scratch), managed=MANAGED)[0] == admission.ADMIT_REFUSED


def test_scratch_escape_via_dotdot_and_symlink(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = _tree(tmp_path)
    (scratch / "sneaky").symlink_to(outside)
    monkeypatch.setenv(admission.SCRATCH_ENV, str(scratch))
    assert admission.admit(str(scratch / "sneaky" / ".." / "root"), managed=MANAGED)[0] == admission.ADMIT_REFUSED
    assert admission.admit(str(scratch / "sneaky"), managed=MANAGED)[0] == admission.ADMIT_REFUSED


def test_scratch_too_broad(monkeypatch):
    monkeypatch.setenv(admission.SCRATCH_ENV, "/")
    assert "too broad" in admission.check_scratch("/tmp/x")


def test_open_nofollow_refuses_leaf_symlink_and_leaves_target_alone(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    link = tmp_path / "leaf"
    link.symlink_to(target)
    with pytest.raises(OSError):
        admission.open_nofollow(str(link))
    assert target.read_text() == "keep"
    with pytest.raises(OSError):
        admission.read_nofollow(str(link))


def test_open_nofollow_fifo_does_not_block(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        admission.open_nofollow(str(fifo))
    with pytest.raises(admission.NotRegularFile):
        admission.read_nofollow(str(fifo))


def test_open_nofollow_exclusive_replaces_stale_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    tmp = tmp_path / "page.tmp"
    tmp.symlink_to(target)
    with admission.open_nofollow(str(tmp), exclusive=True) as f:
        f.write("new")
    assert target.read_text() == "keep"
    assert tmp.read_text() == "new" and not tmp.is_symlink()


def test_open_nofollow_truncates_regular_file(tmp_path):
    p = tmp_path / "f"
    p.write_text("longer content")
    with admission.open_nofollow(str(p)) as f:
        f.write("x")
    assert p.read_text() == "x"


def test_freeze_roundtrip_and_tamper(tmp_path):
    (tmp_path / "index.html").write_text("<html>v1</html>")
    assert admission.verify_freeze(str(tmp_path), "index.html") == ""  # no .FREEZE → not pinned
    sha = admission.write_freeze(str(tmp_path), "index.html")
    assert len(sha) == 64
    assert admission.verify_freeze(str(tmp_path), "index.html") == ""
    (tmp_path / "index.html").write_text("<html>tampered</html>")
    assert "does not match" in admission.verify_freeze(str(tmp_path), "index.html")


def test_freeze_symlinked_page_refused(tmp_path):
    real = tmp_path / "real.html"
    real.write_text("x")
    (tmp_path / "index.html").symlink_to(real)
    with pytest.raises(OSError):
        admission.freeze_sha(str(tmp_path / "index.html"))
