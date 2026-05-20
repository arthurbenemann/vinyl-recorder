"""Pins the pipeline-legibility chrome added to make the implicit
raw → album → music flow visible to new users.

Two surfaces are covered:

  * The one-line `.section-subtitle` under each Library section header
    (Raw / In-progress / Music) that names the section's role and the
    next action, plus the ① ② ③ sequence affordance in the headers.
  * The empty-state copy each section renders when it has no rows — it
    must name the next pipeline action ("Combine", "Split into tracks",
    etc.) rather than a bare "nothing here".

The subtitle assertions are data-independent (the markup is static), so
they always run. The empty-state assertions are conditional: the e2e
stack is session-scoped and other test files seed rows into it, so a
section may or may not be empty by the time this file runs. When a
section currently shows its `.empty-lib` placeholder we assert the copy;
when it's populated we skip that one section's check (and say so) rather
than flake on ordering.
"""
import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover — only without playwright
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


# Each section: the <details> id, the keywords its subtitle must mention
# (so a future copy edit can't silently drop the "what next" hint), and
# the keywords its empty-state copy must name when the section is empty.
SECTIONS = {
    "raw-section": {
        "subtitle_keywords": ["combine"],
        # Raw is the pipeline entry point: its empty copy points at how to
        # get a side IN (capture / drop), not at the next stage.
        "empty_keywords": ["capture", "output/raw/"],
    },
    "in-progress-section": {
        "subtitle_keywords": ["tag", "split"],
        "empty_keywords": ["combine"],
    },
    "music-section": {
        "subtitle_keywords": ["tagged"],
        "empty_keywords": ["split into tracks"],
    },
}


def _subtitle_text(page, section_id: str) -> str:
    """Text of the `.section-subtitle` directly under a section header."""
    loc = page.locator(f"#{section_id} > .section-subtitle").first
    return (loc.text_content() or "").strip()


def test_each_section_has_role_subtitle(stack, page):
    """Every Library section carries a non-empty `.section-subtitle`
    naming its role + next action. Asserted unconditionally — the markup
    is static, so this doesn't depend on what data the shared stack has
    accumulated."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    for section_id, spec in SECTIONS.items():
        text = _subtitle_text(page, section_id)
        assert text, f"#{section_id} has no .section-subtitle"
        lowered = text.lower()
        for kw in spec["subtitle_keywords"]:
            assert kw in lowered, (
                f"#{section_id} subtitle {text!r} is missing keyword {kw!r}"
            )


def test_section_headers_imply_pipeline_order(stack, page):
    """The headers carry a ① ② ③ sequence affordance so the raw → album
    → music order is obvious at a glance, and the Raw subtitle spells the
    full flow out."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    headers = {
        "raw-section": "①",
        "in-progress-section": "②",
        "music-section": "③",
    }
    for section_id, marker in headers.items():
        summary = (
            page.locator(f"#{section_id} > summary").first.text_content() or ""
        )
        assert marker in summary, (
            f"#{section_id} header {summary!r} missing step marker {marker!r}"
        )

    raw_subtitle = _subtitle_text(page, "raw-section")
    # The explicit flow reminder lives in the Raw subtitle.
    for marker in ("①", "②", "③"):
        assert marker in raw_subtitle, (
            f"Raw subtitle {raw_subtitle!r} missing flow marker {marker!r}"
        )


def test_empty_sections_name_the_next_action(stack, page):
    """When a section is empty its `.empty-lib` placeholder must name the
    next pipeline action, not just say "nothing here".

    Conditional per section: the shared session stack may already hold
    rows seeded by earlier files. A section is treated as empty only when
    its tbody shows exactly the placeholder row (`td.empty-lib`) and no
    real data rows. At least one section is asserted; if every section
    happens to be populated the test skips with an explanation."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    tbody_ids = {
        "raw-section": "lib-tbody",
        "in-progress-section": "albums-tbody",
        "music-section": "music-tbody",
    }

    asserted = 0
    skipped = []
    for section_id, tbody_id in tbody_ids.items():
        # The placeholder is a single <tr> whose only cell is td.empty-lib;
        # populated tables have data rows and no .empty-lib cell. Reading
        # both counts lets us tell "empty" from "populated" deterministically.
        info = page.evaluate(
            """
            (tbodyId) => {
                const tb = document.getElementById(tbodyId);
                if (!tb) return null;
                const emptyCell = tb.querySelector('td.empty-lib');
                return {
                    hasEmpty: !!emptyCell,
                    emptyText: emptyCell ? emptyCell.textContent.trim() : '',
                    rowCount: tb.querySelectorAll('tr').length,
                };
            }
            """,
            tbody_id,
        )
        assert info is not None, f"#{tbody_id} not found"
        # Empty == the placeholder cell is the only row showing.
        is_empty = info["hasEmpty"] and info["rowCount"] == 1
        if not is_empty:
            skipped.append(section_id)
            continue
        lowered = info["emptyText"].lower()
        # Generic "nothing here" copy without a next action is the
        # regression we're guarding against.
        assert "no " in lowered, (
            f"#{section_id} empty copy {info['emptyText']!r} looks wrong"
        )
        for kw in SECTIONS[section_id]["empty_keywords"]:
            assert kw in lowered, (
                f"#{section_id} empty-state copy {info['emptyText']!r} "
                f"does not name next action {kw!r}"
            )
        asserted += 1

    if asserted == 0:
        pytest.skip(
            "no Library section was empty in the shared stack "
            f"(populated: {skipped}); subtitle test covers the static copy"
        )
