"""Unit tests for the MusicBrainz JSON helpers."""
from services.musicbrainz import extract_discogs_id


def test_extract_discogs_id_finds_release_url():
    # Standard MB release-rels shape: a list of typed relations.
    mb = {
        "relations": [
            {"type": "wikipedia",
             "url": {"resource": "https://en.wikipedia.org/wiki/Foo"}},
            {"type": "discogs",
             "url": {"resource": "https://www.discogs.com/release/12345"}},
        ]
    }
    assert extract_discogs_id(mb) == 12345


def test_extract_discogs_id_handles_locale_prefix_in_url():
    # Discogs URLs sometimes carry a locale segment (`/en/release/...`); the
    # regex must match either form.
    mb = {"relations": [{
        "type": "discogs",
        "url": {"resource": "https://www.discogs.com/en/release/987654"},
    }]}
    assert extract_discogs_id(mb) == 987654


def test_extract_discogs_id_ignores_master_releases():
    # Discogs distinguishes /master/ from /release/. Only release IDs are
    # actionable for our cover-art enrichment, so master URLs must be ignored.
    mb = {"relations": [{
        "type": "discogs",
        "url": {"resource": "https://www.discogs.com/master/42"},
    }]}
    assert extract_discogs_id(mb) is None


def test_extract_discogs_id_missing_relation_returns_none():
    assert extract_discogs_id({}) is None
    assert extract_discogs_id({"relations": []}) is None
    assert extract_discogs_id({"relations": [{"type": "wikipedia"}]}) is None


def test_extract_discogs_id_handles_null_url_field():
    # Real MB payloads occasionally carry `"url": null` for stale relations.
    mb = {"relations": [{"type": "discogs", "url": None}]}
    assert extract_discogs_id(mb) is None
