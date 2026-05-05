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
# the app uses (showwavespic, silencedetect, astats, aformat, volume, atrim,
# asetpts, concat). flac provides metaflac.
RUN apk add --no-cache ffmpeg flac

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

WORKDIR /app
COPY app/ /app/
COPY --from=version /VERSION /app/VERSION

RUN mkdir -p /output

EXPOSE 8080

CMD ["python3", "main.py"]
