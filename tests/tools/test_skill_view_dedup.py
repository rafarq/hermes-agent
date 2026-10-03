"""Tests for skill_view repeat-view dedup (unchanged-skill stub)."""

import json
import time

import pytest

from tools.skills_tool import (
    _skill_view_with_bump,
    reset_skill_view_dedup,
)

@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skills = home / "skills"
    d = skills / "demo-dedup-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: demo-dedup-skill\ndescription: Demo skill for dedup tests.\n---\n"
        "# Demo\n\nStep one: run the demo procedure fully.\n",
        encoding="utf-8",
    )
    refs = d / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\n\nDetailed reference content here.\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_skill_view_dedup()
    from tools.skill_manager_guards import _reset_background_review_read_marks
    _reset_background_review_read_marks()
    return home

def _view(name, file_path=None, task="t-svd"):
    args = {"name": name}
    if file_path:
        args["file_path"] = file_path
    return json.loads(_skill_view_with_bump(args, task_id=task))

class TestSkillViewDedup:
    def test_first_view_returns_full_content(self, skills_home):
        r = _view("demo-dedup-skill")
        assert r["success"] is True
        assert "Step one" in r.get("content", "")

    def test_repeat_view_returns_stub(self, skills_home):
        _view("demo-dedup-skill")
        r2 = _view("demo-dedup-skill")
        assert r2["success"] is True
        assert r2.get("dedup") is True
        assert r2.get("content_returned") is False
        assert "content" not in r2

    def test_modified_skill_returns_full_content(self, skills_home):
        _view("demo-dedup-skill")
        md = skills_home / "skills" / "demo-dedup-skill" / "SKILL.md"
        time.sleep(0.01)
        md.write_text(md.read_text(encoding="utf-8") + "\nStep two: new instruction.\n", encoding="utf-8")
        r2 = _view("demo-dedup-skill")
        assert "Step two" in r2.get("content", "")
        assert r2.get("dedup") is None

    def test_linked_file_dedup_is_independent(self, skills_home):
        _view("demo-dedup-skill")
        # First view of a DIFFERENT file within the skill: full content.
        r = _view("demo-dedup-skill", file_path="references/guide.md")
        assert "Detailed reference" in r.get("content", "")
        # Repeat of that file: stub.
        r2 = _view("demo-dedup-skill", file_path="references/guide.md")
        assert r2.get("dedup") is True

    def test_different_tasks_do_not_share_cache(self, skills_home):
        _view("demo-dedup-skill", task="task-A")
        r = _view("demo-dedup-skill", task="task-B")
        assert "Step one" in r.get("content", "")

    def test_reset_returns_full_content(self, skills_home):
        _view("demo-dedup-skill")
        reset_skill_view_dedup("t-svd")
        r2 = _view("demo-dedup-skill")
        assert "Step one" in r2.get("content", "")

    def test_no_task_id_never_dedups(self, skills_home):
        args = {"name": "demo-dedup-skill"}
        r1 = json.loads(_skill_view_with_bump(args, task_id=None))
        r2 = json.loads(_skill_view_with_bump(args, task_id=None))
        assert "Step one" in r2.get("content", "")

    def test_background_review_skips_dedup_and_marks_read(self, skills_home):
        from tools.skill_provenance import (
            reset_current_write_origin,
            set_current_write_origin,
        )

        _view("demo-dedup-skill")

        token = set_current_write_origin("background_review")
        try:
            review = _view("demo-dedup-skill")
        finally:
            reset_current_write_origin(token)

        assert review["success"] is True
        assert "Step one" in review.get("content", "")
        assert review.get("dedup") is None
        assert review.get("content_returned") is None
        # The real read marks the file, so the fork's read-before-write guard now admits the patch.
        from tools.skill_manager_guards import _background_review_has_read
        assert _background_review_has_read(skills_home / "skills" / "demo-dedup-skill" / "SKILL.md")

    def test_background_review_does_not_pollute_foreground_cache(self, skills_home):
        from tools.skill_provenance import (
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin("background_review")
        try:
            _view("demo-dedup-skill")
        finally:
            reset_current_write_origin(token)

        foreground = _view("demo-dedup-skill")
        assert "Step one" in foreground.get("content", "")

        repeat = _view("demo-dedup-skill")
        assert repeat.get("dedup") is True
        assert repeat.get("content_returned") is False


class TestPruneMarkerReleasesDedup:
    """A body demoted to the canonical [SKILL_PRUNED: ...] marker must release the dedup entry at
    EMIT time, or the reload the marker asks for is refused with an 'unchanged' stub (#112763)."""

    def test_invalidate_returns_content_again(self, skills_home):
        from tools.skills_tool_dedup import invalidate_skill_view_dedup_for_skill

        _view("demo-dedup-skill")
        assert _view("demo-dedup-skill").get("dedup") is True
        assert invalidate_skill_view_dedup_for_skill("demo-dedup-skill") == 1
        reloaded = _view("demo-dedup-skill")
        assert "Step one" in reloaded.get("content", "")
        assert reloaded.get("dedup") is None

    def test_invalidate_matches_qualified_forms(self, skills_home):
        from tools.skills_tool_dedup import invalidate_skill_view_dedup_for_skill

        _view("demo-dedup-skill")
        # The marker carries the name the model passed, which may be category- or plugin-qualified.
        assert invalidate_skill_view_dedup_for_skill("media:demo-dedup-skill") == 1
        assert "Step one" in _view("demo-dedup-skill").get("content", "")

    def test_invalidate_leaves_other_skills_stubbed(self, skills_home):
        from tools.skills_tool_dedup import invalidate_skill_view_dedup_for_skill

        other = skills_home / "skills" / "other-dedup-skill"
        other.mkdir()
        (other / "SKILL.md").write_text(
            "---\nname: other-dedup-skill\ndescription: Other demo skill.\n---\n"
            "# Other\n\nStep other: unrelated procedure.\n",
            encoding="utf-8",
        )
        _view("demo-dedup-skill")
        _view("other-dedup-skill")
        assert invalidate_skill_view_dedup_for_skill("demo-dedup-skill") == 1
        assert _view("demo-dedup-skill").get("dedup") is None
        assert _view("other-dedup-skill").get("dedup") is True

    def test_invalidate_unknown_skill_is_a_noop(self, skills_home):
        from tools.skills_tool_dedup import invalidate_skill_view_dedup_for_skill

        _view("demo-dedup-skill")
        assert invalidate_skill_view_dedup_for_skill("no-such-skill") == 0
        assert _view("demo-dedup-skill").get("dedup") is True

    def test_marker_emit_then_view_returns_full_content(self, skills_home):
        """End to end: the sequence that dead-ends today — view, body demoted to the marker, reload."""
        from agent.context_compressor import (
            SKILL_PRUNED_MARKER_PREFIX,
            _summarize_tool_result,
        )

        _view("demo-dedup-skill")
        assert _view("demo-dedup-skill").get("dedup") is True
        summary = _summarize_tool_result(
            "skill_view", '{"name":"demo-dedup-skill"}', "x" * 6000
        )
        assert SKILL_PRUNED_MARKER_PREFIX in summary
        assert "demo-dedup-skill" in summary
        reloaded = _view("demo-dedup-skill")
        assert "Step one" in reloaded.get("content", "")
        assert reloaded.get("dedup") is None
