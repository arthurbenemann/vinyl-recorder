"""Unit tests for the per-source composer/conductor extractors used by
the tag panel. Both functions are pure dict→string helpers; the tests
assert prefix-matching, joining, dedupe, and the empty cases."""
from routes.tagging import (
    _discogs_extra_artists, _mb_artist_relation, _mb_extra_tags,
)


def test_mb_extra_tags_pulls_ids_media_releasetype():
    mb = {
        "release-group": {"id": "rg-123", "primary-type": "Album"},
        "artist-credit": [{"artist": {"id": "art-456", "name": "X"}}],
        "media": [{"format": "Vinyl"}],
    }
    out = _mb_extra_tags(mb)
    assert out["musicbrainz_releasegroupid"] == "rg-123"
    assert out["releasetype"] == "Album"
    # albumartistid mirrors artistid (single album-artist credit).
    assert out["musicbrainz_artistid"] == "art-456"
    assert out["musicbrainz_albumartistid"] == "art-456"
    assert out["media"] == "Vinyl"


def test_mb_extra_tags_omits_missing_fields():
    # Empty release → no keys at all (never write blank tags).
    assert _mb_extra_tags({}) == {}
    # Partial: only what's present.
    assert _mb_extra_tags({"media": [{"format": "Vinyl"}]}) == {"media": "Vinyl"}


def test_mb_artist_relation_picks_named_role():
    mb = {"relations": [
        {"type": "conductor", "artist": {"name": "Herbert von Karajan"}},
        {"type": "discogs",   "url": {"resource": "https://discogs.com/release/1"}},
    ]}
    assert _mb_artist_relation(mb, "conductor") == "Herbert von Karajan"


def test_mb_artist_relation_joins_multiple():
    mb = {"relations": [
        {"type": "conductor", "artist": {"name": "A"}},
        {"type": "conductor", "artist": {"name": "B"}},
    ]}
    assert _mb_artist_relation(mb, "conductor") == "A, B"


def test_mb_artist_relation_dedupes():
    mb = {"relations": [
        {"type": "conductor", "artist": {"name": "A"}},
        {"type": "conductor", "artist": {"name": "A"}},
    ]}
    assert _mb_artist_relation(mb, "conductor") == "A"


def test_mb_artist_relation_returns_empty_when_no_match():
    assert _mb_artist_relation({"relations": []}, "conductor") == ""
    assert _mb_artist_relation({}, "conductor") == ""


def test_mb_artist_relation_is_case_insensitive_on_type():
    mb = {"relations": [{"type": "Conductor", "artist": {"name": "A"}}]}
    assert _mb_artist_relation(mb, "conductor") == "A"


def test_discogs_extra_artists_prefix_matches_composed_by():
    d = {"extraartists": [
        {"role": "Composed By", "name": "Beethoven"},
        {"role": "Cover [Photography]", "name": "Photographer Name"},
    ]}
    assert _discogs_extra_artists(d, ("composed by", "composer")) == "Beethoven"


def test_discogs_extra_artists_handles_bracketed_qualifier():
    # "Composed By [Original Music]" is real on Discogs — prefix must catch it.
    d = {"extraartists": [
        {"role": "Composed By [Original Music]", "name": "X"},
    ]}
    assert _discogs_extra_artists(d, ("composed by", "composer")) == "X"


def test_discogs_extra_artists_dedupes_and_joins():
    d = {"extraartists": [
        {"role": "Conductor", "name": "A"},
        {"role": "Conductor", "name": "B"},
        {"role": "Conductor", "name": "A"},
    ]}
    assert _discogs_extra_artists(d, ("conductor",)) == "A, B"


def test_discogs_extra_artists_empty_cases():
    assert _discogs_extra_artists({"extraartists": []}, ("conductor",)) == ""
    assert _discogs_extra_artists({}, ("conductor",)) == ""
