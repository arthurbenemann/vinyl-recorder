"""Discogs public-API client. Used to enrich MusicBrainz releases with
vinyl-accurate label/catno/country/format/genres + cover image when MB has
linked the two."""
from typing import Optional

from services.musicbrainz import _http_json
from state import DISCOGS_BASE


def release(release_id: int) -> Optional[dict]:
    """Fetch a Discogs release by ID. Public endpoint, no auth required."""
    try:
        return _http_json(f"{DISCOGS_BASE}/releases/{release_id}")
    except Exception:
        return None
