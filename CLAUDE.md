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

When a UI change benefits from a visual preview, capture targeted
screenshots that show *what the PR changes*, then publish them on the
shared **`previews`** branch under a per-PR folder. Embed the raw URLs
in the PR body.

The canonical `images/library.png` / `images/album-combine.png` /
`images/split-editor.png` on `main` are refreshed automatically by
`.github/workflows/screenshots.yml` after merge — do not commit them to
the PR branch.

## Convention: one shared `previews` branch, per-PR folders
- A single long-lived branch named `previews`, orphan (no shared
  history with `main`) so it doesn't carry the entire repo tree.
- Each PR adds a folder at the branch root: `<short-slug>/`. The slug
  is a human-readable hint at the feature
  (e.g. `drag-split-markers`, `feat-14-side-indicators`). It's only
  read from the PR body, so it just needs to be unambiguous and not
  collide with another PR's folder.
- One PNG per state worth showing
  (`shift-drag-before.png`, `side-switches.png`, etc.) inside the
  folder. Don't reuse the canonical filenames (`library.png` etc.);
  those are reserved for the post-merge refresh on `main`.
- Reference each PNG from the PR body via the raw GitHub URL:
  `https://raw.githubusercontent.com/<owner>/<repo>/previews/<short-slug>/<file>.png`.
- The branch is append-only in practice — new PR folders fast-forward
  on top of the existing tip. The upstream proxy rejects force-pushes,
  so treat existing folders as immutable. If a PR's screenshot needs
  to be updated, push it under a versioned slug
  (`<short-slug>-v2/<file>.png`) rather than overwriting.

## How to capture
1. Bring up the dev+test stack: `make test` (or the explicit `docker
   compose … up --build -d` command above).
2. Seed demo data: `docker exec vinyl-recorder python /app/tools/seed_demo_data.py --output-dir /output`.
3. Drive the browser via Playwright. For the canonical 3 views,
   `python tools/screenshots.py --url http://127.0.0.1:8080
   --output-dir ./images` works; for feature-specific captures, write
   a small Playwright script that opens the editor, sets the state you
   want to show, and calls `page.screenshot()`. Capture at 1440×900,
   `device_scale_factor=2`, `reduced_motion=reduce`.
4. Land the captures on the `previews` branch via a worktree inside
   the repo (signing needs the worktree under the main repo path):
   ```
   git fetch origin previews
   git worktree add .previews-worktree origin/previews
   mkdir -p .previews-worktree/<short-slug>
   cp /tmp/shots/*.png .previews-worktree/<short-slug>/
   cd .previews-worktree
   git checkout -B previews
   git add <short-slug>/ && git commit -m "Add <short-slug> previews (PR #N)"
   git push origin previews
   ```
   (First-time setup: `git checkout --orphan previews && git rm -rf .`
   then commit an empty `.gitkeep` so the branch has a root tree, then
   push.)
5. Update the code PR body with the raw-URL embeds.

If Playwright Chromium isn't installed yet:
`pip install --group screenshots && playwright install --with-deps chromium`.
