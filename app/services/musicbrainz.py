"""Thin MusicBrainz / Cover Art Archive client. Sync HTTP via urllib so the
caller can run it on a thread (asyncio.to_thread) without pulling extra deps.

Includes a pacing gate (MusicBrainz asks for ≤1 rps per User-Agent) and a
small short-lived cache for `release_full` so the typical "search → click →
apply" round trip doesn't hit MB three times for the same MBID."""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from state import CAA_BASE, MB_BASE, MB_UA


# MusicBrainz documents 1 request/sec/UA as the sustained rate. We pace just
# under that and serialize requests through `_PACE_LOCK` so concurrent threads
# don't race past the gate. CAA shares the gate because it lives at the same
# infra; that's stricter than required but keeps a single place to tune.
_MB_RATE_INTERVAL = 1.05
_PACE_LOCK = threading.Lock()
_PACE_LAST_REQ = 0.0


def _pace() -> None:
    """Block until at least `_MB_RATE_INTERVAL` has elapsed since the last
    request, then mark the new request time. Holding the lock for the sleep
    ensures concurrent callers serialize — the only way to honor MB's
    "1 rps PER UA" rule in a multi-threaded app."""
    global _PACE_LAST_REQ
    with _PACE_LOCK:
        now = time.monotonic()
        wait = _MB_RATE_INTERVAL - (now - _PACE_LAST_REQ)
        if wait > 0:
            time.sleep(wait)
        _PACE_LAST_REQ = time.monotonic()


def _open_with_retry(req: urllib.request.Request, timeout: int):
    """Pace + open + retry once on 503/429 with a 2 s backoff. MusicBrainz
    returns 503 when its capacity gate trips and 429 if you exceed the
    documented rate; both are transient."""
    for attempt in (0, 1):
        _pace()
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt == 0:
                time.sleep(2.0)
                continue
            raise


def _http_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA, "Accept": "application/json"})
    with _open_with_retry(req, timeout) as r:
        return json.loads(r.read())


def _http_bytes(url: str, timeout: int = 20) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
    try:
        with _open_with_retry(req, timeout) as r:
            return r.read()
    except Exception:
        return None


# ── Lucene escape ─────────────────────────────────────────────────────────
# MB's search endpoint runs Lucene under the hood. The user-supplied artist /
# album fields land inside a quoted phrase, so without escaping a stray `"`
# breaks the phrase, and `\` corrupts the next char. The full reserved set
# from Lucene's docs is also escaped defensively; some have no effect inside
# a quoted phrase but cost nothing to escape.
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/])')


def _lucene_escape(s: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", s)


def search_releases(artist: str = "", album: str = "", limit: int = 5,
                    *, q: str = "") -> list[dict]:
    """Return the top-N MusicBrainz release matches as compact dicts.

    Two query modes:
      - structured: `artist`/`album` map to `artist:"…"` / `release:"…"`
        Lucene clauses ANDed together (precise when both are known).
      - generic: a free-text `q` is passed as a bare Lucene query so MB's
        indexer scores across all release fields. Used for the UI search
        bars, where the user might type just an album title, an
        artist+album mash-up, a catalog number, etc.
    """
    if q:
        s = q.strip()
        if not s:
            return []
        query = _lucene_escape(s)
    else:
        q_parts = []
        if artist: q_parts.append(f'artist:"{_lucene_escape(artist)}"')
        if album:  q_parts.append(f'release:"{_lucene_escape(album)}"')
        if not q_parts:
            return []
        query = " AND ".join(q_parts)
    data = _http_json(f"{MB_BASE}/release/?query={urllib.parse.quote(query)}&limit={limit}&fmt=json")
    out = []
    for r in (data.get("releases") or [])[:limit]:
        label = ""
        li = r.get("label-info") or []
        if li:
            label = (li[0].get("label") or {}).get("name", "") or ""
        catno = ""
        if li:
            catno = li[0].get("catalog-number", "") or ""
        out.append({
            "mbid":           r["id"],
            "title":          r.get("title", ""),
            "artist":         (r.get("artist-credit") or [{}])[0].get("name", ""),
            "year":           (r.get("date") or "")[:4],
            "label":          label,
            "catalog_number": catno,
            "country":        r.get("country", "") or "",
            "format":         (r.get("media") or [{}])[0].get("format", "") or "",
            "score":          r.get("score"),
        })
    return out


# ── release_full cache ────────────────────────────────────────────────────
# MB releases are very nearly immutable from our point of view; caching the
# full record for a few minutes lets the typical "search → /api/release/{mbid}
# → /api/apply → cover" sequence hit MB once instead of three times.
_RELEASE_CACHE_TTL_S = 300
_release_cache: dict[str, tuple[float, dict]] = {}
_release_cache_lock = threading.Lock()


def release_full(mbid: str) -> dict:
    now = time.monotonic()
    with _release_cache_lock:
        cached = _release_cache.get(mbid)
        if cached and (now - cached[0]) < _RELEASE_CACHE_TTL_S:
            return cached[1]
    # `artist-rels` pulls release-level artist relations like "conductor" so
    # the tag panel can surface them on classical/jazz pressings without an
    # extra round trip.
    data = _http_json(
        f"{MB_BASE}/release/{mbid}"
        f"?inc=artist-credits+labels+recordings+url-rels+release-groups+artist-rels&fmt=json"
    )
    with _release_cache_lock:
        _release_cache[mbid] = (time.monotonic(), data)
    return data


def _clear_caches_for_tests() -> None:
    """Reset module-level state between tests so cache hits don't suppress
    HTTP calls the test is asserting on. Not used at runtime."""
    global _PACE_LAST_REQ
    with _release_cache_lock:
        _release_cache.clear()
    with _PACE_LOCK:
        _PACE_LAST_REQ = 0.0


def extract_discogs_id(mb_release: dict) -> Optional[int]:
    """Pull the Discogs release ID from MB url-rels, if curators have linked it."""
    for rel in mb_release.get("relations", []) or []:
        if rel.get("type") != "discogs":
            continue
        url = (rel.get("url") or {}).get("resource", "") or ""
        m = re.search(r"discogs\.com/(?:[^/]+/)?release/(\d+)", url)
        if m:
            return int(m.group(1))
    return None


def caa_front(mbid: str) -> Optional[bytes]:
    return _http_bytes(f"{CAA_BASE}/release/{mbid}/front-500")
