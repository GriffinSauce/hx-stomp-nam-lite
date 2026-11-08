#!/usr/bin/env python3
"""gapfill.py - reference model and simulator test for gapfill_kernel.asm, the
ping-pong loop that fills the HX Stomp's preset-change gap.

usage: gapfill.py [--wav DIR] [--gap-ms 80] [--ramp-ms 7.5] [--signal sine|pluck|chord|silence|all]

Models a preset change the way the firmware does it (research/
gapless-preset-switch-findings.md): the graph's output fades out through the
stock one-pole mute ramp, the DSP is held (command 0x0f), the graph is torn
down and rebuilt (its blocks are skipped: the codec buffer keeps a stale
block), the hold ends and the new preset's output fades in. The kernel runs
on every block, in the simulator, on a TDM block of 8 slots as the codec
buffer holds it, and is compared with GapFill (the same algorithm in
float32) word for word.

Reports the largest jump between consecutive output samples with and without
the fill, and with --wav writes before/after .wav files to listen to.
"""
import argparse
import math
import os
import struct
import sys

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import sasm  # noqa: E402
import ssim  # noqa: E402

FS = 48000
BLOCK = 16
SLOTS = 8
N = 8192
RING_L, RING_R = 0xEC7000, 0xEC9000
G = 0xEC6F00
(G_MODE, G_HOLD, G_RAN, G_SLOT_L, G_SLOT_R, G_GAIN, G_RAMP, G_TARGET,
 G_W, G_P, G_DIR, G_S, G_LEN, G_THRESH, G_MARGIN) = range(G, G + 15)
f32 = np.float32


def ramp_step(ramp_ms):
    """Per-sample step of a linear ramp over ramp_ms (the stock mute: 7.5 ms, measured)."""
    return f32(1.0 / (ramp_ms / 1000 * FS))


def q31(x):
    """float -> Q31 as the output primitive does it: clip, round to nearest, bit 0 clear."""
    lim = float(ssim.b2f(0x3F7FFFFE))                   # the output primitive's clip
    v = int(np.rint(f32(min(max(x, -lim), lim)) * f32(2 ** 31)))
    v = max(-2 ** 31, min(2 ** 31 - 1, v))
    return (v & 0xFFFFFFFE) & 0xFFFFFFFF


def from_q31(w):
    v = w - (1 << 32) if w & 0x80000000 else w
    return f32(v) * f32(2.0 ** -31)


class GapFill:
    """The kernel's algorithm in float32, one block at a time."""

    def __init__(self, length=3072, step=None, slot_l=0, slot_r=1, margin=528):
        self.mode, self.gain, self.target = 0, f32(0), f32(0)
        self.step = f32(step if step is not None else ramp_step(7.5))
        self.ring_l, self.ring_r = np.zeros(N, np.float32), np.zeros(N, np.float32)
        self.w, self.p, self.dir, self.s, self.length = 0, 0, 1, 0, length
        self.margin = margin                                   # frames the stock mute faded before the hold
        self.slot_l, self.slot_r = slot_l, slot_r
        self.thresh = f32(0.001)

    def block(self, buf, hold, ran):
        """buf: 16*8 Q31 words, changed in place."""
        if hold and self.mode != 1:
            self.mode, self.target = 1, f32(1)
            self.p, self.s, self.dir = (self.w - self.length - self.margin) % N, 0, 1
        elif not hold and self.mode == 1:
            self.mode, self.target = 2, f32(0)
        if self.mode == 0:
            if ran:
                for k in range(BLOCK):
                    self.ring_l[self.w] = from_q31(buf[k * SLOTS + self.slot_l])
                    self.ring_r[self.w] = from_q31(buf[k * SLOTS + self.slot_r])
                    self.w = (self.w + 1) % N
            return
        live = f32(1 if ran else 0)
        for k in range(BLOCK):
            d = f32(self.target - self.gain)
            self.gain = f32(self.gain + f32(max(-self.step, min(self.step, d))))
            xl, xr = self.ring_l[self.p], self.ring_r[self.p]
            if self.s == self.length - 1:
                self.dir = -1
            if self.s == 0:
                self.dir = 1
            self.p = (self.p + self.dir) % N
            self.s += self.dir
            for slot, x in ((self.slot_l, xl), (self.slot_r, xr)):
                i = k * SLOTS + slot
                y = f32(f32(from_q31(buf[i]) * live) + f32(x * self.gain))
                buf[i] = q31(y) | (buf[i] & 1)
        if self.mode == 2 and self.gain < self.thresh:
            self.mode, self.gain = 0, f32(0)


# ------------------------------------------------------------------ simulator
def load_kernel():
    src = open(os.path.join(here, 'gapfill_kernel.asm')).read()
    secs, syms = sasm.assemble(src, 'gapfill_kernel.asm')
    sim = ssim.Sim()
    sim.strict_cbufen = True
    sim.load(secs)
    return sim, syms, sum(len(s.words) for s in secs if s.kind == 'pm')


def sim_block(sim, syms, buf, hold, ran, base=0xB3000):
    for k, w in enumerate(buf):
        sim.wr(base + k, w)
    sim.wr(G_HOLD, 1 if hold else 0)
    sim.wr(G_RAN, 1 if ran else 0)
    sim.set(0x11, base)                                   # I1
    before = {c: sim.get(c) for c in range(0x10, 0x50) if c != 0x11}
    mode1 = sim.u[ssim.U_MODE1]
    cycles = sim.call(syms['GAPFILL_ENTRY'])
    bad = [f'{c:#x}' for c, v in before.items() if sim.get(c) != v]
    if sim.u[ssim.U_MODE1] != mode1:
        bad.append('MODE1')
    if bad:
        sys.exit(f'registers changed: {bad}')
    return [sim.rd(base + k) for k in range(len(buf))], cycles


# ------------------------------------------------------------------ the scenario
def signals(seconds):
    n = int(FS * seconds)
    t = np.arange(n) / FS
    out = {
        'sine': 0.5 * np.sin(2 * np.pi * 220 * t),
        'pluck': sum(0.4 / (h * h) * np.sin(2 * np.pi * 196 * h * t + h) for h in range(1, 6)) * np.exp(-t / 0.6),
        'chord': 0.2 * (np.sin(2 * np.pi * 196.0 * t) + np.sin(2 * np.pi * 246.9 * t) + np.sin(2 * np.pi * 293.7 * t)),
        'silence': np.zeros(n),
    }
    return out


def scenario(x, gap_ms, switch_s=0.25, arm_sleep_ms=11.0, ramp_ms=7.5, new_phase=False):
    """What the codec gets per block, stock: (live samples, hold, ran) per block.
    The ARM mutes at switch_s, sleeps 11 ms, holds and tears the graph down;
    gap_ms later it rebuilds, releases the hold and unmutes. The mute is a
    linear ramp of ramp_ms on the live signal (as measured on the unit); while
    the graph is torn down the buffer keeps the last block it wrote."""
    n = len(x)
    y = x.copy()
    if new_phase:                                          # the new preset: same source, other phase
        y[int(FS * switch_s):] = np.roll(x, 777)[int(FS * switch_s):]
    g, target = 1.0, 1.0
    step = float(ramp_step(ramp_ms))
    t_mute = int(FS * switch_s)
    t_hold = t_mute + int(FS * arm_sleep_ms / 1000)
    t_release = t_hold + int(FS * gap_ms / 1000)
    blocks = []
    last = np.zeros(BLOCK)
    for b in range(n // BLOCK):
        i0 = b * BLOCK
        hold = t_hold <= i0 < t_release
        ran = not hold or i0 < t_hold + BLOCK              # the first held block still runs
        ran = ran and not (t_hold <= i0 < t_release)
        out = np.zeros(BLOCK)
        for k in range(BLOCK):
            i = i0 + k
            if i == t_mute:
                target = 0.0
            if i == t_release:
                target = 1.0
            g = g + max(-step, min(step, target - g))
            out[k] = y[i] * g
        if ran:
            last = out
            blocks.append((out, hold, True))
        else:
            blocks.append((last.copy(), hold, False))      # stale: the buffer is not rewritten
    return blocks


def max_jump(samples):
    s = np.asarray(samples)
    return float(np.max(np.abs(np.diff(s)))) if len(s) > 1 else 0.0


def write_wav(path, left, right):
    data = np.stack([left, right], axis=1)
    pcm = np.clip(np.rint(data * 32767), -32768, 32767).astype('<i2').tobytes()
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 2, FS, FS * 4, 4, 16))
        f.write(b'data' + struct.pack('<I', len(pcm)) + pcm)


def run(name, x, gap_ms, wav_dir, ramp_ms=7.5, check=True):
    blocks = scenario(x, gap_ms, ramp_ms=ramp_ms)
    step = ramp_step(ramp_ms)
    ref = GapFill(step=step)
    sim, syms, code = load_kernel()
    sim.wr(G_RAMP, ssim.f2b(float(step)))                 # the fill's ramp matches the mute's
    stock, filled, worst, mismatches = [], [], 0, 0
    for out, hold, ran in blocks:
        buf = [0] * (BLOCK * SLOTS)
        for k in range(BLOCK):
            buf[k * SLOTS + 0] = q31(out[k])
            buf[k * SLOTS + 1] = q31(out[k]) | 1          # right: the flag bit set, as a check
        stock += [from_q31(w) for w in buf[0::SLOTS]]
        rbuf = list(buf)
        ref.block(rbuf, hold, ran)
        sbuf, cycles = sim_block(sim, syms, list(buf), hold, ran)
        worst = max(worst, cycles)
        if check and sbuf != rbuf:
            mismatches += 1
        filled += [from_q31(w) for w in sbuf[0::SLOTS]]
        if any((w & 1) != (o & 1) for w, o in zip(sbuf, buf)):
            sys.exit(f'{name}: the flag bit was not preserved')
    stock, filled = np.array(stock), np.array(filled)
    print(f'{name:8s} gap {gap_ms:3d} ms, ramp {ramp_ms} ms: largest sample jump stock {max_jump(stock):.4f}, '
          f'filled {max_jump(filled):.4f}; {mismatches} blocks differ from the reference; '
          f'{worst} cycles per block at most; {code} instructions')
    if wav_dir:
        os.makedirs(wav_dir, exist_ok=True)
        write_wav(os.path.join(wav_dir, f'{name}_{gap_ms}ms_before.wav'), stock, stock)
        write_wav(os.path.join(wav_dir, f'{name}_{gap_ms}ms_after.wav'), filled, filled)
    return mismatches


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--wav', help='directory for before/after .wav files')
    ap.add_argument('--gap-ms', type=int, default=80)
    ap.add_argument('--signal', default='all')
    ap.add_argument('--seconds', type=float, default=0.7)
    ap.add_argument('--ramp-ms', type=float, default=7.5, help='the stock mute ramp (muteRampTimeMS)')
    a = ap.parse_args()
    sigs = signals(a.seconds)
    names = list(sigs) if a.signal == 'all' else [a.signal]
    bad = 0
    for name in names:
        bad += run(name, sigs[name], a.gap_ms, a.wav, a.ramp_ms)
    if bad:
        sys.exit(f'{bad} blocks differ between the kernel and the reference')
    print('kernel and reference agree on every block')


if __name__ == '__main__':
    main()
