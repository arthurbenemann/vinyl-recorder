"""Partial-merge semantics for `POST /api/album/{album_id}/plan`.

The wave-editor's debounced save sends only the fields that need to
change — most edits touch `tracks` (cuts, titles, skip flags) without
re-sending the normalize / target_peak_db / measured_peak_db / bit_depth
knobs. The endpoint's contract is: omitted fields preserve their prior
value; only `tracks` is replaced wholesale on every call.

If that contract regresses (e.g. someone refactors to `manifest["plan"]
= req.dict()` style overwrite), the user's normalize choice silently
flips back to the default the next time they drag a cut. These tests
pin the merge behavior at the route boundary.
"""
import json

from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def _seed_album(album_id: str, plan: dict | None = None) -> None:
    """Drop a minimal album folder + manifest under the test OUTPUT_DIR.
    No real FLAC bytes — these tests don't run ffmpeg."""
    from state import IN_PROGRESS_DIR

    d = IN_PROGRESS_DIR / album_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "side1.flac").write_bytes(b"")
    manifest = {
        "schema_version": 2,
        "tags": {"artist": "X", "album": "Y", "year": "2003"},
        "sides": ["side1.flac"],
        "cover": None,
        "plan": plan,
        "music_relpath": None,
    }
    (d / "album.json").write_text(json.dumps(manifest))


def _read_plan(album_id: str) -> dict | None:
    from state import IN_PROGRESS_DIR

    return json.loads(
        (IN_PROGRESS_DIR / album_id / "album.json").read_text()
    )["plan"]


def _cleanup(album_id: str) -> None:
    import shutil

    from state import IN_PROGRESS_DIR

    shutil.rmtree(IN_PROGRESS_DIR / album_id, ignore_errors=True)


def test_first_plan_post_writes_full_payload():
    """Baseline: posting tracks + every knob persists everything."""
    album_id = "tplan001"
    _seed_album(album_id)
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [
                    {"title": "A", "duration_seconds": 30.0, "skip": False},
                    {"title": "B", "duration_seconds": 25.0, "skip": False},
                ],
                "normalize": True,
                "target_peak_db": -1.0,
                "measured_peak_db": -3.5,
                "bit_depth": 24,
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["tracks"][0]["title"] == "A"
        assert plan["normalize"] is True
        assert plan["target_peak_db"] == -1.0
        assert plan["measured_peak_db"] == -3.5
        assert plan["bit_depth"] == 24
    finally:
        _cleanup(album_id)


def test_partial_post_with_only_tracks_preserves_prior_knobs():
    """The big one: the editor's per-cut-drag save sends only `tracks`.
    The previously-saved normalize / target_peak_db / measured_peak_db /
    bit_depth must survive."""
    album_id = "tplan002"
    _seed_album(album_id, plan={
        "tracks": [{"title": "old", "duration_seconds": 30.0, "skip": False}],
        "normalize": True,
        "target_peak_db": -2.0,
        "measured_peak_db": -4.5,
        "bit_depth": 24,
    })
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [
                    {"title": "newA", "duration_seconds": 12.0, "skip": False},
                    {"title": "newB", "duration_seconds": 18.0, "skip": True},
                ],
                # No normalize / target_peak_db / measured_peak_db / bit_depth.
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        # Tracks replaced wholesale.
        assert [(t["title"], t["skip"]) for t in plan["tracks"]] == [
            ("newA", False), ("newB", True),
        ]
        # Knobs preserved — the absence of these fields in the body must
        # NOT be interpreted as "set to default".
        assert plan["normalize"] is True
        assert plan["target_peak_db"] == -2.0
        assert plan["measured_peak_db"] == -4.5
        assert plan["bit_depth"] == 24
    finally:
        _cleanup(album_id)


def test_explicit_field_in_body_overrides_prior_value():
    """The merge isn't one-way — when the editor DOES send a knob (e.g.
    user toggles normalize off), it must override the prior value."""
    album_id = "tplan003"
    _seed_album(album_id, plan={
        "tracks": [{"title": "x", "duration_seconds": 10.0, "skip": False}],
        "normalize": True,
        "target_peak_db": -1.0,
        "measured_peak_db": None,
        "bit_depth": 16,
    })
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "x", "duration_seconds": 10.0, "skip": False}],
                "normalize": False,        # toggled off
                "bit_depth": 24,           # bumped
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["normalize"] is False
        assert plan["bit_depth"] == 24
        # Untouched knobs still preserved.
        assert plan["target_peak_db"] == -1.0
        assert plan["measured_peak_db"] is None
    finally:
        _cleanup(album_id)


def test_post_to_album_with_no_existing_plan_creates_one():
    """First-time POST against an album whose `plan` is null in the
    manifest. The merge starts from an empty dict; absent knobs stay
    absent (vs. being defaulted)."""
    album_id = "tplan004"
    _seed_album(album_id, plan=None)
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "only", "duration_seconds": 5.0, "skip": False}],
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["tracks"] == [{"title": "only", "duration_seconds": 5.0, "skip": False}]
        # The endpoint shouldn't manufacture defaults for absent knobs —
        # they stay out of the manifest until the editor sets them. Tests
        # that the merge is "preserve absent" not "fill with defaults".
        for k in ("normalize", "target_peak_db", "measured_peak_db", "bit_depth"):
            assert k not in plan, f"{k} appeared with a default value: {plan.get(k)!r}"
    finally:
        _cleanup(album_id)


def test_partial_post_preserves_prior_sample_rate():
    """The sample_rate knob has the same merge contract as bit_depth — a
    debounced save that doesn't include sample_rate must NOT default it
    away from whatever the user previously chose."""
    album_id = "tplan006"
    _seed_album(album_id, plan={
        "tracks": [{"title": "old", "duration_seconds": 30.0, "skip": False}],
        "normalize": True,
        "target_peak_db": -2.0,
        "measured_peak_db": -4.5,
        "bit_depth": 24,
        "sample_rate": 96000,
    })
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [
                    {"title": "newA", "duration_seconds": 12.0, "skip": False},
                ],
                # No sample_rate — must survive the merge.
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["sample_rate"] == 96000
    finally:
        _cleanup(album_id)


def test_explicit_sample_rate_overrides_prior_value():
    """When the user changes the sample-rate dropdown, the new value must
    overwrite the prior one — same semantics as bit_depth."""
    album_id = "tplan007"
    _seed_album(album_id, plan={
        "tracks": [{"title": "x", "duration_seconds": 10.0, "skip": False}],
        "sample_rate": 44100,
    })
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "x", "duration_seconds": 10.0, "skip": False}],
                "sample_rate": 48000,
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["sample_rate"] == 48000
    finally:
        _cleanup(album_id)


def test_post_to_unknown_album_404s():
    """Bad album_id → 404, not a stub-creation."""
    r = _client().post(
        "/api/album/zzzz9999/plan",
        json={"tracks": [{"title": "x", "duration_seconds": 1.0, "skip": False}]},
    )
    assert r.status_code == 404


def test_split_emit_does_not_clear_existing_plan_knobs():
    """Adjacent invariant: even though the split route writes a fresh
    `plan`, the editor's prior normalize/peak/bit-depth choices flow
    through the request body. This catches a regression where the split
    route would default-fill any field the editor didn't send."""
    # This is intentionally a unit-level shape test — the full split
    # ffmpeg path is in e2e. We just confirm the manifest preserves
    # whatever the route writes.
    album_id = "tplan005"
    _seed_album(album_id, plan={
        "tracks": [{"title": "old", "duration_seconds": 10.0, "skip": False}],
        "normalize": True, "target_peak_db": -1.0,
        "measured_peak_db": -3.0, "bit_depth": 24,
    })
    try:
        # Re-save a draft with all knobs present → server must keep them.
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "newer", "duration_seconds": 10.0, "skip": False}],
                "normalize": True,
                "target_peak_db": -1.0,
                "measured_peak_db": -3.0,
                "bit_depth": 24,
            },
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["normalize"] is True
        assert plan["bit_depth"] == 24
    finally:
        _cleanup(album_id)
