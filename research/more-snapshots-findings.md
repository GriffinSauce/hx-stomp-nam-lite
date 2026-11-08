# How the HX Stomp limits snapshots to 3

Research notes for `research/more-snapshots-prompt.md`, against stock
firmware 3.80.0 (`HX_Stomp_v3.80.0_0x103966115.hxf`, MD5
`a2cd8f4ae56f49415380270ebc484bf1`). Everything below is about Entry 0, the
Cortex-M4 application, unless stated otherwise. Addresses are Entry 0 load
addresses; `tools/research/snapshot_sites.py` prints the payload offsets and
instruction bytes for every site from the owner's own firmware file, and
`tools/research/e0_xref.py` is the cross-reference helper we used.

Nothing here was tested on hardware.

## 1. Summary

**The limit is compiled in, not configured.** There is no device capability
table. The snapshot count is a set of immediates spread over the preset class
and the code around it, and, more importantly, the in-RAM `Preset` object
embeds exactly three `Snapshot` structs with every later field addressed by a
fixed offset.

- Raising the count to 6 is possible in principle (no checksum or signature
  stands in the way, and the storage format is variable-size), but it is not a
  small patch. It needs the `Preset` object to grow by `0x720` bytes, which
  means rebasing roughly 140 field accesses inside the preset class and about
  35 outside it, plus about 25 loop bounds, encoder and decoder changes, and
  UI work. We rate our chance of getting that right by hand-patching, without
  a decompiler and without hardware iteration, as **low**.
- The fallback route (recall a neighbouring preset's snapshot without a
  preset change) is tractable: the recall function takes a `Preset*` and an
  index and applies bypass states and parameter values to the live engine
  without rebuilding the graph. The missing piece is a second decoded
  `Preset` in RAM; the firmware keeps only one (the edit buffer), so the patch
  has to decode the neighbour itself. Details in section 5. Confidence that
  this route works: **medium**; it needs a hardware test.

Confidence levels: *certain* means read directly from the code; *high*,
*medium* and *low* are our judgement.

## 2. Where the 3 comes from

### The JSON keys do not exist in the firmware

Presets are not stored as JSON on the device. `snapshot0`, `@valid`,
`@current_snapshot` and the other names from the `.hxb` backup are produced
by HX Edit. The device stores and transmits **MessagePack with small integer
keys**; the factory presets in Entries 12 and 13 are in that form, and the
preset class contains a hand-written msgpack encoder (`0x28de3c9c`) and
decoder (`0x28de42c4`). This is why none of the strings from the prompt are
found; only `SNAPSHOT ` (the default name prefix, `0x28e3c5a8`) and the UI
labels (`Rename Snapshot`, `Snapshot\nBPM`, `Snpsht`, ...) are present.

Key map of a preset record, as far as we decoded it (certain for the
structure, names are our interpretation):

| key | content |
|---|---|
| 7 | meta: `{36: "P33", 35: version number, 37: "v2.92-457-ge30d971"}` (P33 is the Stomp's internal product name) |
| 0 | dsp0: `{21: topology, 22: [20 block slots]}` |
| 1 | dsp1: `nil` (the empty `dsp1: {}` in the backup) |
| 3, 4, 2 | footswitch / controller / cursor sections |
| 5 | global: `{16: tempo, 45..56, 30, 111, 112, ...}` |
| 6 | `{98: ..., 26: ...}` |
| 10 | snapshots: `{6: current snapshot, 7: previous, 8: controller count, 9: 20, 10: [snapshot, snapshot, snapshot], 13: [20 per-slot "snapshot bypass" flags]}` |

One snapshot is a fixmap of 8: `{0: valid, 1: 11×9-byte assignment records
(99-byte string), 2: 64 controller values (7 bytes each), 3: [20 bypass
states], 4: name, 5: tempo, 12: led colour, 14: custom name}`, about 507
bytes with factory defaults. `@valid` is key 0; the decoder and
`selectSnapshot` use it as described below.

### The Preset object

Constructor `0x28de3c08` (vtable `0x28de20e9`), `memset(this, 0, 0x9e4)`.
Layout (certain for the offsets we list; field names are ours):

| offset | field |
|---|---|
| `0x000` | vtable |
| `0x004` | lock (taken with `0x28e24c00`, released with `0x28e24bb0`) |
| `0x00c` | `Snapshot[3]`, each `0x260` bytes, ends at `0x72c` |
| `0x72c` | `u8[20]` per-slot "snapshot bypass enabled" flags (record key 13), initialised to 1 |
| `0x741` | controller count (key 8) |
| `0x742` | footswitch assignment count (key 7) |
| `0x743` | **current snapshot index** (key 6) |
| `0x744` | previous snapshot index |
| `0x748` | a fourth, scratch `Snapshot` (`0x260` bytes): backup/restore of the current snapshot, see below |
| `0x9a8`..`0x9ab` | flags (snapshot changed, loading, locked) |
| `0x9ac` | `u32` |
| `0x9e4` | end |

`Snapshot` (`0x260` bytes):

| offset | field |
|---|---|
| `0x000` | `u8[20]` bypass state per slot (key 3) |
| `0x028` | `64 × 7` bytes controller values (key 2) |
| `0x1e8` | `11 × 9` bytes assignment records (key 1) |
| `0x24b` | valid flag (key 0) |
| `0x24c` | custom name flag (key 14) |
| `0x250` | tempo, float (key 5) |
| `0x255` | name, `char[11]` (key 4) |

The scratch snapshot at `0x748` is written by `0x28de1810` (copy current
snapshot to `+0x748`, event `0x44115`) and read back by `0x28de184c`
(restore). It is the "Snapshot Edits: Discard" mechanism, and it cannot be
reused as a fourth user snapshot: index arithmetic everywhere is
`Preset + 0xc + i * 0x260`, which for `i = 3` gives `0x72c`, not `0x748`.

**Only one `Preset` object exists.** The constructor is called once, from
`0x28d96a6c` (at `0x28d96b12`); `0x28d960a4` returns that pointer (global at
`0x2812c630`), and the import path calls it the "Edit Buffer"
(`0x28e3db04`). The setlist manager (`0x28df3798` ff.) advances pointers by
`0x9e4` in a few places, but we did not establish that those are decoded
presets; they look like directory entries (confidence medium).

### No capability table

Searching for `0x210006` finds nothing; the device id is only assembled byte
by byte in the universal device inquiry reply (`0x28d9b992`: manufacturer
`00 01 0C`, family `0x0021`, member `0x0006`, version `0x5003`). The only
table-like data near the snapshot strings, `3, 6, 0, 4, 2, 1, 1, 1, 1, 0, 4`
at `0x28e3c5b4`, has no literal-pool reference anywhere in the code and we
could not tie it to the snapshot count (confidence low that it is one). The
hypothesis "shared Helix code with the limits as parameters" is confirmed for
the data format (eight-keyed snapshot maps, `dsp1`, the valid flag) and
rejected for the code: the counts are immediates.

### MIDI CC 69

The CC dispatcher is `0x28d9b120(cc, value)` (jump table at `0x28d9b138`,
indexed by `cc - 1`); CC 69 lands at `0x28d9b5a2`:

```
cmp r1, #2 ; bls -> selectSnapshot(current, value)
cmp r1, #8 ; beq -> next     (0x28d9b66a)
cmp r1, #9 ; bne -> ignore
           ; prev: current - 1, wrapping at 0 (0x28d9b6d8)
```

Values 3..7 and 10..127 are dropped, not clamped. The reserved-CC check
("This CC# is reserved!") is in `0x28d9b89c` and does not involve the count.

### Every use of the snapshot count

Output of `tools/research/snapshot_sites.py` on the stock file (0 mismatches):

| address | offset | bytes | instruction | what |
|---|---|---|---|---|
| `0x28de3c0a` | `0x07e9bd` | `40f6e412` | `movw r2, #0x9e4` | constructor: `sizeof(Preset)` (memset) |
| `0x28de3c56` | `0x07ea09` | `04f10c06` | `add.w r6, r4, #0xc` | constructor: `snapshot[0]` at `+0xc` |
| `0x28de3c82` | `0x07ea35` | `06f51876` | `add.w r6, r6, #0x260` | constructor: `sizeof(Snapshot)` |
| `0x28de3c80` | `0x07ea33` | `042d` | `cmp r5, #4` | constructor: name loop 1..3 |
| `0x28de1cf0` | `0x07caa3` | `042d` | `cmp r5, #4` | `initSnapshots` (`0x28de1c9c`): name loop 1..3 |
| `0x28de1e60` | `0x07cc13` | `b8f1040f` | `cmp.w r8, #4` | `resetSnapshots` (`0x28de1d04`): loop 1..3 |
| `0x28de432a` | `0x07f0dd` | `042c` | `cmp r4, #4` | decoder (`0x28de42c4`): pre-fill names 1..3 |
| `0x28de262c` | `0x07d3df` | `bef1030f` | `cmp.w lr, #3` | `removeController` (`0x28de2594`): loop over snapshots |
| `0x28de2674` | `0x07d427` | `bef1030f` | `cmp.w lr, #3` | `removeController`: second loop |
| `0x28de26ae` | `0x07d461` | `bef5e46f` | `cmp.w lr, #0x720` | `removeController`: third loop, `3 * 0x260` |
| `0x28de29e8` | `0x07d79b` | `bbf1030f` | `cmp.w fp, #3` | `addController` (`0x28de285c`): loop over snapshots |
| `0x28de2a80` | `0x07d833` | `bbf1030f` | `cmp.w fp, #3` | `addController`: second loop |
| `0x28de2c9e` | `0x07da51` | `b8f1030f` | `cmp.w r8, #3` | `clearController` (`0x28de2ba4`): loop over snapshots |
| `0x28de3d70` | `0x07eb23` | `9323` | `movs r3, #0x93` | encoder (`0x28de3c9c`): msgpack `fixarray(3)` header |
| `0x28de3f64` | `0x07ed17` | `b5f5e46f` | `cmp.w r5, #0x720` | encoder: snapshot loop end |
| `0x28de420c` | `0x07efbf` | `b5f5e46f` | `cmp.w r5, #0x720` | encoder: snapshot loop end (second exit) |
| `0x28de2fdc` | `0x07dd8f` | `bbf1020f` | `cmp.w fp, #2` | `selectSnapshot` (`0x28de2f90`): index > 2 rejected (`-0x2e`) |
| `0x28de329c` | `0x07e04f` | `022d` | `cmp r5, #2` | `selectSnapshot`: previous index must be <= 2 to copy from |
| `0x28d9b5a2` | `0x036355` | `0229` | `cmp r1, #2` | CC 69: values 0..2 select |
| `0x28de59e6` | `0x080799` | `0228` | `cmp r0, #2` | footswitch snapshot command (`0x28de59a8`): next wraps after 2 |
| `0x28dd162e` | `0x06c3e1` | `0228` | `cmp r0, #2` | UI (`0x28dd14a0`): next wraps after 2 |
| `0x28dcfe7e` | `0x06ac31` | `0128` | `cmp r0, #1` | UI (`0x28dcfc30`): increment only while index <= 1 |
| `0x28de441e` | `0x07f1d1` | `03f00f03` | `and r3, r3, #0xf` | decoder: count from `fixarray` header, **no clamp** |
| `0x28de4cb2` | `0x07fa65` | `9b78` | `ldrb r3, [r3, #2]` | decoder: count from `array16`, **no clamp** |
| `0x28de1824` | `0x07c5d7` | `00f5e961` | `add.w r1, r0, #0x748` | `backupSnapshot` (`0x28de1810`) |
| `0x28de1860` | `0x07c613` | `00f5e962` | `add.w r2, r0, #0x748` | `restoreSnapshot` (`0x28de184c`) |

Not in the table but part of the same limit: the roughly 90 `#0x260`
stride uses in `0x28de1888`..`0x28de4d6a` (fine as they are, the stride does
not change), the field offsets `>= 0x72c` (about 140 instructions in the
preset class `0x28de12d4`..`0x28de5a30`, and these outside it: `0x28d9b120`
CC handler, `0x28d9b89c`, `0x28dbf0a0`, `0x28dbfe7c`, `0x28dc0ca4`,
`0x28dc1e6c`, `0x28dc9d74`, `0x28dca558`, `0x28dcc4ba`, `0x28dcfa3c`,
`0x28deb4e8`, `0x28df3f14`, `0x28df5ccc`, `0x28df7208`, `0x28e002fc`,
`0x28e1bf80`), and the footswitch command encoding in `0x28de59a8`
(assignment byte at `+0x81`: 1 = next, 2 = previous, `3 + n` = snapshot n).

Other `cmp #3` / `cmp #4` sites in the class (`0x28de2742`, `0x28de28ce`,
`0x28de379a`, `0x28de1f2e`, `0x28de3148`, ...) compare block or controller
*types*, not snapshot indices; we checked each one.

## 3. Storage

**Variable-size records in a block store, no fixed snapshot slots.**

- `0x28df0f0c` creates three record stores with `0x28daa458`: `Presets`
  (`0x28e3da58`), `Raw Presets` (`0x28e3da54`) and `Factory Presets`
  (`0x28e3da60`), each with a 512-byte sector size and a `0x200`-sector
  block size; `SetlistDirectory` (`0x28df1d24`) sits on top. The exact
  capacity per store (`0x80400` sectors or bytes) we did not pin down.
- A record is `"l6-helix\0"` (msgpack fixstr), a 48-byte string holding 12
  `u32` section offsets (one per top-level key, so a reader can seek to a
  section without parsing the whole map; `0x28deff58` is the header reader),
  then the msgpack map. Records are read whole into a `0x5000`-byte (20 KiB)
  buffer (`0x28def9a8` from `0x28deb4e8`).
- Entry 12 (341 KiB) is the factory `Presets` bundle: an `array16` of 1026
  records (1024 slots plus two), 122 non-empty in slots 0..125, 2.2 to 3.4 KB
  each. Entry 13 (930 KiB) is the factory `Raw Presets` bundle: the same
  slots, 7.8 KB each, holding a 5805-byte compiled DSP image under key 8
  **plus the same snapshots section** (3 snapshots per record). "Rebuilding
  Presets..." regenerates the raw form from the source form.
- Snapshots cost about 507 bytes each in encoded form. Six instead of three
  adds about 1.5 KB to both records of a preset; both stay far below the
  20 KiB read buffer. Reducing the preset count frees nothing that matters.

Confidence: high for the record format and the sizes, medium for the store
capacity statement.

## 4. Memory at runtime

- The snapshot array is sized for **3**, not 8: `Preset` is `0x9e4` bytes,
  `3 * 0x260 = 0x720` of which are snapshots at `+0xc`, immediately followed
  by other fields. The scratch snapshot at `+0x748` is the only spare and is
  in use.
- One `Preset` is allocated (the edit buffer). Growing it by `0x720` bytes is
  not a memory problem; rebasing every access to fields past `0x72c` is the
  problem.
- Other state sized by the count: the footswitch command encoding (`3 + n`),
  the UI "next/previous" wrap logic, per-snapshot LED colour (key 12) and the
  `Snpsht` footswitch mode labels. We did not find a separate undo buffer or
  HX Edit-side list sized by it; the HX Edit protocol carries whole records.

## 5. The candidate patches

### 5a. Raising the count to 6 (not recommended)

The exact sites that would change:

1. `sizeof(Preset)`: `0x28de3c0a` `movw r2, #0x9e4` to `#0x1104`.
2. Every field access at an offset `>= 0x72c` on a `Preset` pointer, rebased
   by `+0x720`: about 140 instructions in `0x28de12d4`..`0x28de5a30` and the
   external users listed in section 2. The Thumb-2 encodings allow it in
   place (12-bit offsets reach `0xfff`, and `addw`/`movw` reach `0xfff` /
   `0xffff`), but the offsets are plain immediates and nothing marks which
   base register is a `Preset`; every site has to be judged by hand. The
   setlist manager's `0x9e4` strides would need the same judgement.
3. Loop bounds: the 16 `cmp #3/#4/#0x720` rows in the table (`#4` to `#7`,
   `#3` to `#6`, `#0x720` to `#0xe40`).
4. Index bounds: the six `cmp #2` / `cmp #1` rows (`#2` to `#5`, `#1` to `#4`).
5. Encoder header `0x93` to `0x96` at `0x28de3d70`. The decoder needs no
   change for count (it takes the count from the blob) but needs a clamp
   added, because today a record with more than 3 snapshots overflows the
   `Preset` object (into the flags at `+0x72c` and beyond).
6. UI: the footswitch command values (`3 + n`) are already open-ended, but
   the assignment menus and the snapshot list/rename screens enumerate 3; the
   `Snpsht` footswitch mode maps the three footswitches to snapshots 1..3 and
   would need paging to reach 4..6. MIDI CC 69 values 3..5 would work
   immediately after the index bound change.

Risks: preset corruption from a missed field rebase (silent, since the
msgpack decoder writes past the array), records with 6 snapshots crashing a
stock firmware (and HX Edit backups made from the patched unit becoming
unloadable on stock), and HX Edit rejecting or truncating the records (see
section 6). Confidence that a hand patch of this size is correct on the
first try: low.

### 5b. Fallback: virtual snapshots 4..6 from the next preset

Functions involved (certain):

- **Recall snapshot**: `int Preset::selectSnapshot(Preset* this, uint8 index)`
  at `0x28de2f90`. Returns 0 on success, `-0x2e` when `index > 2`. Steps:
  refuses while `+0x9a9` is set; posts message `0xc02` (begin, `0x28df0650`)
  and `0xd02` (end, `0x28df0684`) to the preset task; writes `+0x744` and
  `+0x743`; reads global parameter `0x4f`; event `0x44113`; if the target
  snapshot's valid flag (`+0x24b`) is clear it copies the previous snapshot
  into it with `0x28de12d4(this, src, dst, 0)`; otherwise it walks the 20
  slots and calls `0x28e0f884(engine, slot, enabled, 4)` for each bypass
  state, then walks the controller records and sets each parameter with
  `0x28e007ec` / `0x28e1f8f4`, then re-applies the footswitch assignments.
  No DSP graph rebuild. Callers: CC 69 (`0x28d9b5de`), footswitch command
  (`0x28de59d8`, `0x28de59fc`, `0x28de5a1e`), UI (`0x28dc12b8`,
  `0x28dcc554`, `0x28dcfe90`, `0x28dd1640`), preset load (`0x28deb8a0`),
  HX Edit commands (`0x28e1bfd2`, `0x28e1c0ee`).
- **Current preset**: `Preset* 0x28d960a4()` (global at `0x2812c630`).
- **Load preset N**: `0x28df0430(setlist, index)` posts message
  `{0x0301, setlist, index}` through `0x28df1bf4` to the preset task. The
  worker `0x28df22f8` to `0x28df2234(record)` reads the record
  (`0x28def7b4` / `0x28def8ac`) and calls `0x28deb4e8(Preset*)`, which
  decodes it into the edit buffer with `0x28de42c4(Preset*, blob, len, flag)`
  (at `0x28deb862`), rebuilds the DSP graph and finally selects the stored
  current snapshot (`0x28deb8a0`). `0x28df026c()` returns the current preset
  index; `0x28df024c(&out)` the current location. The CC handler's
  preset-up path (`0x28d9b62c`) wraps at 126 (`cmp r4, #0x7e`).
- **Decoder**: `0x28de42c4`. **Encoder**: `0x28de3c9c(Preset*, writer*,
  flag)`.

Because there is only one decoded `Preset` in RAM, the fallback needs its
own copy of the neighbour:

1. Reserve a second `Preset` (`0x9e4` bytes) and a `0x5000` read buffer in
   the unused SDRAM above `0x28e3fadc` (where the NAM patch already places
   its tables).
2. After a preset load (hook at the end of `0x28deb4e8`, or on the first
   CC 69 value `>= 3`), read preset `N+1`'s record the way `0x28df2234` does
   and run `0x28de42c4` on the spare object, then call `0x28de1c9c`-style
   initialisation only if the decoder expects it (it pre-fills the names
   itself at `0x28de4306`).
3. In the CC 69 handler, replace `cmp r1, #2; bls` with a branch to a stub:
   values 3..5 call `0x28de2f90(spare, value - 3)`; everything else falls
   through to the original code.

Risks and limits (medium confidence):

- `selectSnapshot` posts `0xc02`/`0xd02` and `0x44113` with the spare's
  index and sets the spare's `+0x743`; the UI keeps showing the current
  preset's snapshot index. Probably harmless, to be tested.
- Block identity: the recall addresses engine blocks by slot number and
  parameter id; preset `N+1` must be an exact copy of `N` in blocks and
  model choices, or parameters land on the wrong block. Nothing enforces
  this.
- Edits made while a virtual snapshot is active are recorded in the current
  preset's real snapshot (whichever `+0x743` says), not in the spare, and
  saving writes the current preset only. The virtual snapshots are therefore
  read-only; edit them in HX Edit as snapshots 1..3 of preset `N+1`, after
  which the spare has to be re-decoded (reload the preset).
- Snapshot-footswitch LEDs and the `Snpsht` mode know nothing about 4..6;
  only CC 69 reaches them.

The zero-risk variant is to let CC 69 values 3..5 load preset `N+1` through
`0x28df0430` and then select `value - 3`; that is a real preset change with
its audio gap, which the owner wants to avoid, but it is a few instructions.

## 6. HX Edit

- The device does not advertise a snapshot count. HX Edit reads the edit
  buffer as an `l6-helix` record built by `0x28e1c460` (`0x28e1c592`, via
  `0x28deacf4` to the 3-snapshot encoder), and sends records back that
  `0x28df1fa4` ("Edit Buffer") decodes with `0x28deb1f4` / `0x28de42c4`.
  Separate commands exist to select a snapshot (`0x28e1bfc0`: byte at
  `+0x10` to `selectSnapshot`) and to rename one (`0x28e1c02c` to
  `0x28de3888`). HX Edit most likely decides on 3 from the product id in the
  device inquiry reply (confidence medium).
- With a 6-snapshot firmware, HX Edit's reaction to records carrying six
  snapshot maps is unknown: it may ignore 4..6, refuse the preset, or show
  them. A backup `.hxb` written from such a unit would contain `snapshot3`
  and up and could not be restored onto stock firmware without stripping
  them, because the stock decoder has no clamp.
- The fallback route does not change the record format, so HX Edit is
  unaffected.

## 7. Open questions and hardware tests

1. Does `selectSnapshot` on a second `Preset` object actually drive the
   engine correctly (block slot and parameter ids are preset-relative, which
   our reading says is fine for an identical copy)? Test with the fallback
   stub and a copied preset.
2. What do messages `0xc02`/`0xd02` and event `0x44113` do with a snapshot
   index that belongs to another object; does the UI or the MIDI-out
   snapshot echo (CC 69 send) misbehave?
3. The store capacity (`0x80400`) units, in case anyone wants to go to 8
   snapshots on all 126 presets; not relevant at 6 snapshots on 6 presets.
4. The 11-word table at `0x28e3c5b4` next to `SNAPSHOT `: what reads it.
5. Whether the `0x9e4`-stride structures in the setlist manager hold decoded
   presets (if they did, the fallback would not need its own decode step).
6. HX Edit's behaviour with more than three snapshot maps per record, which
   only a test with a patched unit can answer.
7. Comparing with the Helix / HX Effects firmware (8 and 4 snapshots) would
   confirm the site list quickly: each site above should read `#8`/`#9` and
   `#4`/`#5` there. We did not have those files.
