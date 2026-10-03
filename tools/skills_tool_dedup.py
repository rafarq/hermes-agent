"""skill_view repeat-view dedup registry: per-task cache of (skill name, file_path) ->
(skill file mtime+size). A repeat view of an UNCHANGED file returns a short stub — the earlier
tool result already carries the content verbatim. Cleared via ``reset_skill_view_dedup()`` on
context compression AND on a committed proactive tool-result prune, because both replace the
original content with a one-line marker.
"""

import json
import os
import threading
from typing import Dict

_skill_view_tracker: Dict[str, Dict[tuple, tuple]] = {}
_skill_view_tracker_lock = threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this "
    "conversation — refer to the earlier skill_view result; it is still "
    "current and complete. (Re-issued after context compression, this "
    "returns the full content again.)")


def _skill_view_fingerprint(payload: dict) -> tuple | None:
    """Stat the skill file a successful skill_view served, for change detection."""
    if not (src := payload.get("_source_path")):
        return None
    try:
        st = os.stat(src)
        return (src, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _record_skill_view(task_id, name, file_path, payload: dict) -> None:
    """Record a served skill_view so an identical repeat can be deduped."""
    # Never dedup setup-needed views: readiness depends on config/env state that
    # changes without the file changing; the model must see the refreshed status.
    if (not task_id or payload.get("setup_needed")
            or payload.get("readiness_status") == "setup_needed"):
        return
    if (fp := _skill_view_fingerprint(payload)) is None:
        return
    key = (str(payload.get("name") or name), file_path or "")
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        cache[key] = fp
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:  # FIFO eviction
            del cache[next(iter(cache))]


def _check_skill_view_dedup(task_id, name, file_path) -> str | None:
    """Dedup stub when this exact skill file was already served to this task and
    is unchanged on disk; None otherwise."""
    if not task_id:
        return None
    n = str(name)
    with _skill_view_tracker_lock:
        if not (cache := _skill_view_tracker.get(str(task_id))):
            return None
        # Record key is the RESOLVED name; match raw and resolved forms so
        # 'category/skill' and bare-name views coalesce.
        for key, (src, mtime_ns, size) in list(cache.items()):
            rec_name, rec_fp = key
            if rec_fp != (file_path or "") or not _names_match(rec_name, n):
                continue
            try:
                st = os.stat(src)
                changed = (st.st_mtime_ns, st.st_size) != (mtime_ns, size)
            except OSError:
                changed = True
            if changed:
                cache.pop(key, None)
                return None
            return json.dumps({
                "success": True, "status": "unchanged", "name": rec_name,
                "file": file_path or "SKILL.md", "dedup": True, "content_returned": False,
                "message": _SKILL_VIEW_DEDUP_MESSAGE}, ensure_ascii=False)
    return None


def _names_match(recorded: str, queried: str) -> bool:
    """True when *recorded* (the resolved name) and *queried* (raw or qualified form) denote the
    same skill: bare name, ``category/name`` and ``plugin:name`` forms all coalesce."""
    rec, n = str(recorded), str(queried)
    return (rec == n or n.endswith("/" + rec) or rec.endswith("/" + n)
            or n.split(":")[-1] == rec or rec.split(":")[-1] == n.split("/")[-1])


def invalidate_skill_view_dedup_for_skill(name: str) -> int:
    """Drop every recorded skill_view for *name* across all tasks; returns how many were dropped.

    Called where a skill body is demoted to the canonical ``[SKILL_PRUNED: ...]`` marker: the marker
    states the instructions are gone and asks for a ``skill_view`` reload, so the repeat-view dedup
    must not answer "unchanged" for it — otherwise the reload the marker asks for is refused and the
    skill is unrecoverable for the rest of the session. Doing this at EMIT time (rather than only at a
    compaction / committed-prune boundary) closes the window where a demotion lands on a pass that
    reports no change, which keeps the dedup entry alive.
    """
    dropped = 0
    with _skill_view_tracker_lock:
        for task_id in list(_skill_view_tracker):
            cache = _skill_view_tracker[task_id]
            for key in list(cache):
                if _names_match(key[0], name):
                    cache.pop(key, None)
                    dropped += 1
            if not cache:
                _skill_view_tracker.pop(task_id, None)
    return dropped


def reset_skill_view_dedup(task_id: str | None = None) -> None:
    """Clear the dedup cache (all tasks when task_id is None); called on context compression."""
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
        else:
            _skill_view_tracker.pop(str(task_id), None)
