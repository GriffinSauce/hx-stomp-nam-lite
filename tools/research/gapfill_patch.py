#!/usr/bin/env python3
"""gapfill_patch.py - build a test image with the gap-fill hook in the DSP.

    python3 tools/research/gapfill_patch.py <in.hxf|.l6ia> <out.hxf> [--fill] [--donor stock.hxf]

Takes a built firmware (stock 3.80.0, or the NAM A2 Lite build so a RAM run
matches what the unit has installed) and adds, in Entry 2 (the SHARC boot
stream):

  1. the hook `tools/sharc/gapfill_stub.asm` as an INIT_L48 block after the
     last injected kernel (or at 0xc4ef8 on a stock image);
  2. three VISA parcel patches in the audio task (block 1 code), so that every
     block goes through the hook: the CJUMP at 0x176463 targets the stub, the
     two skip branches at 0x176400 and 0x176405 lead to it;
  3. with --fill: `tools/sharc/gapfill_kernel.asm` and its state, and the end
     of DSP heap 4 moved from 0xed7000 to 0xec7000 (immediates at 0x178855,
     0x17885b and 0x6098b7) to make room for the ring.

Every checksum is recomputed by l6ia.rebuild_archive. Without --fill the image
is the stock behaviour plus the hook: the first thing to try on the unit (from
RAM, RAM_RUN.md on upstream main). Addresses: research/gapless-preset-switch-findings.md.
"""
import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'sharc'))

import l6ia  # noqa: E402
import patch_amp  # noqa: E402
import sasm  # noqa: E402
from sharc_ldr import payload_words  # noqa: E402

SHARC_DIR = os.path.join(HERE, '..', 'sharc')

# (parcel address, stock parcels, patched parcels); the stub's address is filled in
AUDIO_TASK_PATCHES = [
    (0x176400, [0x070e, 0x0000, 0x0008], [0x070e, 0x0000, 0x0063]),   # IF SV JUMP (PC, +8) -> +0x63: the hook
    (0x176405, [0x072e, 0x0000, 0x005e], [0x073e, 0x0000, 0x005e]),   # IF NOT SV JUMP (PC, 0x176463) -> unconditional
    (0x176463, [0x1804, 0x0008, 0xcb20], None),                       # CJUMP 0x8cb20 (DB) -> CJUMP stub (DB)
]
HEAP_END_PATCHES = [
    (0x178855, [0x0f14, 0x00ed, 0x7000], [0x0f14, 0x00ec, 0x7000]),   # I4 = 0xed7000: heap 4 end
    (0x17885b, [0x0f0c, 0x00ba, 0xf000], [0x0f0c, 0x00b9, 0xf000]),   # R12 = 0xbaf000: heap 4 size
    (0x6098b7, [0x0f0c, 0x00ed, 0x7000], [0x0f0c, 0x00ec, 0x7000]),   # R12 = 0xed7000: the same end, at init
]


def ldr_blocks(w):
    """(tag, count, address, first payload word) of every block in the stream."""
    out, i = [], 384
    while i + 3 <= len(w):
        tag, cnt, addr = w[i:i + 3]
        out.append((tag, cnt, addr, i + 3))
        if tag == 0:
            break
        i += 3 + payload_words(tag, cnt)
    return out


def parcel_word(blocks, sw):
    """(word index, shift) of parcel address `sw` in the stream: INIT_L16 blocks pack
    two parcels per word, the high half at the lower address; INIT_EXT32 blocks
    hold external memory, whose lower parcel is the low half of the word."""
    for tag, cnt, addr, idx in blocks:
        if tag == 3 and addr <= sw < addr + cnt:
            k = sw - addr
            return idx + k // 2, 16 if k % 2 == 0 else 0
        if tag == 10 and 2 * addr <= sw < 2 * (addr + cnt):
            k = sw - 2 * addr
            return idx + k // 2, 0 if k % 2 == 0 else 16
    raise SystemExit(f'parcel {sw:#x} is not in the boot stream')


def patch_parcels(w, blocks, sw, expect, new):
    for k, (e, n) in enumerate(zip(expect, new)):
        wi, sh = parcel_word(blocks, sw + k)
        cur = (w[wi] >> sh) & 0xFFFF
        if cur != e:
            raise SystemExit(f'parcel {sw + k:#x} is {cur:#06x}, expected {e:#06x}: not the firmware these patches are for')
        w[wi] = (w[wi] & ~(0xFFFF << sh)) | (n << sh)


def assemble(path, pm_base):
    src = open(path).read()
    src = __import__('re').sub(r'(\.section\s+pm\s+)0x[0-9a-fA-F]+', lambda m: f'{m.group(1)}{pm_base:#x}', src, count=1)
    return src


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('output')
    ap.add_argument('--fill', action='store_true', help='add the fill kernel and the heap move (default: the hook only)')
    ap.add_argument('--donor', help='a stock .hxf for the container header when the input is a .l6ia')
    a = ap.parse_args()

    l6ia_bytes, is_hxf = patch_amp.load_l6ia_from_any(a.input)
    entries = l6ia.parse(l6ia_bytes)
    e2_raw = l6ia_bytes[entries[2]['payload_off']: entries[2]['payload_off'] + entries[2]['payload_len']]
    pm_end, _ = patch_amp._scan_e2_injected(e2_raw)
    w = list(struct.unpack('<%dI' % (len(e2_raw) // 4), e2_raw))
    blocks = ldr_blocks(w)

    # 1. the kernel (optional) and the stub, assembled together so the stub sees GAPFILL_ENTRY
    pm = (pm_end + 1) & ~1
    src = ''
    stub_pm = pm
    if a.fill:
        kernel = assemble(os.path.join(SHARC_DIR, 'gapfill_kernel.asm'), pm)
        ksecs, _ = sasm.assemble(kernel, 'gapfill_kernel.asm')
        stub_pm = pm + ((sum(len(s.words) for s in ksecs if s.kind == 'pm') + 1) & ~1)
        src += kernel + '\n'
    src += f'.equ GAPFILL, {1 if a.fill else 0}\n'
    if not a.fill:
        src += '.equ GAPFILL_ENTRY, 0\n'
    src += assemble(os.path.join(SHARC_DIR, 'gapfill_stub.asm'), stub_pm)
    secs, syms = sasm.assemble(src, 'gapfill')
    ldr_words = []
    for s in secs:
        if s.kind == 'pm':
            assert s.addr + len(s.words) <= 0xC5554, f'PM code at {s.addr:#x} runs past block 2'
            ldr_words += patch_amp.pack_init_l48(s.addr, s.words)
            print(f'[+] Entry 2: {len(s.words)} instructions @ PM {s.addr:#x}')
        else:
            ldr_words += patch_amp.pack_init_l32(s.addr, s.words)
            print(f'[+] Entry 2: {len(s.words)} data words @ DM {s.addr:#x}')
    stub = syms['GAPSTUB_ENTRY']
    print(f'[+] hook GAPSTUB_ENTRY = {stub:#x}' + (f', fill GAPFILL_ENTRY = {syms["GAPFILL_ENTRY"]:#x}' if a.fill else ' (hook only)'))

    # 2. the audio task patches (and 3. the heap move)
    patches = list(AUDIO_TASK_PATCHES)
    patches[2] = (0x176463, patches[2][1], [0x1804, stub >> 16, stub & 0xFFFF])
    if a.fill:
        patches += HEAP_END_PATCHES
    for sw, expect, new in patches:
        patch_parcels(w, blocks, sw, expect, new)
        print(f'[+] parcels {sw:#x}: {" ".join(f"{x:04x}" for x in expect)} -> {" ".join(f"{x:04x}" for x in new)}')
    e2_patched = struct.pack('<%dI' % len(w), *w)
    e2_patched = patch_amp.inject_sharc_ldr_blocks(e2_patched, ldr_words)

    new_l6ia, old_end, new_end = l6ia.rebuild_archive(l6ia_bytes, {2: l6ia.swap32(e2_patched)})
    donor = a.input if is_hxf else a.donor
    patch_amp.save_l6ia_to_any(new_l6ia, a.output, donor_hxf=donor)
    print(f'[+] wrote {a.output} (L6IA {old_end:#x} -> {new_end:#x})')


if __name__ == '__main__':
    main()
