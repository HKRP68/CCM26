"""Generate the default no-ball umpire-whistle sound.

The other gameplay sounds were supplied as mp3 files; no no-ball sound was
provided, so this script synthesizes one with the stdlib only (no ffmpeg on
the deploy hosts). Output is committed at static/cricket/sounds/no_ball.wav
and only needs regenerating if the sound design changes.

Usage: python tools/generate_noball_sound.py
"""
import math
import os
import struct
import wave

SAMPLE_RATE = 44100
DURATION = 0.45  # seconds
ATTACK = 0.010   # seconds
RELEASE = 0.080  # seconds
PEAK = 0.8       # of full scale

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "cricket", "sounds", "no_ball.wav",
)


def sample(t: float) -> float:
    # Pea-whistle model: two close partials trilled at ~38 Hz.
    tone = math.sin(2 * math.pi * 2200 * t) + 0.5 * math.sin(2 * math.pi * 2900 * t)
    trill = 0.55 + 0.45 * math.sin(2 * math.pi * 38 * t)
    if t < ATTACK:
        env = t / ATTACK
    elif t > DURATION - RELEASE:
        env = math.exp(-6.0 * (t - (DURATION - RELEASE)) / RELEASE)
    else:
        env = 1.0
    return (tone / 1.5) * trill * env * PEAK


def main() -> None:
    n_frames = int(SAMPLE_RATE * DURATION)
    frames = bytearray()
    for i in range(n_frames):
        v = sample(i / SAMPLE_RATE)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(OUT_PATH, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    print(f"Wrote {OUT_PATH} ({n_frames} frames, {DURATION}s)")


if __name__ == "__main__":
    main()
