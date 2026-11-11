"""Unit tests for marcus_runner.integrity."""

import pytest
from marcus_runner import integrity

pytestmark = pytest.mark.unit


def test_tree_hash_is_deterministic_and_order_free(tmp_path):
    """The same bytes hash the same whatever order the names are given in."""
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")
    one = integrity.tree_hash(tmp_path, ["a.py", "b.py"])
    assert one == integrity.tree_hash(tmp_path, ["b.py", "a.py"])
    (tmp_path / "b.py").write_text("c")
    assert one != integrity.tree_hash(tmp_path, ["a.py", "b.py"])


def test_a_missing_file_is_a_refusal(tmp_path):
    """Never a quietly shorter hash."""
    (tmp_path / "a.py").write_text("a")
    with pytest.raises(FileNotFoundError, match="missing"):
        integrity.tree_hash(tmp_path, ["a.py", "gone.py"])
    with pytest.raises(FileNotFoundError, match="not a directory"):
        integrity.tree_hash(tmp_path / "nope", ["a.py"])


def test_version_record_renders_like_the_minecraft_runs(tmp_path):
    """Outside a repository the fields are empty and dirty is zero; the layout holds."""
    version = integrity.git_version(tmp_path)
    assert version["dirty"] == "0"
    text = integrity.format_version(
        {"checkout": "/c", "branch": "develop", "commit": "abc", "dirty": "0"}
    )
    assert text == "checkout: /c\nbranch:   develop\ncommit:   abc\ndirty:    0 files\n"


def test_contains_commit_is_false_outside_a_repository(tmp_path):
    """A refusal, not an exception."""
    assert integrity.contains_commit(tmp_path, "deadbeef") is False
