# Testing capabilities

The WebUI is end-to-end testable from this repo — do not assume browser
behavior is unverifiable.

## Playwright e2e suite
- Location: `tests/e2e/` (`test_recordings_ui.py`, `test_frontend.py`,
  `test_wave_editor.py`, plus `conftest.py`)
- Drives the live UI at http://localhost:8080
- Use these tests to validate frontend refactors, not just `pytest` on
  Python code

## Fake-stream test fixtures
- `docker-compose.test.yml` overlays `test-streams` (a synthetic audio
  source container) on top of dev compose
- The recorder is pointed at `http://test-streams:8090/loop` via
  `DEFAULT_STREAM_URL`, with `AUTO_CONNECT=true` and
  `PRE_ROLL_SECONDS=0` so timing assertions are reproducible
- Available stream paths from `test-streams`:
  - `/loop` — continuous 440/660 Hz tones (~-8 dBFS); steady VU, basic
    recording, multi-tab sync
  - `/album` — 4 tones, 2 s gaps, 20 s side-break; wave-editor split,
    silence detection, auto-skip
  - `/clip` — clipping content for clip-indicator tests

## How to run
- `make test` — full stack (compose up, run tests, tear down)
- For exploratory runs: `docker compose -f docker-compose.yml -f
  docker-compose.dev.yml -f docker-compose.test.yml up --build -d`,
  then drive Playwright manually or open the UI at localhost:8080

# Screenshots for UI PRs

When a UI change needs a visual reference, capture the
`images/split-editor.png` / `images/library.png` / `images/album-combine.png`
PNGs with Playwright via `tools/screenshots.py` — do NOT add ad-hoc
screenshot files.

## Convention: dedicated screenshot branch
- Code PR branches must stay free of binary PNG churn (those branches
  three-way-merge badly).
- Generate screenshots on a sibling branch named `screenshots/<pr-branch>`
  branched from the PR branch tip. Commit only `images/*.png` there.
- Reference the screenshot branch (or upload the PNGs as a comment / PR
  attachment) from the code PR — don't merge the screenshot branch into
  the code branch.
- The `Screenshots` workflow (`.github/workflows/screenshots.yml`) handles
  the post-merge refresh on `main` automatically; the dedicated branch is
  only for pre-merge review preview.

## How to capture
1. Bring up the dev+test stack: `make test` (or the explicit `docker
   compose … up --build -d` command above).
2. Seed demo data: `docker exec vinyl-recorder python /app/tools/seed_demo_data.py --output-dir /output`.
3. Run the capture: `python tools/screenshots.py --url http://127.0.0.1:8080 --output-dir ./images`.
4. Commit `images/*.png` to the `screenshots/<pr-branch>` branch and push.

If Playwright Chromium isn't installed yet:
`pip install --group screenshots && playwright install --with-deps chromium`.
