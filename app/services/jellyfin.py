"""Best-effort Jellyfin library-scan trigger.

The split orchestrator drops finished tracks into the Jellyfin-shaped
`music/` tree, but Jellyfin only notices on its own periodic scan. When
`JELLYFIN_URL` + `JELLYFIN_API_KEY` are configured, every successful split
POSTs `/Library/Refresh` so the album is playable right away.

Sync HTTP via urllib (same no-deps pattern as services/musicbrainz.py),
fired from a daemon thread — a slow or down Jellyfin must never delay or
fail a finished split. The outcome is surfaced on the UI log via the
eventbus so the user can see whether the poke landed.
"""
import logging
import threading
import urllib.request

from services.eventbus import bus
from state import JELLYFIN_API_KEY, JELLYFIN_URL

logger = logging.getLogger("jellyfin")

_TIMEOUT_SECONDS = 10

if JELLYFIN_URL and not JELLYFIN_API_KEY:
    logger.warning(
        "JELLYFIN_URL is set but JELLYFIN_API_KEY is not — /Library/Refresh "
        "requires an API key (Jellyfin Dashboard → API Keys); the scan "
        "trigger is disabled until both are configured")


def enabled() -> bool:
    """Whether the scan trigger is configured. Both vars are required —
    `/Library/Refresh` rejects unauthenticated requests."""
    return bool(JELLYFIN_URL and JELLYFIN_API_KEY)


def trigger_library_scan() -> bool:
    """POST `/Library/Refresh` and report whether Jellyfin accepted it
    (the endpoint returns 204 No Content). Never raises — the split that
    invoked us already succeeded, so a Jellyfin hiccup is only worth a
    log line, not an error."""
    if not enabled():
        return False
    req = urllib.request.Request(
        JELLYFIN_URL + "/Library/Refresh",
        method="POST",
        data=b"",
        headers={"X-Emby-Token": JELLYFIN_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS):
            pass
    except Exception as e:
        logger.warning("Jellyfin library scan failed: %s", e)
        bus.log(f"Jellyfin library scan failed: {e}", level="err")
        return False
    logger.info("Jellyfin library scan triggered")
    bus.log("Jellyfin library scan triggered")
    return True


def trigger_library_scan_bg() -> None:
    """Fire-and-forget `trigger_library_scan` on a daemon thread so split
    completion never waits on Jellyfin. No-op when not configured."""
    if not enabled():
        return
    threading.Thread(target=trigger_library_scan,
                     name="jellyfin-scan", daemon=True).start()
