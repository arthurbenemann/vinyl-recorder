"""Thin MusicBrainz / Cover Art Archive client. Sync HTTP via urllib so the
caller can run it on a thread (asyncio.to_thread) without pulling extra deps."""
import json
import re
import urllib.parse
import urllib.request
from typing import Optional

from state import CAA_BASE, MB_BASE, MB_UA


def _http_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _http_bytes(url: str, timeout: int = 20) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def search_releases(artist: str, album: str, limit: int = 5) -> list[dict]:
    """Return the top-N MusicBrainz release matches as compact dicts."""
    q_parts = []
    if artist: q_parts.append(f'artist:"{artist}"')
    if album:  q_parts.append(f'release:"{album}"')
    if not q_parts:
        return []
    q = " AND ".join(q_parts)
    data = _http_json(f"{MB_BASE}/release/?query={urllib.parse.quote(q)}&limit={limit}&fmt=json")
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


def release_full(mbid: str) -> dict:
    return _http_json(f"{MB_BASE}/release/{mbid}?inc=artist-credits+labels+recordings+url-rels+release-groups&fmt=json")


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
