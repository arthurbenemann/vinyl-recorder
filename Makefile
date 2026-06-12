# Default target — `make` brings up the dockerized recorder in the background.
.DEFAULT_GOAL := up

# Run each recipe in a single shell so the release flow can use normal shell
# control flow (multi-line if/case, $$variables) instead of \-joined one-liners.
.ONESHELL:

.PHONY: up test test-down test-logs test-rebuild release major minor patch

# Dev flow — base + dev overlay (build from source instead of pulling the
# published image).
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

up:
	$(COMPOSE_DEV) up --build -d

# Test-stream overlay — synthetic audio source so the UI can be exercised
# without a Pi. Stacks on top of the dev overlay so the recorder still builds
# from source. See test-streams/ + docker-compose.test.yml.
COMPOSE_TEST := $(COMPOSE_DEV) -f docker-compose.test.yml

test:
	$(COMPOSE_TEST) up --build -d

test-down:
	$(COMPOSE_TEST) down -v

test-logs:
	$(COMPOSE_TEST) logs -f --tail=50

test-rebuild:
	$(COMPOSE_TEST) up --build -d --force-recreate

# Bump-keyword no-op targets: let `make release minor` parse cleanly. Error if
# invoked alone so a stray `make patch` isn't silent.
major minor patch:
	@test -n "$(filter release,$(MAKECMDGOALS))" || { echo "use with 'make release $@'"; exit 1; }

# Cut a release. Either:
#   make release VERSION=v0.2.0   # explicit version
#   make release minor            # bump last tag (major / minor / patch)
#
# Updates CHANGELOG.md, commits it, tags THAT commit (so the tagged release is
# self-contained), pushes both, and publishes a GitHub Release. Requires
# `git-cliff` and `gh`.
release:
	@set -e
	command -v git-cliff >/dev/null || { echo "git-cliff not installed (https://git-cliff.org)"; exit 1; }
	command -v gh >/dev/null || { echo "gh not installed (https://cli.github.com)"; exit 1; }
	gh auth status >/dev/null 2>&1 || { echo "gh not authenticated; run 'gh auth login'"; exit 1; }
	bump="$(filter major minor patch,$(MAKECMDGOALS))"
	if [ -n "$(VERSION)" ]; then
	  new="$(VERSION)"
	elif [ -n "$$bump" ]; then
	  last=$$(git describe --tags --abbrev=0 2>/dev/null) || { echo "no previous tag; pass VERSION=v0.1.0 explicitly"; exit 1; }
	  s=$${last#v}
	  M=$${s%%.*}; r=$${s#*.}; m=$${r%%.*}; p=$${r#*.}
	  case "$$bump" in
	    major) new="v$$((M+1)).0.0" ;;
	    minor) new="v$$M.$$((m+1)).0" ;;
	    patch) new="v$$M.$$m.$$((p+1))" ;;
	  esac
	else
	  echo "Usage: make release VERSION=vX.Y.Z  |  make release {major|minor|patch}"
	  exit 1
	fi
	test -z "$$(git status --porcelain)" || { echo "working tree not clean"; exit 1; }
	test "$$(git rev-parse --abbrev-ref HEAD)" = "main" || { echo "not on main"; exit 1; }
	! git rev-parse "$$new" >/dev/null 2>&1 || { echo "tag $$new already exists"; exit 1; }
	echo "==> Releasing $$new"
	git cliff --tag "$$new" --unreleased --prepend CHANGELOG.md
	git add CHANGELOG.md
	git commit -m "Release $$new"
	git tag -a "$$new" -m "Release $$new"
	git push origin main --follow-tags
	gh release create "$$new" --notes "$$(git cliff --current)"
