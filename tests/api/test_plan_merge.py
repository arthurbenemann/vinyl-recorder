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


# ── Optimistic-concurrency tests ─────────────────────────────────────────
# `plan_version` lets two tabs editing the same album detect a stale write
# instead of silently clobbering each other. See `update_plan` in
# routes/albums.py for the contract.


def test_plan_post_returns_version_field():
    """Every successful plan POST returns the new `plan_version` so the
    client can update its optimistic-concurrency token."""
    album_id = "tver001"
    _seed_album(album_id)
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "x", "duration_seconds": 1.0, "skip": False}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert "plan_version" in body
        assert isinstance(body["plan_version"], int)
        assert body["plan_version"] >= 1
    finally:
        _cleanup(album_id)


def test_plan_post_increments_version_on_write():
    """Each successful write bumps plan_version by exactly 1. Two sequential
    saves go 0→1→2, matching the read-modify-write semantics the
    optimistic-concurrency check relies on."""
    album_id = "tver002"
    _seed_album(album_id)
    try:
        r1 = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "a", "duration_seconds": 1.0, "skip": False}]},
        )
        assert r1.status_code == 200
        v1 = r1.json()["plan_version"]
        r2 = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "b", "duration_seconds": 1.0, "skip": False}]},
        )
        assert r2.status_code == 200
        v2 = r2.json()["plan_version"]
        assert v2 == v1 + 1
    finally:
        _cleanup(album_id)


def test_plan_post_with_stale_expected_version_returns_409():
    """When the client sends an out-of-date `expected_version`, the server
    rejects with 409 and surfaces the current plan + version so the
    client can offer a "reload?" path. The user's stale local edits
    must NOT be persisted — assert the manifest's tracks are unchanged."""
    album_id = "tver003"
    _seed_album(album_id)
    try:
        # Save once to bump the version off the default 0.
        r1 = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "first", "duration_seconds": 1.0, "skip": False}]},
        )
        assert r1.status_code == 200
        current_version = r1.json()["plan_version"]
        # Tab B writes after tab A's snapshot — bumps to current_version + 1.
        r2 = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "tabB", "duration_seconds": 2.0, "skip": False}]},
        )
        assert r2.status_code == 200
        latest_version = r2.json()["plan_version"]
        # Tab A's debounced save fires with the STALE expected_version.
        r3 = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "tabA-stale", "duration_seconds": 3.0, "skip": False}],
                "expected_version": current_version,  # tab A's old snapshot
            },
        )
        assert r3.status_code == 409
        body = r3.json()
        # 409 body must surface the server's current state so the client
        # can repaint without a second round-trip.
        assert body["plan_version"] == latest_version
        assert body["plan"]["tracks"][0]["title"] == "tabB"
        # Tab A's stale write was REJECTED — the manifest still has tab B's
        # edits, not "tabA-stale". This is the whole point of the feature.
        plan_after = _read_plan(album_id)
        assert plan_after["tracks"][0]["title"] == "tabB"
    finally:
        _cleanup(album_id)


def test_plan_post_with_matching_expected_version_succeeds():
    """The happy path: client sends the version it loaded, server matches
    it, write goes through, response carries the bumped version."""
    album_id = "tver004"
    _seed_album(album_id)
    try:
        r1 = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "x", "duration_seconds": 1.0, "skip": False}]},
        )
        v1 = r1.json()["plan_version"]
        r2 = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "y", "duration_seconds": 1.0, "skip": False}],
                "expected_version": v1,
            },
        )
        assert r2.status_code == 200
        assert r2.json()["plan_version"] == v1 + 1
        plan = _read_plan(album_id)
        assert plan["tracks"][0]["title"] == "y"
    finally:
        _cleanup(album_id)


def test_plan_post_without_expected_version_writes_unconditionally():
    """Backward-compat: callers that don't track plan_version (legacy
    clients, integration scripts, the split route's own writes) keep
    working — the omitted field means "write blind", same as before
    this feature landed."""
    album_id = "tver005"
    _seed_album(album_id)
    try:
        # Bump the version a few times so it's clearly non-zero.
        for title in ("a", "b", "c"):
            _client().post(
                f"/api/album/{album_id}/plan",
                json={"tracks": [{"title": title, "duration_seconds": 1.0, "skip": False}]},
            )
        # No expected_version → must succeed regardless of the server's
        # current value.
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "unconditional", "duration_seconds": 5.0, "skip": False}]},
        )
        assert r.status_code == 200
        plan = _read_plan(album_id)
        assert plan["tracks"][0]["title"] == "unconditional"
    finally:
        _cleanup(album_id)


def test_plan_post_with_expected_version_zero_matches_legacy_albums():
    """A pre-existing album with no `plan_version` in its manifest reads
    back as 0. A client that loaded it (and got 0 from /tracks) should
    be able to save with expected_version=0 without a 409. This guards
    the migration path for albums that existed before the feature
    landed."""
    album_id = "tver006"
    # Seed an album with a manifest that pre-dates the plan_version field.
    from state import IN_PROGRESS_DIR
    d = IN_PROGRESS_DIR / album_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "side1.flac").write_bytes(b"")
    legacy_manifest = {
        "schema_version": 2,
        "tags": {"artist": "X", "album": "Y"},
        "sides": ["side1.flac"],
        "cover": None,
        "plan": None,
        "music_relpath": None,
        # No plan_version key at all — simulates a pre-feature album.
    }
    (d / "album.json").write_text(json.dumps(legacy_manifest))
    try:
        r = _client().post(
            f"/api/album/{album_id}/plan",
            json={
                "tracks": [{"title": "legacy-compat", "duration_seconds": 2.0, "skip": False}],
                "expected_version": 0,
            },
        )
        assert r.status_code == 200
        assert r.json()["plan_version"] == 1
    finally:
        _cleanup(album_id)


def test_tracks_endpoint_returns_plan_version():
    """The /tracks endpoint seeds the editor's planVersion at load time,
    so it has to surface the current value."""
    album_id = "tver007"
    _seed_album(album_id, plan={
        "tracks": [{"title": "saved", "duration_seconds": 10.0, "skip": False}],
    })
    try:
        # Bump the version via a plan POST so it's non-zero.
        _client().post(
            f"/api/album/{album_id}/plan",
            json={"tracks": [{"title": "saved", "duration_seconds": 10.0, "skip": False}]},
        )
        r = _client().get(f"/api/album/{album_id}/tracks")
        assert r.status_code == 200
        body = r.json()
        assert "plan_version" in body
        assert isinstance(body["plan_version"], int)
        assert body["plan_version"] >= 1
    finally:
        _cleanup(album_id)


def test_tracks_endpoint_returns_zero_version_for_empty_plan():
    """Albums with no plan yet still return plan_version=0 from /tracks
    so the editor's planVersion init has a sensible default."""
    album_id = "tver008"
    _seed_album(album_id, plan=None)
    try:
        r = _client().get(f"/api/album/{album_id}/tracks")
        assert r.status_code == 200
        body = r.json()
        assert body["plan"] is None
        assert body["plan_version"] == 0
    finally:
        _cleanup(album_id)
