#!/usr/bin/env python3
"""measure_gap.py - measure the audio gap of an HX Stomp preset change from a
recording.

Record the Stomp's output over USB audio (any sample rate, mono or stereo)
while a note is held or a loop plays, and change preset in the middle. Then:

    python3 tools/research/measure_gap.py recording.wav [--threshold-db -40]
                                          [--window-ms 1] [--plot out.png]

The script follows the signal's envelope (RMS over short windows, in dB
relative to the level before the switch) and reports, for every dip below
the threshold that lasts longer than a few milliseconds:

  - fade-out: from the last window at full level to the first below threshold
  - gap:      how long the signal stays below the threshold
  - fade-in:  from the first window above threshold to the first back at
              full level (within 3 dB of the level before the switch)

A sustained source is needed: a held note decays, so keep the recording
short around the switch, or play a looper / a signal generator into the
input. With a steady tone the three numbers are what the firmware does; with
a guitar note the fade-out blends with the note's own decay.

Needs numpy. Reads 16/24/32-bit PCM and float WAV files.
"""
import argparse
import struct
import sys

import numpy as np


def read_wav(path):
    with open(path, 'rb') as f:
        data = f.read()
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise SystemExit(f'{path}: not a WAV file')
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack('<I', data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b'fmt ':
            fmt = struct.unpack('<HHIIHH', body[:16])
        elif cid == b'data':
            raw = body
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise SystemExit(f'{path}: no fmt or data chunk')
    tag, channels, rate, _, block_align, bits = fmt
    frames = len(raw) // block_align
    raw = raw[:frames * block_align]
    if tag == 3 or (tag == 0xFFFE and bits == 32 and False):
        x = np.frombuffer(raw, dtype='<f4').reshape(frames, channels).astype(np.float64)
    elif bits == 16:
        x = np.frombuffer(raw, dtype='<i2').reshape(frames, channels).astype(np.float64) / 32768.0
    elif bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(frames, channels, 3).astype(np.int32)
        v = b[:, :, 0] | (b[:, :, 1] << 8) | (b[:, :, 2] << 16)
        v = np.where(v & 0x800000, v - (1 << 24), v)
        x = v.astype(np.float64) / 8388608.0
    elif bits == 32:
        x = np.frombuffer(raw, dtype='<i4').reshape(frames, channels).astype(np.float64) / 2147483648.0
    else:
        raise SystemExit(f'{path}: unsupported sample format ({bits} bits, tag {tag})')
    return x, rate


def envelope_db(x, rate, window_ms):
    mono = x.mean(axis=1) if x.ndim == 2 else x
    n = max(1, int(rate * window_ms / 1000))
    frames = len(mono) // n
    w = mono[:frames * n].reshape(frames, n)
    rms = np.sqrt((w * w).mean(axis=1)) + 1e-12
    return 20 * np.log10(rms), n / rate


def find_gaps(env, step, threshold_db, min_gap_ms=3.0, pre_ms=100.0):
    """Dips below threshold_db (relative to the level just before them)."""
    out = []
    i = 0
    pre = max(1, int(pre_ms / 1000 / step))
    min_len = max(1, int(min_gap_ms / 1000 / step))
    while i < len(env):
        ref = np.median(env[max(0, i - pre):i]) if i else env[0]
        if env[i] < ref + threshold_db and i:
            j = i
            while j < len(env) and env[j] < ref + threshold_db:
                j += 1
            if j - i >= min_len:
                # fade-out: back from i to the last window within 3 dB of ref
                k = i
                while k > 0 and env[k - 1] < ref - 3:
                    k -= 1
                # fade-in: forward from j to the first window within 3 dB of the level after
                post = np.median(env[j + min_len:j + min_len + pre]) if j + min_len < len(env) else ref
                m = j
                while m < len(env) and env[m] < post - 3:
                    m += 1
                out.append({'fade_out_ms': (i - k) * step * 1000, 'gap_ms': (j - i) * step * 1000,
                            'fade_in_ms': (m - j) * step * 1000, 'at_s': k * step,
                            'level_before_db': ref, 'level_after_db': post, 'floor_db': env[i:j].min()})
            i = j
        else:
            i += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('wav')
    ap.add_argument('--threshold-db', type=float, default=-40.0, help='gap = envelope below this, relative to before (default -40)')
    ap.add_argument('--window-ms', type=float, default=1.0, help='envelope window (default 1 ms)')
    ap.add_argument('--plot', help='write a PNG of the envelope (needs matplotlib)')
    a = ap.parse_args()
    x, rate = read_wav(a.wav)
    env, step = envelope_db(x, rate, a.window_ms)
    gaps = find_gaps(env, step, a.threshold_db)
    print(f'{a.wav}: {len(x) / rate:.2f} s at {rate} Hz, {x.shape[1] if x.ndim == 2 else 1} channel(s), '
          f'envelope window {a.window_ms} ms, threshold {a.threshold_db} dB')
    if not gaps:
        print('no gap found (nothing below the threshold for more than 3 ms)')
    for g in gaps:
        print(f'  at {g["at_s"]:7.3f} s: fade-out {g["fade_out_ms"]:6.1f} ms, gap {g["gap_ms"]:6.1f} ms, '
              f'fade-in {g["fade_in_ms"]:6.1f} ms  (level {g["level_before_db"]:.1f} dB before, '
              f'{g["level_after_db"]:.1f} dB after, floor {g["floor_db"]:.1f} dB)')
    if a.plot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        t = np.arange(len(env)) * step
        plt.figure(figsize=(12, 4))
        plt.plot(t, env, lw=0.8)
        for g in gaps:
            plt.axvspan(g['at_s'], g['at_s'] + (g['fade_out_ms'] + g['gap_ms'] + g['fade_in_ms']) / 1000, color='red', alpha=0.2)
        plt.xlabel('s')
        plt.ylabel('dBFS (RMS)')
        plt.tight_layout()
        plt.savefig(a.plot, dpi=120)
        print(f'plot: {a.plot}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
