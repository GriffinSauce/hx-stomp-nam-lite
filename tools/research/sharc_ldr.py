#!/usr/bin/env python3
"""sharc_ldr.py - read Entry 2 of the HX Stomp firmware (the SHARC ADSP-21469
boot stream) into memory images.

The stream is the ADI "LDR" format for a 2146x SPI/parallel boot: 384 32-bit
words of boot kernel (loaded at PM 0x8c000 by the ROM), then blocks of
{tag, count, address} headers and payload, ending in a FINAL_INIT block: tag
0, two words holding the instruction the kernel runs last (a JUMP to the
program's entry, `entry_insn`), then 384 words (256 instructions) that replace
the boot kernel at 0x8c000 (the interrupt vector table).

It reads the owner's own firmware file; nothing from it is stored here.

    from sharc_ldr import Image
    img = Image('HX_Stomp_v3.80.0_0x103966115.hxf')
    img.pm[0x8c100]      # a 48-bit instruction word
    img.dm[0xc0a6c]      # a 32-bit data word (internal, or external from 0x200000)
    img.dm16[0x13ac4a]   # a 16-bit short-word
    img.blocks           # [(tag, count, address, payload_word_index)]

Usage: sharc_ldr.py <fw.hxf|.l6ia> [blocks|map]
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'sharc'))

import l6ia  # noqa: E402
import patch_amp  # noqa: E402

TAG_NAMES = {0: 'FINAL_INIT', 1: 'ZERO_LDATA', 2: 'ZERO_L48', 3: 'INIT_L16', 4: 'INIT_L32',
             5: 'INIT_L48', 6: 'INIT_L64', 7: 'ZERO_L64', 8: 'ZERO_L16', 9: 'INIT_EXT9',
             10: 'INIT_EXT32', 11: 'ZERO_EXT11', 12: 'ZERO_EXT12'}


def payload_words(tag, count):
    """32-bit words of payload that follow a block header."""
    return {0: 384, 3: (count + 1) // 2, 4: count, 5: ((count + 1) & ~1) * 3 // 2,
            6: count * 2, 9: count, 10: count}.get(tag, 0)


class Image:
    def __init__(self, fw_path):
        l6ia_bytes, _ = patch_amp.load_l6ia_from_any(fw_path)
        ents = l6ia.parse(l6ia_bytes)
        e2 = l6ia_bytes[ents[2]['payload_off']: ents[2]['payload_off'] + ents[2]['payload_len']]
        self.raw = e2
        self.w = struct.unpack('<%dI' % (len(e2) // 4), e2)
        self.pm = {}        # 48-bit words by normal-word address
        self.dm = {}        # 32-bit words by normal-word address
        self.dm16 = {}      # 16-bit words by short-word address
        self.zero = []      # (kind, address, count) of zero-filled ranges
        self.blocks = []    # (tag, count, address, index of the first payload word)
        self.boot_kernel = self._l48(0, 256)
        i = 384
        while i + 3 <= len(self.w):
            tag, cnt, addr = self.w[i:i + 3]
            nw = payload_words(tag, cnt)
            self.blocks.append((tag, cnt, addr, i + 3))
            if tag == 0:
                # header: tag, then the instruction the kernel runs last (a JUMP to
                # the program's entry) as a 64-bit value shifted left 16 bits
                self.entry_insn = ((self.w[i + 2] << 32) | self.w[i + 1]) >> 16
                self.final_init = self._l48(i + 3, 256)
                for k, v in enumerate(self.final_init):
                    self.pm[0x8c000 + k] = v
                break
            if tag == 5:
                for k, v in enumerate(self._l48(i + 3, cnt)):
                    self.pm[addr + k] = v
            elif tag in (4, 9, 10):
                for k in range(cnt):
                    self.dm[addr + k] = self.w[i + 3 + k]
            elif tag == 3:
                # two short words per 32-bit stream word, the high half at the lower address
                for k in range(cnt):
                    word = self.w[i + 3 + k // 2]
                    self.dm16[addr + k] = (word >> 16) if k % 2 == 0 else (word & 0xFFFF)
            elif tag == 6:
                for k in range(cnt):
                    self.dm[addr + 2 * k] = self.w[i + 3 + 2 * k]
                    self.dm[addr + 2 * k + 1] = self.w[i + 3 + 2 * k + 1]
            elif tag in (1, 2, 7, 8, 11, 12):
                self.zero.append((TAG_NAMES[tag], addr, cnt))
                if tag == 1 and 0x100000 <= addr < 0x200000:    # short-word space
                    for k in range(cnt):
                        self.dm16.setdefault(addr + k, 0)
                elif tag == 1:
                    for k in range(cnt):
                        self.dm.setdefault(addr + k, 0)
                elif tag == 2:
                    for k in range(cnt):
                        self.pm.setdefault(addr + k, 0)
            i += 3 + nw

    def _l48(self, word_index, count):
        raw = self.raw[word_index * 4: word_index * 4 + ((count + 1) & ~1) * 6]
        return [int.from_bytes(raw[6 * k: 6 * k + 6], 'little') for k in range(count)]

    def ranges(self, mem):
        """Contiguous address ranges of a memory dict, as (start, end)."""
        out = []
        for a in sorted(mem):
            if out and out[-1][1] == a:
                out[-1][1] = a + 1
            else:
                out.append([a, a + 1])
        return [tuple(r) for r in out]


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    img = Image(sys.argv[1])
    cmd = sys.argv[2] if len(sys.argv) > 2 else 'map'
    if cmd == 'blocks':
        for tag, cnt, addr, idx in img.blocks:
            print(f'{TAG_NAMES.get(tag, str(tag)):11s} count={cnt:#8x} addr={addr:#09x}..{addr + cnt:#09x} @w{idx}')
    else:
        print('PM (48-bit code) ranges:')
        for a, b in img.ranges(img.pm):
            print(f'  {a:#09x}..{b:#09x}  {b - a:6d} instructions')
        print('DM (32-bit data) ranges:')
        for a, b in img.ranges(img.dm):
            print(f'  {a:#09x}..{b:#09x}  {b - a:6d} words')
        print('DM16 (short-word data) ranges:')
        for a, b in img.ranges(img.dm16):
            print(f'  {a:#09x}..{b:#09x}  {b - a:6d} halfwords (normal {a // 2:#x}..{(b + 1) // 2:#x})')
        print('zero-filled:')
        for kind, a, n in img.zero:
            print(f'  {kind:11s} {a:#09x}..{a + n:#09x}  {n:6d}')


if __name__ == '__main__':
    main()
