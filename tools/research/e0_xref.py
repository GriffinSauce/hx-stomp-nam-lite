#!/usr/bin/env python3
"""e0_xref.py - a small cross-reference helper for Entry 0 (the Cortex-M4
application) of the HX Stomp firmware.

It loads Entry 0's segments at their load addresses, disassembles the code
segments with capstone (Thumb-2, linear sweep) and resolves, per instruction,
the constant it refers to: literal-pool loads, `adr`, `movw`/`movt` pairs and
branch targets. From that it answers "who refers to this address" and "who
calls this function", which is what the snapshot research needs.

It reads the owner's own firmware file; nothing from it is stored here.

Usage:
    e0_xref.py <fw.hxf|.l6ia> strings <text>          find a string, print its address
    e0_xref.py <fw> xrefs <addr> [addr...]             instructions that refer to the address(es)
    e0_xref.py <fw> callers <addr>                     bl/b/blx targets equal to addr
    e0_xref.py <fw> func <addr> [--max N]              disassemble a function
    e0_xref.py <fw> dis <addr> <count>                 disassemble N instructions
    e0_xref.py <fw> words <addr> <count>               dump 32-bit words
    e0_xref.py <fw> imm <value> [--mnem cmp,movs]      instructions with that immediate
    e0_xref.py <fw> bytes <hex>                        find a byte pattern (data)

Addresses are hex, with or without 0x. Thumb bit is ignored.
"""
import argparse
import bisect
import os
import pickle
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'sharc'))

import patch_amp  # noqa: E402

try:
    import capstone  # noqa: E402
except ImportError:
    raise SystemExit('pip install capstone')


class Entry0:
    def __init__(self, fw_path, cache_dir=None):
        l6ia_bytes, _ = patch_amp.load_l6ia_from_any(fw_path)
        self.e0 = patch_amp.get_entry_swapped(l6ia_bytes, 0)
        self.segs = patch_amp._parse_e0_segments(self.e0)
        # (load address, bytes) per segment
        self.mem = [(dst, self.e0[poff:poff + slen]) for _, poff, slen, dst, _ in self.segs]
        self.insns = {}        # addr -> (size, mnemonic, op_str)
        self.consts = {}       # addr -> resolved constant (int) for this instruction
        self.xrefs = {}        # constant -> [addr]
        self.calls = {}        # target -> [addr of bl/b/blx]
        self.func_starts = set()
        cache = None
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            cache = os.path.join(cache_dir, 'e0_xref_%s.pkl' % os.path.basename(fw_path))
        if cache and os.path.exists(cache):
            with open(cache, 'rb') as f:
                self.insns, self.consts, self.xrefs, self.calls, self.func_starts = pickle.load(f)
        else:
            self._sweep()
            if cache:
                with open(cache, 'wb') as f:
                    pickle.dump((self.insns, self.consts, self.xrefs, self.calls, self.func_starts), f)

    # ---- memory -------------------------------------------------------------
    def read(self, va, n):
        for dst, buf in self.mem:
            if dst <= va and va + n <= dst + len(buf):
                return buf[va - dst: va - dst + n]
        return None

    def u32(self, va):
        b = self.read(va, 4)
        return struct.unpack('<I', b)[0] if b else None

    def u16(self, va):
        b = self.read(va, 2)
        return struct.unpack('<H', b)[0] if b else None

    def va_to_foff(self, va):
        return patch_amp._e0_vaddr_to_foff(self.segs, va, 1)

    def cstring(self, va, maxlen=64):
        b = self.read(va, maxlen)
        if not b:
            return None
        end = b.find(b'\x00')
        s = b[:end if end >= 0 else maxlen]
        if all(32 <= c < 127 or c in (9, 10) for c in s):
            return s.decode('latin1')
        return None

    def find_bytes(self, pat):
        out = []
        for dst, buf in self.mem:
            start = 0
            while True:
                i = buf.find(pat, start)
                if i < 0:
                    break
                out.append(dst + i)
                start = i + 1
        return out

    def find_string(self, text):
        """Addresses of NUL-terminated strings equal to text, then substrings."""
        pat = text.encode('latin1')
        exact = [a for a in self.find_bytes(pat + b'\x00') if (self.read(a - 1, 1) == b'\x00' or a in (d for d, _ in self.mem))]
        return exact, self.find_bytes(pat)

    # ---- disassembly ---------------------------------------------------------
    def _code_segments(self):
        # Segments with code: local SRAM (0x1000xxxx) and the big SDRAM segment.
        for dst, buf in self.mem:
            if dst in (0x20000000,) or dst == 0x28000000 or dst == 0x28001be4:
                continue
            yield dst, buf

    def _sweep(self):
        md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        md.detail = False
        movw = {}
        for dst, buf in self._code_segments():
            prev_end_of_func = True
            # capstone stops at the first undecodable halfword; resume after it.
            pos = 0
            while pos < len(buf):
                got = 0
                for ins in md.disasm(buf[pos:], dst + pos):
                    got += ins.size
                    self._record(ins, movw, prev_end_of_func)
                    prev_end_of_func = self._ends_func(ins)
                pos += got + (0 if pos + got >= len(buf) else 2)

    def _ends_func(self, ins):
        m, ops = ins.mnemonic, ins.op_str
        return (m in ('pop', 'pop.w') and 'pc' in ops) or (m == 'bx' and ops == 'lr') \
            or m in ('b', 'b.w') or m.startswith('udf')

    def _record(self, ins, movw, prev_end_of_func):
                a = ins.address
                self.insns[a] = (ins.size, ins.mnemonic, ins.op_str)
                m, ops = ins.mnemonic, ins.op_str
                const = None
                if m.startswith('ldr') and ops.endswith(']') and '[pc' in ops:
                    mm = re.search(r'\[pc, #(-?0x[0-9a-f]+|-?\d+)\]', ops)
                    off = int(mm.group(1), 0) if mm else 0
                    lit = ((a + 4) & ~3) + off
                    if m in ('ldr', 'ldr.w'):
                        const = self.u32(lit)
                    else:
                        const = lit
                elif m == 'adr':
                    mm = re.search(r'#(0x[0-9a-f]+|\d+)', ops)
                    const = ((a + 4) & ~3) + int(mm.group(1), 0)
                elif m in ('movw', 'mov.w', 'movs', 'mov'):
                    mm = re.match(r'(\w+), #(0x[0-9a-f]+|\d+)$', ops)
                    if mm:
                        movw[mm.group(1)] = (int(mm.group(2), 0), a)
                        const = int(mm.group(2), 0) if m == 'movw' else None
                elif m == 'movt':
                    mm = re.match(r'(\w+), #(0x[0-9a-f]+|\d+)$', ops)
                    if mm and mm.group(1) in movw:
                        low, la = movw.pop(mm.group(1))
                        const = (int(mm.group(2), 0) << 16) | low
                        self.consts[la] = const
                        self.xrefs.setdefault(const, []).append(la)
                elif m in ('bl', 'blx', 'b', 'b.w') or m.startswith('b') and m[1:].rstrip('.w') in ('eq', 'ne', 'cs', 'cc', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'hs', 'lo'):
                    mm = re.match(r'#(0x[0-9a-f]+)$', ops)
                    if mm:
                        tgt = int(mm.group(1), 16) & ~1
                        self.calls.setdefault(tgt, []).append(a)
                        if m in ('bl', 'blx'):
                            self.func_starts.add(tgt)
                elif m in ('cbz', 'cbnz'):
                    mm = re.search(r'#(0x[0-9a-f]+)$', ops)
                    if mm:
                        self.calls.setdefault(int(mm.group(1), 16), []).append(a)
                if const is not None:
                    self.consts[a] = const
                    self.xrefs.setdefault(const, []).append(a)
                if m == 'push' and 'lr' in ops and prev_end_of_func:
                    self.func_starts.add(a)

    # ---- queries -------------------------------------------------------------
    def func_start(self, addr):
        """The nearest function start at or before addr."""
        if not hasattr(self, '_sorted_starts'):
            self._sorted_starts = sorted(self.func_starts)
        i = bisect.bisect_right(self._sorted_starts, addr & ~1)
        return self._sorted_starts[i - 1] if i else addr

    def func_insns(self, start, max_insns=600):
        """Instructions from start to a heuristic function end."""
        out = []
        a = start
        n = 0
        while a in self.insns and n < max_insns:
            size, m, ops = self.insns[a]
            out.append((a, size, m, ops))
            n += 1
            if (m in ('pop', 'pop.w') and 'pc' in ops) or (m == 'bx' and ops == 'lr'):
                # a following unconditional branch target may continue the function; stop
                nxt = a + size
                if nxt in self.func_starts or nxt not in self.insns:
                    break
                # keep going only if the next instruction is reachable from inside (branch target)
                if not any(nxt == t for t in self._branch_targets_in(start, nxt)):
                    break
            if m in ('b', 'b.w'):
                nxt = a + size
                if nxt in self.func_starts or not any(nxt <= t < nxt + 2 for t in self._branch_targets_in(start, nxt)):
                    # fallthrough unreachable unless something branches past here
                    later = [t for t in self._branch_targets_in(start, nxt) if t >= nxt]
                    if not later:
                        break
            a += size
        return out

    def _branch_targets_in(self, lo, hi):
        ts = []
        a = lo
        while a < hi and a in self.insns:
            size, m, ops = self.insns[a]
            if m[0] == 'b' or m in ('cbz', 'cbnz'):
                mm = re.search(r'#(0x[0-9a-f]+)$', ops)
                if mm:
                    ts.append(int(mm.group(1), 16) & ~1)
            a += size
        return ts

    def fmt(self, a, size, m, ops):
        extra = ''
        c = self.consts.get(a)
        if c is not None:
            s = self.cstring(c)
            extra = f'  ; {c:#x}' + (f' "{s}"' if s else '')
            if m.startswith('ldr') and c is not None and m not in ('ldr', 'ldr.w'):
                pass
        return f'{a:#010x}: {m:8s} {ops}{extra}'


def parse_addr(s):
    return int(s, 16) & ~1 if not s.startswith('0x') else int(s, 16) & ~1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('fw')
    ap.add_argument('cmd')
    ap.add_argument('args', nargs='*')
    ap.add_argument('--max', type=int, default=600)
    ap.add_argument('--mnem', default=None)
    ap.add_argument('--range', default=None, help='hex address range lo-hi for imm')
    ap.add_argument('--cache', default=os.environ.get('E0_CACHE'))
    a = ap.parse_args()
    e = Entry0(a.fw, a.cache)

    if a.cmd == 'strings':
        text = ' '.join(a.args)
        exact, subs = e.find_string(text)
        for x in exact:
            print(f'{x:#010x}: exact "{e.cstring(x)}"  xrefs={[hex(r) for r in e.xrefs.get(x, [])]}')
        for x in subs:
            if x not in exact:
                start = x
                while e.read(start - 1, 1) not in (None, b'\x00') and x - start < 64:
                    start -= 1
                s = e.cstring(start, 128)
                print(f'{start:#010x}: substring {s!r}  xrefs={[hex(r) for r in e.xrefs.get(start, [])]}')
    elif a.cmd == 'xrefs':
        for s in a.args:
            addr = parse_addr(s)
            for r in e.xrefs.get(addr, []):
                f = e.func_start(r)
                print(f'{addr:#x} <- {e.fmt(r, *e.insns[r])}   (func {f:#x})')
    elif a.cmd == 'callers':
        addr = parse_addr(a.args[0])
        for r in e.calls.get(addr, []):
            print(f'{e.fmt(r, *e.insns[r])}   (func {e.func_start(r):#x})')
    elif a.cmd == 'func':
        start = parse_addr(a.args[0])
        if start not in e.func_starts:
            start = e.func_start(start)
        for ins in e.func_insns(start, a.max):
            print(e.fmt(*ins))
    elif a.cmd == 'dis':
        addr = parse_addr(a.args[0])
        n = int(a.args[1]) if len(a.args) > 1 else 40
        while n and addr in e.insns:
            print(e.fmt(addr, *e.insns[addr]))
            addr += e.insns[addr][0]
            n -= 1
    elif a.cmd == 'words':
        addr = parse_addr(a.args[0])
        n = int(a.args[1]) if len(a.args) > 1 else 16
        for i in range(0, n, 4):
            ws = [e.u32(addr + 4 * (i + k)) for k in range(4)]
            raw = e.read(addr + 4 * i, 16) or b''
            asc = ''.join(chr(c) if 32 <= c < 127 else '.' for c in raw)
            print(f'{addr + 4 * i:#010x}: ' + ' '.join('--------' if w is None else f'{w:08x}' for w in ws) + f'  {asc}')
    elif a.cmd == 'dump':
        # the whole listing, one instruction per line, with the enclosing function
        for addr, (size, m, ops) in sorted(e.insns.items()):
            print(f'{e.fmt(addr, size, m, ops)}   (func {e.func_start(addr):#x})')
    elif a.cmd == 'imm':
        val = int(a.args[0], 0)
        mnems = a.mnem.split(',') if a.mnem else None
        lo, hi = (int(x, 16) for x in a.range.split('-')) if a.range else (0, 1 << 32)
        pat = re.compile(r'#(0x[0-9a-f]+|\d+)$')
        for addr, (size, m, ops) in sorted(e.insns.items()):
            if mnems and m.split('.')[0] not in mnems:
                continue
            if not lo <= addr < hi:
                continue
            mm = pat.search(ops)
            if mm and int(mm.group(1), 0) == val:
                print(f'{e.fmt(addr, size, m, ops)}   (func {e.func_start(addr):#x})')
    elif a.cmd == 'bytes':
        for x in e.find_bytes(bytes.fromhex(a.args[0])):
            print(f'{x:#010x}  xrefs={[hex(r) for r in e.xrefs.get(x, [])]}')
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
