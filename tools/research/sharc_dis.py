#!/usr/bin/env python3
"""sharc_dis.py - a disassembler for the 48-bit ADSP-2146x SHARC code in Entry 2
of the HX Stomp firmware, with cross-references.

The text it writes is the ADI syntax tools/sharc/sasm.py assembles, so the
`verify` command checks it: every stock instruction, disassembled and
re-assembled, must give its own bytes back. Field positions follow sasm.encode.

It reads the owner's own firmware file; nothing from it is stored here.

Usage:
    sharc_dis.py <fw> verify                    round-trip every instruction through sasm
    sharc_dis.py <fw> dis <addr> [count]        disassemble
    sharc_dis.py <fw> func <addr>               disassemble up to the function's RTS
    sharc_dis.py <fw> xrefs <value> [value...]  instructions that carry that address or constant
    sharc_dis.py <fw> callers <addr>            CALLs and JUMPs to the address
    sharc_dis.py <fw> funcs                     every CALL target, with its caller count
    sharc_dis.py <fw> iop                       accesses to IOP registers (addresses below 0x4000)
    sharc_dis.py <fw> dm <addr> [count]         dump 32-bit data words
    sharc_dis.py <fw> dump                      the whole program, one instruction per line

Addresses are hex (0x optional).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'sharc'))

import sasm  # noqa: E402
from sharc_ldr import Image  # noqa: E402

UREG = sasm.UREG
COND = sasm.COND
TERM = sasm.TERM
M24 = 0xFFFFFF


def sx(v, bits):
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


def ureg(code):
    return UREG[code] if code < len(UREG) else f'UREG_{code:#x}'


def dreg(n, float_):
    return f'{"F" if float_ else "R"}{n}'


def fill(tmpl, **kw):
    for k, v in kw.items():
        tmpl = tmpl.replace('{' + k + '}', str(v))
    return tmpl


# ---------------------------------------------------------------- compute field
def compute_text(c):
    if c == 0:
        return ''
    n, x, y = c >> 8 & 15, c >> 4 & 15, c & 15
    if c >> 22:
        code = c >> 16 & 0x3F
        m = c >> 12 & 15
        xm, ym, xa, ya = c >> 6 & 3, 4 + (c >> 4 & 3), 8 + (c >> 2 & 3), 12 + (c & 3)
        if code == 0:
            return f'R{n} = {sasm.MRREG.get(m, "MR?")}'
        if code == 1:
            return f'{sasm.MRREG.get(m, "MR?")} = R{n}'
        if code in sasm.MULALU:
            return fill(sasm.MULALU[code], m=m, a=n, xm=xm, ym=ym, xa=xa, ya=ya)
        if code >> 4 in (2, 3):
            p = 'F' if code >> 4 == 3 else 'R'
            mul = f'F{m} = F{xm} * F{ym}' if p == 'F' else f'R{m} = R{xm} * R{ym} (SSFR)'
            return f'{mul}, {p}{n} = {p}{xa} + {p}{ya}, {p}{code & 15} = {p}{xa} - {p}{ya}'
        return f'<multifunction {c:#08x}>'
    cu, op = c >> 20 & 3, c >> 12 & 0xFF
    if cu == 0:
        if op >> 4 in (7, 0xF):
            p = 'F' if op >> 4 == 0xF else 'R'
            return f'{p}{n} = {p}{x} + {p}{y}, {p}{op & 15} = {p}{x} - {p}{y}'
        if op in sasm.ALU:
            return fill(sasm.ALU[op], n=n, x=x, y=y)
        return f'<alu {op:#x}>'
    if cu == 1:
        t = sasm.mult_text(op)
        return fill(t, n=n, x=x, y=y) if t else f'<mult {op:#x}>'
    if cu == 2:
        t = sasm.SHIFT.get(op)
        if t is None:
            return f'<shift {op:#x}>'
        return fill(t.replace('{f}', 'R{y}').replace('{y}', 'R{y}'), n=n, x=x, y=y)
    return f'<compute {c:#08x}>'


def shimm_text(w):
    """An immediate shift: op6 from bits 21-16, 12-bit data from bits 30-27 and 15-8."""
    op6 = w >> 16 & 0x3F
    n, x = w >> 4 & 15, w & 15
    raw12 = (w >> 27 & 15) << 8 | (w >> 8 & 0xFF)
    t = sasm.SHIFT.get(op6 << 2)
    if t is None:
        return f'<shimm {op6:#x}>'
    if '{f}' in t:
        return fill(t, n=n, x=x, f=f'{raw12 & 0x3F}:{raw12 >> 6 & 0x3F}')
    return fill(t, n=n, x=x, y=sx(raw12, 12))


def cond_prefix(cc):
    return '' if cc == 31 else f'IF {COND[cc]} '


def with_compute(prefix, c, tail):
    ct = compute_text(c)
    parts = [p for p in (ct, tail) if p]
    return prefix + ', '.join(parts) if parts else prefix.rstrip()


# ---------------------------------------------------------------- one instruction
class Insn:
    __slots__ = ('addr', 'word', 'text', 'const', 'target', 'call', 'ends')

    def __init__(self, addr, word):
        self.addr, self.word = addr, word
        self.const = None       # a 32-bit constant or address the instruction carries
        self.target = None      # a branch or loop-end target
        self.call = False
        self.ends = False       # RTS/RTI or unconditional JUMP: a flow boundary
        self.text = decode(self)


def mem(space, i, m, pre=False):
    return f'{space}(M{m}, I{i})' if pre else f'{space}(I{i}, M{m})'


def decode(ins):
    w, pc = ins.word, ins.addr
    top = w >> 45
    op8 = w >> 40
    cc = w >> 33 & 31
    cmp_ = w & 0x7FFFFF
    if top == 1:                                            # type 1: compute, DM and PM moves
        dst, di, dmm, pst = w >> 44 & 1, w >> 41 & 7, w >> 38 & 7, w >> 37 & 1
        dr, pi, pmm, pr = w >> 33 & 15, 8 + (w >> 30 & 7), 8 + (w >> 27 & 7), w >> 23 & 15
        fl = bool(cmp_) and (cmp_ >> 22 or cmp_ >> 20 & 3 == 0 and cmp_ >> 19 & 1) and 'F' or 'R'
        d = f'DM(I{di}, M{dmm}) = {fl}{dr}' if dst else f'{fl}{dr} = DM(I{di}, M{dmm})'
        p = f'PM(I{pi}, M{pmm}) = {fl}{pr}' if pst else f'{fl}{pr} = PM(I{pi}, M{pmm})'
        return with_compute('', cmp_, f'{d}, {p}')
    if top == 2:                                            # type 3: ureg <-> DM/PM, register modify
        post, i, m, g, st, lw, u = w >> 44 & 1, w >> 41 & 7, w >> 38 & 7, w >> 32 & 1, w >> 31 & 1, w >> 30 & 1, w >> 23 & 0x7F
        i, m = i + 8 * g, m + 8 * g
        a = mem('PM' if g else 'DM', i, m, pre=not post)
        r = ureg(u)
        mv = f'{a} = {r}' if st else f'{r} = {a}'
        if lw:
            mv += ' (LW)'
        return with_compute(cond_prefix(cc), cmp_, mv)
    if top == 3 and not w >> 44 & 1:                        # type 4: dreg <-> DM/PM, immediate modify
        i, g, st, post, d6, r = w >> 41 & 7, w >> 40 & 1, w >> 39 & 1, w >> 38 & 1, sx(w >> 27 & 0x3F, 6), w >> 23 & 15
        i += 8 * g
        sp = 'PM' if g else 'DM'
        a = f'{sp}(I{i}, {d6})' if post else f'{sp}({d6}, I{i})'
        mv = f'{a} = R{r}' if st else f'R{r} = {a}'
        return with_compute(cond_prefix(cc), cmp_, mv)
    if top == 3 and not w >> 43 & 1:                        # type 5: ureg <-> ureg
        s = (w >> 38 & 0x1F) << 2 | w >> 31 & 3
        d = w >> 23 & 0x7F
        return with_compute(cond_prefix(cc), cmp_, f'{ureg(d)} = {ureg(s)}')
    if top == 3:                                            # swap
        return with_compute(cond_prefix(cc), cmp_, f'R{w >> 23 & 15} <-> S{w >> 38 & 15}')
    if top == 4 and w >> 44 & 1:                            # type 16: constant -> DM/PM
        i, m, g = w >> 41 & 7, w >> 38 & 7, w >> 37 & 1
        ins.const = w & 0xFFFFFFFF
        return f'{mem("PM" if g else "DM", i + 8 * g, m + 8 * g)} = {w & 0xFFFFFFFF:#x}'
    if top == 4:                                            # type 6: immediate shift with a move
        i, m, g, st, r = w >> 41 & 7, w >> 38 & 7, w >> 32 & 1, w >> 31 & 1, w >> 23 & 15
        a = mem('PM' if g else 'DM', i + 8 * g, m + 8 * g)
        mv = f'{a} = R{r}' if st else f'R{r} = {a}'
        return f'{cond_prefix(cc)}{shimm_text(w)}, {mv}'
    if top == 5:                                            # type 15: ureg <-> DM/PM, 32-bit immediate modify
        g, i, st, lw, u = w >> 44 & 1, w >> 41 & 7, w >> 40 & 1, w >> 39 & 1, w >> 32 & 0x7F
        v = sx(w & 0xFFFFFFFF, 32)
        ins.const = w & 0xFFFFFFFF
        a = f'{"PM" if g else "DM"}({v:#x}, I{i + 8 * g})'
        mv = f'{a} = {ureg(u)}' if st else f'{ureg(u)} = {a}'
        return mv + (' (LW)' if lw else '')
    if top:
        return f'<type {w:012x}>'
    # ---- the 000 group, by op8
    if op8 == 0x00:
        return 'IDLE' if w >> 39 & 1 else ('EMUIDLE' if w >> 38 & 1 else 'NOP')
    if op8 == 0x01:
        return cond_prefix(cc) + compute_text(cmp_)
    if op8 == 0x02:
        return cond_prefix(cc) + shimm_text(w)
    if op8 == 0x04:                                         # compute, MODIFY(I, M)
        g = 8 * (w >> 38 & 1)
        src, m, dx = g + (w >> 30 & 7), g + (w >> 27 & 7), w >> 24 & 7
        dst = src ^ dx
        mv = f'MODIFY(I{src}, M{m})' if dst == src else f'I{dst} = MODIFY(I{src}, M{m})'
        return with_compute(cond_prefix(cc), cmp_, mv)
    if op8 in (0x06, 0x07):                                 # direct jump/call
        call, la, db, ci = w >> 39 & 1, w >> 38 & 1, w >> 26 & 1, w >> 24 & 1
        t = (pc + sx(w & M24, 24)) & M24 if op8 == 0x07 else w & M24
        ins.target, ins.call = t, bool(call)
        ins.ends = cc == 31 and not call
        mods = [x for x, f in (('DB', db), ('LA', la), ('CI', ci)) if f]
        # sasm writes a PC-relative branch as (PC, absolute target)
        tgt = f'(PC, {t:#x})' if op8 == 0x07 else f'{t:#x}'
        return f'{cond_prefix(cc)}{"CALL" if call else "JUMP"} {tgt}' + (f' ({", ".join(mods)})' if mods else '')
    if op8 in (0x08, 0x09):                                 # indirect, or short relative, with compute
        call, la, db, els, ci = w >> 39 & 1, w >> 38 & 1, w >> 26 & 1, w >> 25 & 1, w >> 24 & 1
        mods = [x for x, f in (('DB', db), ('LA', la), ('CI', ci)) if f]
        if op8 == 0x08:
            i, m = 8 + (w >> 30 & 7), 8 + (w >> 27 & 7)
            tgt = f'(M{m}, I{i})'
        else:
            d = sx(w >> 27 & 0x3F, 6)
            ins.target = (pc + d) & M24
            tgt = f'(PC, {ins.target:#x})'
        ins.call = bool(call)
        ins.ends = cc == 31 and not call and op8 == 0x09
        head = f'{cond_prefix(cc)}{"CALL" if call else "JUMP"} {tgt}' + (f' ({", ".join(mods)})' if mods else '')
        ct = compute_text(cmp_)
        return head + (f', {"ELSE " if els else ""}{ct}' if ct else '')
    if op8 in (0x0A, 0x0B):
        db, els, lr = w >> 26 & 1, w >> 25 & 1, w >> 24 & 1
        mods = [x for x, f in (('DB', db), ('LR', lr)) if f]
        ins.ends = cc == 31
        head = f'{cond_prefix(cc)}{"RTI" if op8 == 0x0B else "RTS"}' + (f' ({", ".join(mods)})' if mods else '')
        ct = compute_text(cmp_)
        return head + (f', {"ELSE " if els else ""}{ct}' if ct else '')
    if op8 in (0x0C, 0x0D):
        end = (pc + sx(w & M24, 24)) & M24
        ins.target = end
        n = f'{w >> 24 & 0xFFFF}' if op8 == 0x0C else ureg(w >> 32 & 0x7F)
        return f'LCNTR = {n}, DO {end:#x} UNTIL LCE'
    if op8 == 0x0E:
        end = (pc + sx(w & M24, 24)) & M24
        ins.target = end
        return f'DO {end:#x} UNTIL {TERM[w >> 33 & 31]}'
    if op8 == 0x0F:
        ins.const = w & 0xFFFFFFFF
        return f'{ureg(w >> 32 & 0x7F)} = {w & 0xFFFFFFFF:#x}'
    if 0x10 <= op8 <= 0x13:
        g, st, lw, u = op8 >> 1 & 1, op8 & 1, w >> 39 & 1, w >> 32 & 0x7F
        ins.const = w & 0xFFFFFFFF
        a = f'{"PM" if g else "DM"}({w & 0xFFFFFFFF:#x})'
        mv = f'{a} = {ureg(u)}' if st else f'{ureg(u)} = {a}'
        return mv + (' (LW)' if lw else '')
    if op8 == 0x14:
        bop = {0: 'SET', 1: 'CLR', 2: 'TGL', 4: 'TST', 5: 'XOR'}.get(w >> 37 & 7, '?')
        ins.const = w & 0xFFFFFFFF
        return f'BIT {bop} {sasm._SREG7[w >> 32 & 15]} {w & 0xFFFFFFFF:#x}'
    if op8 == 0x16:
        br, g = w >> 39 & 1, 8 * (w >> 38 & 1)
        src = g + (w >> 32 & 7)
        dst = src ^ (w >> 35 & 7)
        v = sx(w & 0xFFFFFFFF, 32)
        ins.const = w & 0xFFFFFFFF
        op = 'BITREV' if br else 'MODIFY'
        return f'{op}(I{src}, {v})' if dst == src else f'I{dst} = {op}(I{src}, {v})'
    if op8 == 0x17:
        bits = w >> 33 & 0x7F
        ops = [sasm.STACKOP[k] for k in range(7) if bits >> (6 - k) & 1]
        return ', '.join(ops) or 'NOP'
    if op8 == 0x18:
        rel = w >> 38 & 1
        t = (pc + sx(w & M24, 24)) & M24 if rel else w & M24
        ins.target = t
        db = w >> 34 & 1 or w >> 26 & 1
        return f'CJUMP {f"(PC, {t:#x})" if rel else f"{t:#x}"}' + (' (DB)' if db else '')
    if op8 == 0x19:
        return 'RFRAME'
    return f'<op {w:012x}>'


# ---------------------------------------------------------------- program
class Program:
    def __init__(self, fw_path):
        self.img = Image(fw_path)
        self.insns = {a: Insn(a, w) for a, w in sorted(self.img.pm.items())}
        self.xrefs = {}
        self.calls = {}
        for ins in self.insns.values():
            if ins.const is not None:
                self.xrefs.setdefault(ins.const, []).append(ins.addr)
            if ins.target is not None:
                self.calls.setdefault(ins.target, []).append(ins.addr)
        self.func_starts = sorted({i.target for i in self.insns.values() if i.call and i.target is not None})

    def fmt(self, ins):
        return f'{ins.addr:#08x}: {ins.word:012x}  {ins.text}'

    def func(self, start, max_insns=2000):
        out = []
        a = start
        while a in self.insns and len(out) < max_insns:
            ins = self.insns[a]
            out.append(ins)
            if ins.ends:
                # a delayed branch runs two more instructions
                if '(DB' in ins.text or ', DB' in ins.text:
                    for k in (1, 2):
                        if a + k in self.insns:
                            out.append(self.insns[a + k])
                # keep going only if something later in the function jumps past here
                later = [t for s in out for t in [s.target] if t is not None and t > a]
                if not later:
                    break
            a += 1
        return out


def parse_addr(s):
    return int(s, 16)


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    fw, cmd, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    prog = Program(fw)
    if cmd == 'verify':
        bad = 0
        unknown = 0
        for ins in prog.insns.values():
            text = ins.text.split('  ;')[0]
            if text.startswith('<'):
                unknown += 1
                if unknown <= 20:
                    print(f'UNKNOWN {prog.fmt(ins)}')
                continue
            try:
                encs = sasm.encode(text, sasm.Ctx(pc=ins.addr))
            except sasm.AsmError as e:
                encs = [f'error: {e}']
            if ins.word not in encs:
                bad += 1
                if bad <= 40:
                    print(f'MISMATCH {prog.fmt(ins)}  ->  {[e if isinstance(e, str) else f"{e:012x}" for e in encs][:3]}')
        print(f'{len(prog.insns)} instructions, {bad} mismatches, {unknown} undecoded')
    elif cmd == 'dis':
        a = parse_addr(args[0])
        n = int(args[1], 0) if len(args) > 1 else 40
        for k in range(n):
            if a + k in prog.insns:
                print(prog.fmt(prog.insns[a + k]))
    elif cmd == 'func':
        for ins in prog.func(parse_addr(args[0])):
            print(prog.fmt(ins))
    elif cmd == 'xrefs':
        for s in args:
            v = parse_addr(s)
            for a in prog.xrefs.get(v, []):
                print(f'{v:#x} <- {prog.fmt(prog.insns[a])}')
    elif cmd == 'callers':
        t = parse_addr(args[0])
        for a in prog.calls.get(t, []):
            print(prog.fmt(prog.insns[a]))
    elif cmd == 'funcs':
        for f in prog.func_starts:
            print(f'{f:#08x}  {len(prog.calls[f])} callers')
    elif cmd == 'iop':
        for ins in prog.insns.values():
            if ins.const is not None and ins.const < 0x4000 and ('DM(' in ins.text or 'PM(' in ins.text):
                print(prog.fmt(ins))
    elif cmd == 'dm':
        a = parse_addr(args[0])
        n = int(args[1], 0) if len(args) > 1 else 16
        for k in range(0, n, 4):
            ws = [prog.img.dm.get(a + k + j) for j in range(4)]
            print(f'{a + k:#08x}: ' + ' '.join('--------' if v is None else f'{v:08x}' for v in ws))
    elif cmd == 'dump':
        for ins in prog.insns.values():
            print(prog.fmt(ins))
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
