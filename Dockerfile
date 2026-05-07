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


FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Alpine's ffmpeg is built with --enable-libmp3lame and ships every filter
# the app uses (silencedetect, astats, aformat, volume, atrim, asetpts,
# concat). flac provides metaflac. audiowaveform precomputes the 8-bit
# min/max peak data the wave editor renders client-side.
#
# audiowaveform lives in Alpine's community repo. python:3.x-alpine bases
# don't always include community in /etc/apk/repositories, so we add it
# explicitly using the matching alpine version detected from /etc/os-release
# — pulling from edge would risk mixing musl/libstdc++ versions across the
# rest of the image.
# Try the configured repos first; if the package isn't there, append the
# matching alpine community URL and retry. The diagnostic prints land in
# the build log so a future failure surfaces /etc/apk/repositories and
# /etc/os-release contents inline. Comments inside the RUN are forbidden
# (Docker joins line-continued shells; a # mid-command swallows the rest).
RUN set -eux \
 && apk add --no-cache ffmpeg flac \
 && cat /etc/os-release \
 && cat /etc/apk/repositories \
 && if ! apk add --no-cache audiowaveform; then \
        echo "primary install failed; appending matching community repo"; \
        . /etc/os-release; \
        ALPINE_VER=$(echo "$VERSION_ID" | cut -d. -f1-2); \
        echo "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER}/community" \
            >> /etc/apk/repositories; \
        apk update; \
        apk add --no-cache audiowaveform; \
    fi \
 && audiowaveform --version

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

WORKDIR /app
COPY app/ /app/
COPY --from=version /VERSION /app/VERSION

RUN mkdir -p /output

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)" || exit 1

CMD ["python3", "main.py"]
