"""Discogs public-API client. Used to enrich MusicBrainz releases with
vinyl-accurate label/catno/country/format/genres + cover image when MB has
linked the two — and, when DISCOGS_USERNAME is configured, to surface
matches from the user's owned collection alongside MB candidates.

Includes a small TTL cache keyed by release id, an inter-page sleep when
walking a collection (Discogs's unauthenticated bucket is 25 rpm), and
shared-token authentication for `release()` so the configured DISCOGS_TOKEN
actually reaches the wire."""
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Optional

from state import DISCOGS_BASE, DISCOGS_TOKEN, MB_UA


# Discogs's documented unauthenticated rate is 25 rpm; with a token it's 60.
# Pace pages of the collection walk so a large collection (~1000 releases)
# doesn't burst through the bucket. The token bumps the budget but a small
# inter-page pause is still polite.
_PAGE_SLEEP_S = 0.5

# Per-release short-lived cache. The "search → release detail → apply" flow
# can hit the same release id three times within a minute; this collapses
# that into one network call.
_RELEASE_CACHE_TTL_S = 300
_release_cache: dict[int, tuple[float, Optional[dict]]] = {}
_release_cache_lock = threading.Lock()


def _http_json_with_token(url: str, token: Optional[str], timeout: int = 20) -> dict:
    """Fetch JSON with optional Discogs auth header. Tokens raise rate
    limits and are required for private collections.

    On 429 (rate limit) we back off once and retry — Discogs uses 429
    rather than 503, so this is symmetric with the MB client's retry."""
    headers = {"User-Agent": MB_UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(2.0)
                continue
            raise


def release(release_id: int) -> Optional[dict]:
    """Fetch a Discogs release by ID. Uses the configured DISCOGS_TOKEN
    when available so the request lands in the higher rate-limit bucket
    instead of being unauthenticated. Cached for `_RELEASE_CACHE_TTL_S`."""
    now = time.monotonic()
    with _release_cache_lock:
        cached = _release_cache.get(release_id)
        if cached and (now - cached[0]) < _RELEASE_CACHE_TTL_S:
            return cached[1]
    try:
        data = _http_json_with_token(
            f"{DISCOGS_BASE}/releases/{release_id}",
            DISCOGS_TOKEN or None,
        )
    except Exception:
        data = None
    with _release_cache_lock:
        _release_cache[release_id] = (time.monotonic(), data)
    return data


def _clear_caches_for_tests() -> None:
    """Reset module-level state between tests."""
    with _release_cache_lock:
        _release_cache.clear()


# ── Collection ───────────────────────────────────────────────────────────
# In-memory cache keyed by (username,). Refreshes every CACHE_TTL_S unless
# the caller passes force=True (e.g. the /api/collection/refresh endpoint).
_CACHE_TTL_S = 3600
_collection_cache: dict[str, tuple[float, list[dict]]] = {}
_collection_lock = threading.Lock()


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
    Discogs outage doesn't break tagging. Pages are spaced by `_PAGE_SLEEP_S`
    to stay under Discogs's rate limits."""
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
        # Stay under Discogs's rate limit when paginating large collections.
        time.sleep(_PAGE_SLEEP_S)
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
