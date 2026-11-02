# Prompt: find how the HX Stomp limits snapshots to 3

Give this whole file to a coding agent running locally, in a clone of this
repository, with the stock firmware file available.

---

## Context

This repository patches the stock Line 6 HX Stomp firmware 3.80.0
(`HX_Stomp_v3.80.0_0x103966115.hxf`, MD5 `a2cd8f4ae56f49415380270ebc484bf1`)
to add a NAM amp. Read `README.md` and skim `tools/` first. The firmware path
will be given to you; if not, ask for it.

The owner uses only about 6 of the 126 presets and wants **more than 3
snapshots per preset** (6 would be ideal). Giving up presets or memory is
fine. Your job is **research only**: find out how the limit of 3 is enforced
and what a minimal patch would be. Do not build or flash anything.

### What the repo already knows

- `tools/hxf.py unpack <in.hxf> <out.l6ia>` gives you the L6IA archive.
- `tools/l6ia.py` parses it. It has 15 entries; payloads are stored
  big-endian word-swapped. `patch_amp.get_entry_swapped(l6ia, idx)` returns
  a payload in its native little-endian form.
- **Entry 0 is the main application** (ARM, Thumb-2). It has a 0x34-byte header
  (magic `0x230b0c5e`, 7 segments). Each segment is `{len, load addr, next}`.
  `patch_amp._parse_e0_segments` and `_e0_vaddr_to_foff` map load addresses
  to offsets in the payload. Use them to load Entry 0 into a disassembler at
  the right addresses.
- Entry 2 is the SHARC DSP program, Entry 3 the model table and Entry 4 the
  DSP graph library. Snapshots most likely live only in Entry 0.

### What a full device backup (`.hxb`) shows

The `.hxb` is an `L6FA` container of zlib streams. The preset part is JSON,
`"schema": "L6Setlist"`, with 126 presets and device id `2162694`
(`0x210006`). Each preset's `tone` holds:

- `snapshot0`, `snapshot1`, `snapshot2`, never `snapshot3` or higher. Each one
  has `@name`, `@valid`, `@tempo`, `@pedalstate`, `@ledcolor`,
  `@custom_name`, `blocks.dsp0.blockN` (on/off) and
  `controllers.dsp0.blockN.<Param>.@value`.
- `global.@current_snapshot`.
- `controller.dsp0.blockN.<Param>.@snapshot_disable`, and
  `@no_snapshot_bypass` on each block.
- An **empty `dsp1: {}`**, although the Stomp has one DSP.

The `@valid` flag and the empty `dsp1` suggest code shared with the Helix,
which has 8 snapshots and 2 DSPs, with the device limits set somewhere as
parameters. That is the hypothesis to confirm or reject.

## Rules

- **Never commit Line 6 code or data**: no firmware bytes, extracted entries,
  disassembly listings or decompiled code. Commit only your own scripts,
  notes, offsets and short descriptions (an instruction's address and
  encoding is fine).
- Don't write to the device, run Line 6 Updater, or produce a `.hxf`.
- Put temporary files outside the repo, or in a git-ignored folder.
- Give a confidence level for every conclusion that isn't certain.

## Questions to answer

1. **Where does "3" come from?**
   - Find the strings `snapshot`, `@current_snapshot`, `@valid`,
     `@snapshot_disable` and `@no_snapshot_bypass`, and the code that refers
     to them. In particular, find the loop that writes and reads `snapshotN`
     in the JSON, and its bound.
   - Look for a device capability table or struct. It might be keyed by
     device id (`0x210006` and its siblings), with values such as 3
     snapshots, 1 DSP, 126 presets (`0x7e`), footswitch count and so on.
   - Find the MIDI CC 69 handler. On the Stomp, values 0–2 select a snapshot,
     8 = next and 9 = previous. Check how it clamps the value.
   - List **every** place the snapshot count is used: a constant,
     `cmp`/`it` bounds, `movs rN,#3`, loads from a capabilities struct, and
     array sizes in memset, malloc or `new`.
2. **Storage.** How are presets stored on the device?
   - Is it a filesystem with paths or JSON files, or fixed-size binary
     records? Look for path strings and file-format magic.
   - If the records are fixed-size: what is the snapshot array dimension, how
     big is one record, and would fewer presets free space?
   - If the storage is variable-size (JSON-like), extra snapshots cost no
     fixed slot.
3. **Memory at runtime.** How big is the in-RAM preset or snapshot structure?
   Is the snapshot array sized for 8, with only 3 used, or sized for 3?
   Which other buffers are sized by the snapshot count (UI lists, LED state,
   undo, the HX Edit protocol)?
4. **UI and footswitches.** How are snapshots picked on the device itself
   (footswitch snapshot mode, menus), and what would show snapshots 4–6?
   Paging, or MIDI CC 69 values 3–5 only?
5. **HX Edit protocol.** Does the device send or validate the snapshot count
   when HX Edit connects, saves or backs up? What would HX Edit do with
   presets that have more than 3 snapshots: drop them, reject them, or pass
   them through?
6. **Fallback route, in case raising the count is too invasive.** Find:
   - the "recall snapshot N" function. It applies block on/off states and
     parameter values without rebuilding the DSP graph;
   - the "load preset N" function.

   Give their addresses, arguments and callers. The idea: when snapshot 4 is
   selected, call the recall code with the values from the **next preset's**
   snapshot 0. That preset is an identical copy, so the blocks match, and no
   preset change happens.

## Method tips

- A disassembler with Thumb-2 support works well, for example Ghidra in
  headless mode or `capstone` (`pip install capstone`). Load each Entry 0
  segment at its load address and follow string references.
- Optional but valuable: compare with the same functions in other HX firmware
  that the owner can download for free from Line 6. Helix Floor/LT have 8
  snapshots, HX Effects 4 and HX Stomp XL its own count. Where the Stomp says
  3, the Helix 8 and HX Effects 4, you have found the constant. Ask the owner
  before relying on these files.

## Deliverable

Write `research/more-snapshots-findings.md` with:

1. A short summary: is "raise the count" feasible, at what size, and how
   confident are you?
2. A table of every use of the snapshot count: Entry 0 load address, payload
   offset, instruction, and what it does.
3. The storage answer from question 2, and whether reducing presets frees
   anything.
4. A candidate minimal patch for 6 snapshots. List the exact sites to change
   and the risks: preset corruption, HX Edit, UI.
5. The fallback route from question 6, with the addresses found.
6. Open questions, and what would need a test on hardware.

Commit any analysis scripts you write under `tools/research/`. They must read
from the owner's own firmware file and must not embed its bytes. Commit the
findings file too, then push the branch.
