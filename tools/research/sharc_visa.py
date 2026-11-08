#!/usr/bin/env python3
"""sharc_visa.py - a disassembler for the VISA (16/32/48-bit) SHARC code in
Entry 2 of the HX Stomp firmware, with cross-references.

Most of the DSP program is compiled C in the ADSP-2146x "Variable Instruction
Set Architecture": instructions of one, two or three 16-bit parcels in the
short-word address space (internal SRAM blocks 0-2 at 0x118000.., external
DDR2 at 0x400000..). The 48-bit forms are the ISA encodings sharc_dis.py
decodes; the 32-bit forms are the 48-bit forms with the compute field cut off
(bits 22..16 = 0x3f); the 16-bit forms are a short compute, a DM move, NOP,
RTS/RTI and RFRAME. Encodings as in the ADI Programming Reference, cross-checked
against the ghidra-sharc SLEIGH generator (tools/gen_sinc.py, Apache-2.0).

Branch targets: a PC-relative offset is in parcels; an absolute or computed
target in 0x80000..0xfffff is a 48-bit (ISA) address, shown as such.

Code is found by recursive descent from the interrupt vectors, the primitive
registry and every address ISA code jumps to, then from the CJUMP/CALL targets
found on the way. It reads the owner's own firmware file; nothing from it is
stored here.

Usage:
    sharc_visa.py <fw> func <sw-addr>           disassemble a function
    sharc_visa.py <fw> at <sw-addr>             disassemble the function holding the address
    sharc_visa.py <fw> dis <sw-addr> [count]
    sharc_visa.py <fw> funcs                    every function found, with size and callers
    sharc_visa.py <fw> callers <sw-addr>
    sharc_visa.py <fw> xrefs <value> [...]      instructions carrying that constant or address
    sharc_visa.py <fw> dump                     everything reached, one instruction per line
    sharc_visa.py <fw> coverage                 how much of each code region was reached
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'sharc'))

import sasm  # noqa: E402
import sharc_dis  # noqa: E402
from sharc_dis import compute_text, cond_prefix, sx, ureg  # noqa: E402

SHORT = ["R{n} = R{n} + R{x}", "R{n} = R{n} - R{x}", "R{n} = PASS R{x}", "COMP(R{n}, R{x})",
         "R{n} = NOT R{x}", "R{n} = R{x} + 1", "R{n} = R{x} - 1", "R{n} = R{n} * R{x} (SSI)",
         "F{n} = F{n} + F{x}", "F{n} = F{n} - F{x}", "F{n} = FLOAT R{x}", "COMP(F{n}, F{x})",
         "R{n} = R{n} AND R{x}", "R{n} = R{n} OR R{x}", "R{n} = R{n} XOR R{x}", "F{n} = F{n} * F{x}"]

M24 = 0xFFFFFF


def map48(t):
    """A target in the 48-bit range is an ISA address; everything else is a parcel address."""
    return t


def is_isa(t):
    return 0x80000 <= t < 0x100000


def isa_to_sw(a):
    return 3 * a - (a & 0xe0000)


def sw_to_isa(sw):
    """The 48-bit address of a parcel address that aliases ISA code, or None."""
    for blk in (0x80000, 0xa0000, 0xc0000, 0xe0000):
        base = 2 * blk
        if base <= sw < base + 0x60000:
            a, r = divmod(sw - base, 3)
            return blk + a if r == 0 else None
    return None


class VInsn:
    __slots__ = ('addr', 'size', 'word', 'text', 'const', 'target', 'call', 'ends', 'db')

    def __init__(self, addr, size, word):
        self.addr, self.size, self.word = addr, size, word
        self.text, self.const, self.target, self.call, self.ends, self.db = '', None, None, False, False, False


def decode(p0, p1, p2, pc):
    """Decode the instruction whose first parcel is p0 at parcel address pc."""
    # ---- 16-bit forms
    top4 = p0 >> 12
    if top4 == 0xC:
        ins = VInsn(pc, 1, p0)
        op, n, x = p0 >> 8 & 15, p0 >> 4 & 15, p0 & 15
        ins.text = SHORT[op].replace('{n}', str(n)).replace('{x}', str(x))
        return ins
    if top4 == 0x9 and p0 >> 4 & 1:
        ins = VInsn(pc, 1, p0)
        i, m, d, r = p0 >> 9 & 7, p0 >> 6 & 7, p0 >> 5 & 1, p0 & 15
        ins.text = f'DM(I{i}, M{m}) = R{r}' if d else f'R{r} = DM(I{i}, M{m})'
        return ins
    if p0 >> 8 == 0x00 and p0 & 1:
        ins = VInsn(pc, 1, p0)
        ins.text = 'IDLE' if p0 >> 7 & 1 else 'NOP'
        return ins
    if p0 >> 8 in (0x0A, 0x0B) and p0 >> 7 & 1:
        ins = VInsn(pc, 1, p0)
        cc, db, lr = p0 >> 1 & 31, p0 >> 6 & 1, p0 & 1
        mods = [x for x, f in (('DB', db), ('LR', lr)) if f]
        ins.text = f'{cond_prefix(cc)}{"RTI" if p0 >> 8 == 0x0B else "RTS"}' + (f' ({", ".join(mods)})' if mods else '')
        ins.ends, ins.db = cc == 31, bool(db)
        return ins
    if p0 == 0x1901:
        ins = VInsn(pc, 1, p0)
        ins.text = 'RFRAME'
        return ins
    # ---- 32-bit forms
    w32 = p0 << 16 | p1
    top = p0 >> 13
    op8 = p0 >> 8
    nocomp = (p1 & 0x7F) == 0x3F
    w48_nc = (p0 << 32) | ((p1 & 0xFF80) << 16)          # the 48-bit form with no compute
    if top == 1 and nocomp or top == 2 and nocomp or top == 3 and nocomp:
        return _from48(w48_nc, pc, 2)
    if top == 4 and p0 >> 12 == 0x9:
        sub = p0 >> 2 & 7                                   # bits 36..34
        if sub == 1:                                        # 16b: DM(Ia, Mb) = data16
            ins = VInsn(pc, 2, w32)
            g, i, m = p0 >> 5 & 1, p0 >> 9 & 7, p0 >> 6 & 7
            v = sx(p1, 16)
            ins.const = v & 0xFFFFFFFF
            ins.text = f'{"PM" if g else "DM"}(I{i + 8 * g}, M{m + 8 * g}) = {v:#x}' if v >= 0 else f'{"PM" if g else "DM"}(I{i + 8 * g}, M{m + 8 * g}) = {v}'
            return ins
        if sub == 2:                                        # 15b: ureg <-> DM(data7, Ia)
            ins = VInsn(pc, 2, w32)
            g, i, d, lw = p0 >> 5 & 1, p0 >> 9 & 7, p0 >> 8 & 1, p0 >> 7 & 1
            u = w32 >> 7 & 0x7F                             # bits 29..23
            d7 = sx(p1 & 0x7F, 7)
            a = f'{"PM" if g else "DM"}({d7}, I{i + 8 * g})'
            ins.text = (f'{a} = {ureg(u)}' if d else f'{ureg(u)} = {a}') + (' (LW)' if lw else '')
            return ins
        # sub == 0: 16a with data32, 48-bit
    if top == 0:
        if op8 == 0x01 and p0 >> 7 & 1:                     # 2b: compute in bits 38..16
            ins = VInsn(pc, 2, w32)
            ins.text = compute_text(w32 & 0x7FFFFF)
            return ins
        if op8 == 0x04 and nocomp:
            return _from48(w48_nc, pc, 2)
        if op8 in (0x08, 0x09) and not (p1 >> 9 & 1) and nocomp:   # type 9 without compute, E = 0
            return _from48(w48_nc, pc, 2)
        if op8 == 0x0F and p0 >> 7 & 1:                     # ureg = data16
            ins = VInsn(pc, 2, w32)
            v = sx(p1, 16)
            ins.const = v & 0xFFFFFFFF
            ins.text = f'{ureg(p0 & 0x7F)} = {v:#x}' if v >= 0 else f'{ureg(p0 & 0x7F)} = {v}'
            return ins
    # ---- 48-bit forms
    return _from48(p0 << 32 | p1 << 16 | p2, pc, 3)


def _from48(w, pc, size):
    base = sharc_dis.Insn(pc, w)
    ins = VInsn(pc, size, w)
    ins.text, ins.const, ins.call, ins.ends = base.text, base.const, base.call, base.ends
    ins.db = '(DB' in base.text or ', DB' in base.text
    if base.target is not None:
        t = base.target
        op8 = w >> 40
        # sharc_dis computed a relative target as pc + offset; the same holds in parcels
        ins.target = t
        if is_isa(t):
            ins.text = ins.text.replace(f'{t:#x}', f'{t:#x}') + '  ; ISA'
    # the VisualDSP function return: JUMP (M14, I12) (DB)
    if 'JUMP (M14, I12)' in ins.text and not ins.text.startswith('IF'):
        ins.ends = True
    return ins


class VisaProgram:
    REGIONS = ((0x13ac4a, 0x13c000, 'block 0'), (0x16e0b0, 0x17bffb, 'block 1'),
               (0x182508, 0x183138, 'block 2'), (0x600000, 0x61a0de, 'DDR2'))

    def __init__(self, fw_path, ext_low_first=True):
        # internal short-word blocks: the high half of a stream word first (sharc_ldr);
        # external memory: the low half of a 32-bit word is the lower parcel address
        self.isa = sharc_dis.Program(fw_path)
        img = self.isa.img
        self.sw = dict(img.dm16)
        for a, v in img.dm.items():
            if a >= 0x200000:
                lo, hi = v & 0xFFFF, v >> 16
                if ext_low_first:
                    self.sw[2 * a], self.sw[2 * a + 1] = lo, hi
                else:
                    self.sw[2 * a], self.sw[2 * a + 1] = hi, lo
        self.insns = {}
        self.xrefs = {}
        self.calls = {}
        self.funcs = set()
        self._descend(self.entries())
        # code reached only through pointers: scan what is left for function prologues
        # (MODIFY(I7, -n), the C frame) and for pointers in data, then descend again
        for _ in range(4):
            more = self.prologues() | self.pointer_entries()
            more -= set(self.insns)
            if not more:
                break
            self.funcs |= more
            self._descend(more)

    def in_code(self, a):
        return any(lo <= a < hi for lo, hi, _ in self.REGIONS)

    def prologues(self):
        out = set()
        for lo, hi, _ in self.REGIONS:
            a = lo
            while a < hi - 2:
                if a not in self.insns and self.sw.get(a) == 0x1607 and self.sw.get(a + 1) == 0xFFFF \
                        and self.sw.get(a + 2, 0) >= 0xFF00:
                    out.add(a)
                a += 1
        return out

    def pointer_entries(self):
        """Function pointers in data: 32-bit words, and parcel pairs in the short-word blocks."""
        out = set()
        img = self.isa.img
        for a, v in img.dm.items():
            if self.in_code(v) and a < 0x200000 and v not in self.insns and self.sw.get(v) is not None:
                out.add(v)
        for a in sorted(self.sw):
            if a in self.insns:
                continue
            v = (self.sw[a] << 16) | self.sw.get(a + 1, 0)
            if self.in_code(v) and v not in self.insns and self.sw.get(v) == 0x1607:
                out.add(v)
        # code addresses passed as arguments (R4, R8, R12 loaded just before a CJUMP):
        # task entries and callbacks
        addrs = sorted(self.insns)
        for k, a in enumerate(addrs):
            ins = self.insns[a]
            c = ins.const
            if c is None or not self.in_code(c) or c in self.insns or not ins.text.startswith(('R4 = ', 'R8 = ', 'R12 = ')):
                continue
            if any(self.insns[b].text.startswith('CJUMP') for b in addrs[k + 1:k + 5]):
                out.add(c)
        return out

    def entries(self):
        img = self.isa.img
        ents = set()
        for ins in self.isa.insns.values():                 # ISA code jumping into VISA code
            if ins.target is not None and not is_isa(ins.target) and ins.target in self.sw:
                ents.add(ins.target)
            if ins.const is not None and ins.const in self.sw and ins.addr < 0x8c100 \
                    and any(ins.text.startswith(r) for r in ('I12 = ', 'R2 = ')):
                ents.add(ins.const)                         # the vector stubs' handler addresses
        for a in range(0x200000, 0x2008d0, 8):              # the primitive registry
            for k in (1, 3, 4, 5):
                v = img.dm.get(a + k, 0)
                if v and not is_isa(v) and v in self.sw:
                    ents.add(v)
        self.funcs |= ents
        return ents

    def fetch(self, a):
        return self.sw.get(a), self.sw.get(a + 1, 0), self.sw.get(a + 2, 0)

    def _descend(self, starts):
        work = list(starts)
        while work:
            a = work.pop()
            while a not in self.insns:
                p0, p1, p2 = self.fetch(a)
                if p0 is None:
                    break
                ins = decode(p0, p1, p2, a)
                self.insns[a] = ins
                if ins.const is not None:
                    self.xrefs.setdefault(ins.const, []).append(a)
                if ins.target is not None:
                    self.calls.setdefault(ins.target, []).append(a)
                    if ins.call or ins.text.startswith('CJUMP'):
                        self.funcs.add(ins.target)
                    if not is_isa(ins.target) and ins.target in self.sw:
                        work.append(ins.target)
                if ins.ends:
                    if ins.db:                              # two delay slots still run
                        n = a + ins.size
                        for _ in range(2):
                            q0, q1, q2 = self.fetch(n)
                            if q0 is None:
                                break
                            d = decode(q0, q1, q2, n)
                            self.insns.setdefault(n, d)
                            if d.const is not None:
                                self.xrefs.setdefault(d.const, []).append(n)
                            n += d.size
                    break
                a += ins.size

    def fmt(self, ins):
        parcels = ' '.join(f'{self.sw.get(ins.addr + k, 0):04x}' for k in range(ins.size)).ljust(14)
        return f'{ins.addr:#08x}: {parcels} {ins.text}'

    def func(self, start, max_insns=3000):
        out = []
        a = start
        pending = 0
        while a in self.insns and len(out) < max_insns:
            ins = self.insns[a]
            out.append(ins)
            if ins.ends:
                if ins.db:
                    for _ in range(2):
                        a += ins.size
                        if a in self.insns:
                            ins = self.insns[a]
                            out.append(ins)
                later = [s.target for s in out if s.target is not None and s.target > a and not is_isa(s.target)]
                if not later:
                    break
            a += ins.size
        return out


def parse_addr(s):
    return int(s, 16)


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    fw, cmd, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    prog = VisaProgram(fw)
    if cmd in ('func', 'at'):
        start = parse_addr(args[0])
        if cmd == 'at':                                     # the function holding the address
            start = max((f for f in prog.funcs if f <= start), default=start)
            print(f'; function {start:#x}')
        for ins in prog.func(start):
            print(prog.fmt(ins))
    elif cmd == 'dis':
        a = parse_addr(args[0])
        n = int(args[1], 0) if len(args) > 1 else 40
        for _ in range(n):
            p0, p1, p2 = prog.fetch(a)
            if p0 is None:
                break
            ins = prog.insns.get(a) or decode(p0, p1, p2, a)
            print(prog.fmt(ins))
            a += ins.size
    elif cmd == 'funcs':
        for f in sorted(prog.funcs):
            body = prog.func(f)
            print(f'{f:#08x}  {len(body):5d} insns  {len(prog.calls.get(f, [])):3d} callers')
    elif cmd == 'callers':
        t = parse_addr(args[0])
        for a in prog.calls.get(t, []):
            print(prog.fmt(prog.insns[a]))
    elif cmd == 'xrefs':
        for s in args:
            v = parse_addr(s)
            for a in prog.xrefs.get(v, []):
                print(f'{v:#x} <- {prog.fmt(prog.insns[a])}')
    elif cmd == 'dump':
        for a in sorted(prog.insns):
            ins = prog.insns[a]
            f = '  ; FUNC' if a in prog.funcs else ''
            print(prog.fmt(ins) + f)
    elif cmd == 'coverage':
        for lo, hi, name in prog.REGIONS:
            got = sum(prog.insns[a].size for a in prog.insns if lo <= a < hi)
            print(f'{name:8s} {lo:#x}..{hi:#x}: {got} of {hi - lo} parcels reached ({100 * got / (hi - lo):.0f}%)')
        unk = sum(1 for i in prog.insns.values() if i.text.startswith('<'))
        print(f'{len(prog.insns)} instructions, {len(prog.funcs)} functions, {unk} undecoded')
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
