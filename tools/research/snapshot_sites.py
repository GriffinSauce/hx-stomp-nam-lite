#!/usr/bin/env python3
"""snapshot_sites.py - list every place in HX Stomp 3.80.0 Entry 0 where the
snapshot count (3) or the snapshot index bound (2) is used, with the payload
offset and the instruction bytes read from the owner's own firmware file.

The addresses come from the research in research/more-snapshots-findings.md.
Run it against the stock firmware to check they still match:

    python3 tools/research/snapshot_sites.py HX_Stomp_v3.80.0_0x103966115.hxf

Each row prints: load address, Entry 0 payload offset, bytes, disassembly and
the expected mnemonic. A row marked MISMATCH means the firmware is not the
one the addresses were found in.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'sharc'))

import patch_amp  # noqa: E402

try:
    import capstone  # noqa: E402
except ImportError:
    raise SystemExit('pip install capstone')

# (address, size, expected text, what it is)
SITES = [
    # --- the Preset object and its snapshot array -------------------------------
    (0x28de3c0a, 4, 'movw r2, #0x9e4', 'Preset constructor: sizeof(Preset) = 0x9e4 (memset)'),
    (0x28de3c56, 4, 'add.w r6, r4, #0xc', 'Preset constructor: snapshot[0] at Preset+0xc'),
    (0x28de3c82, 4, 'add.w r6, r6, #0x260', 'Preset constructor: sizeof(Snapshot) = 0x260'),
    (0x28de3c80, 2, 'cmp r5, #4', 'Preset constructor: name loop 1..3 ("SNAPSHOT n")'),
    (0x28de1cf0, 2, 'cmp r5, #4', 'Preset::initSnapshots (0x28de1c9c): name loop 1..3'),
    (0x28de1e60, 4, 'cmp.w r8, #4', 'Preset::resetSnapshots (0x28de1d04): loop 1..3'),
    (0x28de432a, 2, 'cmp r4, #4', 'Preset decoder (0x28de42c4): pre-fill names 1..3'),
    # --- loops over the three snapshots -----------------------------------------
    (0x28de262c, 4, 'cmp.w lr, #3', 'Preset::removeController (0x28de2594): loop over snapshots'),
    (0x28de2674, 4, 'cmp.w lr, #3', 'Preset::removeController: second loop over snapshots'),
    (0x28de26ae, 4, 'cmp.w lr, #0x720', 'Preset::removeController: third loop, 3*0x260'),
    (0x28de29e8, 4, 'cmp.w fp, #3', 'Preset::addController (0x28de285c): loop over snapshots'),
    (0x28de2a80, 4, 'cmp.w fp, #3', 'Preset::addController: second loop over snapshots'),
    (0x28de2c9e, 4, 'cmp.w r8, #3', 'Preset::clearController (0x28de2ba4): loop over snapshots'),
    (0x28de3d70, 2, 'movs r3, #0x93', 'Preset encoder (0x28de3c9c): msgpack fixarray(3) header for snapshots'),
    (0x28de3f64, 4, 'cmp.w r5, #0x720', 'Preset encoder: snapshot loop end, 3*0x260'),
    (0x28de420c, 4, 'cmp.w r5, #0x720', 'Preset encoder: snapshot loop end (second exit), 3*0x260'),
    # --- snapshot index bounds (0..2) ---------------------------------------------
    (0x28de2fdc, 4, 'cmp.w fp, #2', 'Preset::selectSnapshot (0x28de2f90): index > 2 is rejected (-0x2e)'),
    (0x28de329c, 2, 'cmp r5, #2', 'Preset::selectSnapshot: previous index must be <= 2 to copy from'),
    (0x28d9b5a2, 2, 'cmp r1, #2', 'MIDI CC 69 handler: values 0..2 select, 8 next, 9 previous'),
    (0x28de59e6, 2, 'cmp r0, #2', 'Footswitch snapshot command (0x28de59a8): next wraps after 2'),
    (0x28dd162e, 2, 'cmp r0, #2', 'UI (0x28dd14a0): next snapshot wraps after 2'),
    (0x28dcfe7e, 2, 'cmp r0, #1', 'UI (0x28dcfc30): increment snapshot only while index <= 1'),
    # --- the decoder takes the count from the blob, unclamped --------------------
    (0x28de441e, 4, 'and r3, r3, #0xf', 'Preset decoder: snapshot count from fixarray header (no clamp)'),
    (0x28de4cb2, 2, 'ldrb r3, [r3, #2]', 'Preset decoder: snapshot count low byte from array16 (no clamp)'),
    # --- the scratch (fourth) snapshot slot at Preset+0x748 ----------------------
    (0x28de1824, 4, 'add.w r1, r0, #0x748', 'Preset::backupSnapshot (0x28de1810): copy current -> Preset+0x748'),
    (0x28de1860, 4, 'add.w r2, r0, #0x748', 'Preset::restoreSnapshot (0x28de184c): copy Preset+0x748 -> current'),
]


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    l6ia_bytes, _ = patch_amp.load_l6ia_from_any(sys.argv[1])
    e0 = patch_amp.get_entry_swapped(l6ia_bytes, 0)
    segs = patch_amp._parse_e0_segments(e0)
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    bad = 0
    print(f'{"address":10s}  {"offset":8s}  {"bytes":9s}  {"instruction":26s}  what')
    for addr, size, expect, what in SITES:
        foff = patch_amp._e0_vaddr_to_foff(segs, addr, size)
        raw = e0[foff:foff + size]
        ins = next(md.disasm(raw, addr), None)
        text = f'{ins.mnemonic} {ins.op_str}' if ins else '?'
        ok = ins is not None and ins.size == size and text.replace(' ', '') == expect.replace(' ', '')
        bad += not ok
        print(f'{addr:#010x}  {foff:#08x}  {raw.hex():9s}  {text:26s}  {what}' + ('' if ok else f'   MISMATCH (expected {expect})'))
    print(f'\n{len(SITES)} sites, {bad} mismatches')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
