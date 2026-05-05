"""Discogs public-API client. Used to enrich MusicBrainz releases with
vinyl-accurate label/catno/country/format/genres + cover image when MB has
linked the two — and, when DISCOGS_USERNAME is configured, to surface
matches from the user's owned collection alongside MB candidates."""
import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Optional

from services.musicbrainz import _http_json
from state import DISCOGS_BASE, MB_UA


def release(release_id: int) -> Optional[dict]:
    """Fetch a Discogs release by ID. Public endpoint, no auth required."""
    try:
        return _http_json(f"{DISCOGS_BASE}/releases/{release_id}")
    except Exception:
        return None


# ── Collection ───────────────────────────────────────────────────────────
# In-memory cache keyed by (username,). Refreshes every CACHE_TTL_S unless
# the caller passes force=True (e.g. the /api/collection/refresh endpoint).
_CACHE_TTL_S = 3600
_collection_cache: dict[str, tuple[float, list[dict]]] = {}
_collection_lock = threading.Lock()


def _http_json_with_token(url: str, token: Optional[str], timeout: int = 20) -> dict:
    """Like services.musicbrainz._http_json but adds a Discogs auth header
    when a token is supplied. Tokens raise rate limits and are required for
    private collections."""
    headers = {"User-Agent": MB_UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _summarize_release(item: dict) -> dict:
    """Flatten one Discogs collection item to the compact shape the UI uses.
    Most useful info is in basic_information; the outer wrapper just carries
    instance_id (useless to us) and per-collection metadata."""
    bi = item.get("basic_information") or {}
    artists = [a.get("name", "") for a in (bi.get("artists") or []) if a.get("name")]
    labels  = [(l.get("name") or "") for l in (bi.get("labels") or []) if l.get("name")]
    catnos  = [(l.get("catno") or "") for l in (bi.get("labels") or []) if l.get("catno")]
    formats = []
    for f in (bi.get("formats") or []):
        parts = [f.get("name", "")] + (f.get("descriptions") or [])
        s = ", ".join(p for p in parts if p)
        if s: formats.append(s)
    return {
        "discogs_release_id": int(bi.get("id") or item.get("id") or 0),
        "title":              bi.get("title", "") or "",
        "artist":             ", ".join(artists),
        "year":               str(bi.get("year") or "") if bi.get("year") else "",
        "labels":             labels,
        "label":              labels[0] if labels else "",
        "catno":              catnos[0] if catnos else "",
        "formats":            formats,
        "format":             formats[0] if formats else "",
        "cover_url":          bi.get("cover_image") or bi.get("thumb") or "",
    }


def collection_releases(username: str, token: Optional[str] = None,
                        force: bool = False) -> list[dict]:
    """Return the user's owned releases (folder 0 = "All").

    Cached in-process for CACHE_TTL_S. Pass force=True to refetch. Network
    failures fall back to whatever's cached (even stale) so a transient
    Discogs outage doesn't break tagging."""
    if not username:
        return []
    now = time.monotonic()
    with _collection_lock:
        cached = _collection_cache.get(username)
    if cached and not force and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    out: list[dict] = []
    page = 1
    while True:
        url = (f"{DISCOGS_BASE}/users/{urllib.parse.quote(username)}"
               f"/collection/folders/0/releases?per_page=100&page={page}")
        try:
            data = _http_json_with_token(url, token)
        except Exception:
            # Surface partial results if we got at least one page; otherwise
            # fall back to the cached snapshot (even if stale) so the user
            # isn't worse off than before the failure.
            if out:
                break
            if cached:
                return cached[1]
            return []
        for item in (data.get("releases") or []):
            out.append(_summarize_release(item))
        pagination = data.get("pagination") or {}
        if page >= int(pagination.get("pages") or 1):
            break
        page += 1
        if page > 50:
            # Safety stop — 50 × 100 = 5 000 releases is more than any sane
            # collection. Prevents a runaway loop on a Discogs API quirk.
            break
    with _collection_lock:
        _collection_cache[username] = (now, out)
    return out


def collection_count(username: str) -> int:
    """Cached size, or 0 if uncached."""
    with _collection_lock:
        cached = _collection_cache.get(username)
    return len(cached[1]) if cached else 0


# ── Fuzzy matching ───────────────────────────────────────────────────────
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(s: str) -> str:
    """Lowercase, strip diacritics, collapse non-alphanumerics. Used for
    fuzzy title/artist matching against the collection."""
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", s.lower()).strip()


def _score(query: str, candidate: str) -> float:
    """Symmetric similarity score in [0, 1]. Empty query → 0 (we never want
    to surface random matches when the user typed nothing)."""
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return 0.0
    return SequenceMatcher(None, q, c).ratio()


def match_collection(query_artist: str, query_album: str,
                     releases: list[dict], limit: int = 5,
                     min_score: float = 0.55) -> list[dict]:
    """Return the top-N collection releases that fuzzy-match the query.

    Score is the average of artist + album similarity. Releases below
    min_score are dropped. If query_artist or query_album is empty, scoring
    falls back to the available field. Returns the same shape as
    `_summarize_release`, with `score` (0..100) appended."""
    if not releases:
        return []
    if not (query_artist or query_album):
        return []
    scored: list[tuple[float, dict]] = []
    for rel in releases:
        s_art = _score(query_artist, rel.get("artist", "")) if query_artist else None
        s_alb = _score(query_album,  rel.get("title",  "")) if query_album  else None
        parts = [x for x in (s_art, s_alb) if x is not None]
        if not parts:
            continue
        s = sum(parts) / len(parts)
        if s >= min_score:
            scored.append((s, rel))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for s, rel in scored[:limit]:
        out.append({**rel, "score": int(round(s * 100)), "source": "collection"})
    return out
