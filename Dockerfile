# Resolve the app version from .git in the build context. A throwaway stage
# keeps git out of the runtime image; falls back to "dev" if .git is missing
# (e.g. building from a release tarball).
FROM alpine/git AS version
WORKDIR /repo
# Copy the full build context so `git describe --dirty` reflects host-side
# uncommitted changes. core.autocrlf=input normalizes CRLF→LF when diffing,
# so Windows checkouts (which smudge LF→CRLF on the host) don't read as dirty
# inside the Linux build.
COPY . /repo
RUN (git update-index --refresh >/dev/null 2>&1 || true) \
    && git -c core.autocrlf=input describe --tags --always --dirty > /VERSION 2>/dev/null \
    || echo "dev" > /VERSION


# Build audiowaveform from source. We dropped the apk-add path because the
# package's availability across alpine versions is too brittle for CI:
# Alpine 3.23 (the current python:3.12-alpine target) doesn't carry it,
# 3.21 was inconsistent in CI, and pinning to an older base risks security
# updates lagging. Building from a pinned upstream tag instead keeps us
# decoupled from alpine's package timeline.
#
# The result is a single ~2 MB binary copied into the runtime stage; build
# tools (cmake, g++, boost-dev, …) stay in the builder layer and never ship.
FROM alpine:3.22 AS aw-builder
RUN apk add --no-cache \
        build-base cmake git \
        boost-dev libsndfile-dev gd-dev libid3tag-dev libmad-dev
WORKDIR /build
RUN git clone --depth=1 --branch=1.10.2 https://github.com/bbc/audiowaveform.git
WORKDIR /build/audiowaveform
RUN cmake -DENABLE_TESTS=0 -DBUILD_STATIC=0 . \
 && make -j"$(nproc)" \
 && strip audiowaveform


FROM python:3.14-alpine3.22

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Alpine's ffmpeg is built with --enable-libmp3lame and ships every filter
# the app uses (silencedetect, astats, aformat, volume, atrim, asetpts,
# concat). flac provides metaflac. chromaprint provides fpcalc for the
# AcoustID "identify by audio" flow (services/acoustid.py). The runtime
# libs match the dynamically-linked deps of the audiowaveform binary
# copied from the builder stage — omitting any of them would surface as a
# "library not found" at first invocation rather than at image build.
RUN apk add --no-cache \
        ffmpeg flac chromaprint \
        libstdc++ \
        boost-program_options boost-filesystem boost-regex \
        libsndfile gd libid3tag libmad \
        tini

COPY --from=aw-builder /build/audiowaveform/audiowaveform /usr/local/bin/audiowaveform
RUN audiowaveform --version
# Assert fpcalc landed with the chromaprint package — fail the build, not
# the first /api/identify call, if the package ever stops shipping it.
RUN fpcalc -version

# Install the `runtime` dependency group from pyproject.toml. Centralising
# the pin list in pyproject keeps Docker + CI on the same versions — bump
# once, both follow.
COPY pyproject.toml /tmp/pyproject.toml
RUN pip install --no-cache-dir --group /tmp/pyproject.toml:runtime \
 && rm /tmp/pyproject.toml

WORKDIR /app
COPY app/ /app/
# Pi capture service source — pushed to a Pi over SSH by the in-app
# "deploy to pi" button (see services/pi_deploy.py). Lives outside /app
# so the same files keep their canonical location both in the repo and
# inside the runtime image.
COPY pi/ /pi/
COPY CHANGELOG.md /app/CHANGELOG.md
COPY --from=version /VERSION /app/VERSION

RUN mkdir -p /output

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)" || exit 1

# tini as PID 1 forwards SIGTERM/SIGINT cleanly to Python and reaps any
# stray ffmpeg children that exit while Python is busy. Without it,
# Python-as-PID-1 leaves zombies until it gets around to wait()ing them.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "main.py"]
