FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg python3 \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /srv/streams

# Pre-render the test patterns to disk at build time. The runtime server
# replays them with `ffmpeg -re -stream_loop -1` so the consumer sees a
# real-time, infinitely-looping WAV stream — same shape the Pi exposes.
# All streams are 96 kHz / 24-bit stereo to match the Pi default and so
# the bit-depth / split features have realistic input.

# /loop is generated live by the runtime server (see server.py) using lavfi
# sines paced with -re — no pre-rendered file needed. Going via lavfi rather
# than `-stream_loop` over a fixed-length WAV avoids the burst that ffmpeg
# emits each time stream_loop wraps and resets input PTS, which broke the
# "record N seconds, expect N seconds of FLAC" e2e assertion.

# album.wav — fake vinyl side: lead-in (5s silence) + 4 tones of 30/25/30/25s
# separated by 2s gaps, with a 20s silence between tracks 2 and 3 emulating
# a side change. Total 144s. Each tone is a different frequency so split
# output can be auditioned. Drives the wave-editor's silence detection,
# the auto-skip-≥15s rule, and the multi-track split path.
RUN ffmpeg -y -hide_banner -loglevel error \
      -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=96000:duration=5" \
      -f lavfi -i "sine=frequency=440:sample_rate=96000:duration=30" \
      -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=96000:duration=2" \
      -f lavfi -i "sine=frequency=550:sample_rate=96000:duration=25" \
      -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=96000:duration=20" \
      -f lavfi -i "sine=frequency=660:sample_rate=96000:duration=30" \
      -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=96000:duration=2" \
      -f lavfi -i "sine=frequency=880:sample_rate=96000:duration=25" \
      -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=96000:duration=5" \
      -filter_complex "\
[1:a]pan=stereo|c0=c0|c1=c0,volume=3.16[t1];\
[3:a]pan=stereo|c0=c0|c1=c0,volume=3.16[t2];\
[5:a]pan=stereo|c0=c0|c1=c0,volume=3.16[t3];\
[7:a]pan=stereo|c0=c0|c1=c0,volume=3.16[t4];\
[0:a][t1][2:a][t2][4:a][t3][6:a][t4][8:a]concat=n=9:v=0:a=1[out]" \
      -map "[out]" -ac 2 -c:a pcm_s24le /srv/streams/album.wav

# clip.wav — 50s pattern: 20s clean sine at -8 dBFS, 5s clipped sine
# (volume 12.5 = +22 dB lifts the -18 dBFS sine well past 0 dBFS, so the
# pcm_s24le encoder saturates samples at +-max → flat-topped wave),
# then 25s clean again. Drives the upstream CLIP latch + log line + UI
# badge. The 20s lead-in gives a tab time to start a recording so the
# clip-during-recording path also gets exercised.
RUN ffmpeg -y -hide_banner -loglevel error \
      -f lavfi -i "sine=frequency=440:sample_rate=96000:duration=20" \
      -f lavfi -i "sine=frequency=440:sample_rate=96000:duration=5" \
      -f lavfi -i "sine=frequency=440:sample_rate=96000:duration=25" \
      -filter_complex "\
[0:a]pan=stereo|c0=c0|c1=c0,volume=3.16[c0];\
[1:a]pan=stereo|c0=c0|c1=c0,volume=12.5[c1];\
[2:a]pan=stereo|c0=c0|c1=c0,volume=3.16[c2];\
[c0][c1][c2]concat=n=3:v=0:a=1[out]" \
      -map "[out]" -ac 2 -c:a pcm_s24le /srv/streams/clip.wav

WORKDIR /srv
COPY server.py /srv/

EXPOSE 8090

CMD ["python3", "/srv/server.py"]
