"""Upstream stream-format probing.

Cheap path: hit the Pi recorder's `/info` endpoint over HTTP. Fallback:
spawn ffprobe against the URL. Both return the same fmt dict shape so
callers can branch once and forget which path produced the answer.
"""
import json
import logging
import subprocess
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)


def probe_stream(url: str, timeout: float = 10.0) -> dict:
    """Run ffprobe against `url` and return {sample_rate, channels, codec,
    bit_depth}. Raises on failure with a user-facing message."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-i", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffprobe failed").strip()[:300])
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError("no streams reported by ffprobe")
    s = streams[0]
    bd = s.get("bits_per_sample") or 0
    return {
        "sample_rate": int(s.get("sample_rate") or 44100),
        "channels":    int(s.get("channels") or 2),
        "bit_depth":   int(bd) if bd else 16,
        "codec":       s.get("codec_name", ""),
    }


def _probe_via_pi_info(url: str, timeout: float = 2.0) -> dict:
    """Probe a Pi-recorder-style upstream by hitting its `/info` endpoint.

    Strips the path off `url` and asks for `<base>/info`; returns the
    same fmt dict shape as `probe_stream`. Much cheaper than spawning
    ffprobe (which itself opens a /stream connection on the Pi, kicking
    any in-flight consumer for a second). Caller is responsible for
    falling back to ffprobe on any failure here — we raise a plain
    RuntimeError (or let urllib's own errors propagate) so the fallback
    site can wrap with a single except.
    """
    # Build base = scheme://host[:port]. Drop path/query/fragment.
    from urllib.parse import urlparse, urlunparse
    parts = urlparse(url)
    if not parts.scheme or not parts.netloc:
        raise RuntimeError("not an http(s) URL")
    base = urlunparse((parts.scheme, parts.netloc, "", "", "", ""))
    info_url = base + "/info"
    req = urllib.request.Request(info_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) != 200:
            raise RuntimeError(f"/info returned HTTP {resp.status}")
        body = resp.read()
    info = json.loads(body)
    sample_rate = int(info["sample_rate"])
    channels    = int(info["channels"])
    bit_depth   = int(info["bit_depth"])
    # The Pi serves raw PCM little-endian; map bit depth → pcm_s{NN}le for
    # parity with what ffprobe would have returned (downstream consumers
    # only inspect codec for logging, but stay consistent).
    codec = "pcm_s24le" if bit_depth >= 24 else "pcm_s16le"
    return {
        "sample_rate": sample_rate,
        "channels":    channels,
        "bit_depth":   bit_depth,
        "codec":       codec,
    }


def _probe_format(url: str) -> dict:
    """Probe the upstream format. Tries the Pi's /info endpoint first (cheap,
    ~20 ms over LAN, doesn't kick the active /stream consumer); falls back
    to ffprobe on any failure — wrong host, missing endpoint, network error,
    JSON parse, missing keys, anything. Logs which path produced the result
    at debug level so a confused operator can grep for it."""
    try:
        fmt = _probe_via_pi_info(url)
        _log.debug("probe via /info succeeded for %s", url)
        return fmt
    except (urllib.error.URLError, OSError, RuntimeError, ValueError,
            KeyError, TypeError) as e:
        _log.debug("probe via /info failed for %s: %s — falling back to ffprobe",
                   url, e)
    # Fallback path. Let ffprobe's own RuntimeError surface to the caller
    # so the user-facing connect message stays informative.
    fmt = probe_stream(url)
    _log.debug("probe via ffprobe succeeded for %s", url)
    return fmt
