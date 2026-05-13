# Shift-drag on split markers — UI previews

Detached screenshot branch for PR #13 (`feat(wave-editor): shift-drag a
cut to also shift later cuts`). Captured headlessly against a local
uvicorn at 1440 × 900, device-scale 2.

The trailing-cut markers are visually thickened in both frames as an
annotation aid — the real handles are 2 px wide, which doesn't read on
a screenshot. The cuts the user grabs are real; the highlight + chip
are injected for the preview only.

| state | file |
|-------|------|
| three cuts in place; middle handle shows the new shift-drag hint, trailing cuts highlighted as "what moves" | `shift-drag-before.png` |
| after a shift-drag on the middle cut: lead and trailing cut both shifted by +8 s, gap to cut 1 preserved | `shift-drag-after.png` |

This branch shares no history with `main` — the `images/*.png` refresh
workflow doesn't touch it.
