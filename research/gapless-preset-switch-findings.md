# Hiding the preset-switch gap with a reverse loop

Findings for `research/gapless-preset-switch-prompt.md`, against stock HX
Stomp 3.80.0 (`HX_Stomp_v3.80.0_0x103966115.hxf`, MD5
`a2cd8f4ae56f49415380270ebc484bf1`). ARM addresses are Entry 0 load
addresses (Cortex-M4). SHARC addresses are 48-bit (ISA) program addresses
`0x8c000..` or short-word (VISA) addresses `0x17xxxx` / `0x60xxxx`, as the
tools below print them; `DM(x)` is a 32-bit data word. Function names are
ours. *Certain* means read from the code; *high*, *medium* and *low* are our
judgement. Nothing was run on hardware.

Tools written for this (all under `tools/research/`, all read the owner's
own firmware file): `sharc_ldr.py` (the SHARC boot stream as memory images),
`sharc_dis.py` (48-bit ISA disassembler with cross-references; `verify`
round-trips every stock instruction through `tools/sharc/sasm.py`),
`sharc_visa.py` (the 16/32/48-bit VISA code that holds most of the DSP
program, 99% of it reached), `measure_gap.py` (the hardware measurement),
`e0_xref.py` (the ARM cross-referencer from the snapshot research). The
prototype is `tools/sharc/gapfill_kernel.asm` with `tools/sharc/gapfill.py`.

## 1. Summary

**The fill is feasible and has a clean place to live.** Confidence high for
the mechanism, medium for the exact buffer and slot numbers, which need one
hardware check.

- The gap is explicit and the ARM drives it: it mutes the DSP's output through
  a parameter ramp (7.5 ms by default), **sleeps 11 ms**, sends DSP command
  `0x0f` ("hold"), tears the chain down and rebuilds it with messages over
  SPI, sends `0x0f` again to release the hold and unmutes. Nothing waits for
  the DSP; the gap is the sleep plus the time the DSP needs for the messages,
  plus the two ramps.
- On the SHARC, one RTOS task processes audio: it waits for the codec DMA
  block (16 frames), fetches the buffer halves of 8 DMA channels, runs the
  graph (ISA runner `0x8cb20`) **only if the graph is valid and processing is
  enabled**, and loops. While the chain is being rebuilt the graph is
  skipped, and the codec transmit buffer keeps whatever was in it (silence
  by then). The task runs every block no matter what: that is the hook.
- Command `0x0f` lands in `DM(0xc4cf5)`, a global hold flag several stock
  kernels already consult. It is the gap signal: 1 from hold to release.
- Memory: a 2 × 8192-frame ring (170 ms stereo, float) fits in DDR2 by
  moving the end of the DSP's big heap down by 64K words (two immediates in
  the heap setup). Internal RAM has no room left after the NAM kernel.
- Cycles: the prototype kernel costs 762 cycles per 16-frame block in the
  simulator, about 0.5% of a block.
- Prototype: `gapfill_kernel.asm` runs in `ssim.py` on the codec's TDM block
  format and matches its Python reference word for word on every block of
  every test signal. With the loop's fade matched to the mute ramp, the
  largest sample-to-sample jump of the filled output equals the signal's own
  slope: no discontinuity is added.

## 2. The preset change on both processors

### 2.1 ARM (Entry 0)

Transport: SSP0 (`0x40083000`), ARM is SPI master at 20 MHz, 16-bit words,
GPDMA, continuous full-duplex 1 KB frames pumped by task "F" (`0x10000cf4`);
a frame is about 0.4 ms, so a message reaches the DSP within one frame
(certain for SSP0/DMA/frames, high for the rate). Frame: `0xfacef00d`,
`seq<<24 | ack<<16 | words`, messages (`type<<24 | words`, tag, payload),
trailer `0xd00fecaf`. Types: `0x05` one parameter `{handle, id, value}`,
`0x28` parameter batch, `0x21` command `{cmd}` + payload, `0x03` Entry 4
model blob (sent once per block type, cached on the DSP), `0x18` boot image,
`0x08` synchronous request (only the init handshake and HX Edit queries wait
for a reply). There is no "load graph image": the DSP gets instantiate,
connect, parameter and bypass messages.

Preset load `0x28deb4e8(Preset*)`, in order (certain):

1. `0x28e007f8(engine, 1)` = suspend: `0x28dfa504` → `0x28dfa434` writes two
   **mute** parameters (value 1) on the chain's I/O block, then sleeps
   **11 ms** (`0x28e24c58(0x2af8)`, microseconds; the constant is the `movw
   r0, #0x2af8` at `0x28dfa49e`); `0x28da7c84(1)` holds the send queue; the
   first suspend level sends **command `0x0f` with payload `{2, 1}`**
   (`0x28e00886..0x28e008a2` → `0x28df3608` → `0x28da865c`).
2. Sections of the record are decoded (no DSP traffic).
3. Teardown: `0x28e022d4` removes every block (`0x28df4a98`, one "enabled"
   parameter write and a remove per block), slot 0 is re-created
   (`0x28e03b1c`, `0x28e03f38(C, 0, 0x101)`), relinked (`0x28e0ecdc`).
4. Blocks are added (`0x28e03f38` → `0x28dfcdd0`: blob on a cache miss, then
   connect `0x28df47a8`), parameters set, footswitch and controller
   assignments decoded, the snapshot section decoded (`0x28de42c4`) and
   `selectSnapshot` `0x28de2f90` applied (bypass + parameters, batched).
5. `0x28e007f8(engine, 0)` = resume: command `0x0f` with `{2, 0}`
   (`0x28e00820..0x28e00840`), `hold(0)` flushes the queue, the two mute
   parameters are written back to 0.

Everything after the sleep is fire-and-forget. The snapshot path
(`0x28de2f90`) sends only parameter batches: no mute, no teardown, no gap.
There is no double-buffered graph on the ARM (one graph handle at device
`+0x250`, one generation counter); the only ARM-side levers are the 11 ms
constant and the order of the calls.

### 2.2 SHARC (Entry 2)

The program is 11,926 ISA instructions (`0x8c000..0x8ee96`: vectors, RTOS
primitives, the graph runner, the DSP kernels) plus about 170k parcels of
VISA C code (internal blocks 0–2 and DDR2 `0x300000..0x30d06f`, seen as
`0x600000..`). A small RTOS: semaphores `0x8eaf4` (wait) / `0x8eb15`
(signal), interrupt masking `0x8ecd0`/`0x8ecd4`, software interrupt SFT3I
(`0x8ed2e`) for the scheduler, tasks created by `0x8ebe4`.

**Audio task** (entry `0x1763a4`, created at `0x60979b` with priority 0x5a,
task object `0xc4cf8`), one iteration per 16-frame DMA block (certain):

| step | where | what |
|---|---|---|
| wait | `0x1763a4` | `sem_wait(0xc5b3c)`; the semaphore is signalled by `0x176394`, the DMA-done callback registered through `0x1760c9` (`DM(0xc4cd0)`), reached from the P10I/P11I handlers `0x17602e` / `0x175f93` (the DMA dispatcher with callbacks at `0xc4cc8`/`0xc4cca`) |
| lock | `0x1763ae` | mutex `0x9d61f` (`0x174148` lock / `0x174159` unlock), shared with the graph editing functions |
| buffers | `0x1763c2..0x1763f2` | `0x176228(channel)` for channels 0, 1 → `DM(0xc5b44)`, `DM(0xc5b45)` and 2..7 → `DM(0xc5b4c..0xc5b51)`: the pointer to the DMA buffer half that is not being transferred (position from the SPORT DMA register `0x440`, buffer table `0xe1492` of 4 words per channel, filled at boot by `0x17615a`) |
| run | `0x1763f5..0x176405` | if `DM(0xc4d14) != 0` (graph valid) and `DM(0xc4cf7) != 0` (processing enabled) then `CJUMP 0x8cb20` at `0x176463`, the ISA graph runner, which walks the member list calling each primitive with I0 = its parameter block, I1 = input, I2 = output |
| after | `0x176408..0x17645c` | frame counter `0xc5b52/53`, unlock, every `DM(0xc4d13)` frames signal the semaphore in `DM(0xc4d12)` (a lower-priority task), loop |

The codec side: four SPORT register blocks at `0x400`, `0x800`, `0xc00`,
`0x4800` (control at `+0`, divider `+1`, DMA chain pointers at
`+0x43/+0x47/+0x4b/+0x4f`), programmed by `0x1760ed`/`0x60925b` from TCBs in
the channel table. Eight DMA channels; the App graph's port primitives read
and write them in Q31 (registry ids `0x2a94..0x2aac`, ISA entries
`0x8cdbb..0x8ce39`): inputs convert with `FLOAT BY -31` into the graph's
16-float blocks; **the main output primitive `0x2aac` (`0x8ce11`) writes
channel 1 (`DM(0xc5b45)`), a TDM buffer of 8 slots per frame** (slot from its
parameter, stride 8, `CLIP BY 0x3f7ffffe`, `FIX BY 31`, bit 0 used as a flag;
`0x2aa8/0x2aaa/0x2aab` write channels 5, 6, 7). Which slots carry main L/R
and which the sends and phones is set by the App's `outputSelect` parameters
and has to be confirmed on hardware (medium: slots 0 and 1).

**Mute and fade** (certain): the top-level graph is Entry 3 model 0
`HelixStomp_App` (Entry 4 blob 1). Its parameters include `mute` (slot 38),
`muteRampTimeMS` (slot 39, default 7.5, max 50), `muteOutput` (44),
`masterVolume` (46), `inputGain`/`outputGain`. `mute` writes pid 1 of the
ramp primitive `0x2713` instances in the I/O composites; the ramp kernel is
ISA `0x8d7e8`: per sample `y = x * g; g += (target - g) * (1 - c)`, a
one-pole, with `c` from `DM(1, I0)`, which the primitive's control function
`0x60f636` computes from the ramp time (its exact formula was not decoded;
the related fixed-coefficient primitives `0x80`/`0x225` use `0x3f7f62b7` =
0.9976, time constant 8.7 ms). The ramp runs inside the graph, so it only
acts while the graph runs; once the chain is torn down the transmit buffer is
simply not refilled.

**Commands** (certain): the message dispatcher `0x17724a` handles commands
`0x0f..0x12` through the table at `0xc5b9c`: `0x0f` → `0x177566` →
`DM(0xc4cf5) = payload[0]`, `0x10` → `DM(0xc4cf6)`, `0x12` → `0x1775bd` →
`DM(0xc4cf7)` (processing enable), `0xf0/0xf1` the IR slot commands. So the
ARM's hold command sets **`DM(0xc4cf5)`, read by kernels `0x8ceba`,
`0x8d355`, `0x8d7ef`, `0x8dc1c`, `0x8dcf3`, `0x8dd0a`, `0x8e154`** (the
ramp variant at `0x8d7ee` forces its target to 1 when the flag is clear).

**Graph edits**: `0x17646d`, `0x1764a8`, `0x17652a` take the mutex, clear
`DM(0xc4d14)`, build (`0x178990` / `0x1789e4`), store the new graph in
`DM(0xc4d14)` and unlock. Between messages the graph runs if valid; during
each add or remove it is skipped for a few blocks.

### 2.3 Timing, measured on the unit

`tools/research/record_gap.py` plays a 440 Hz tone into the Stomp over USB
(preset 018 takes USB 5/6 as its input), records USB out 1/2 and sends MIDI
on the unit's channel. Switching between presets 018 and 019, two identical
copies, five times (`--midi-channel 2 --out-pair 5`, events `pc19`/`pc18`),
at a −40 dB threshold:

| switch | fade-out | silence | fade-in |
|---|---|---|---|
| 018 → 019 (twice) | 5–7 ms | 84–85 ms | 5 ms |
| 019 → 018 (three times) | 5–6 ms | 59 ms | 4–5 ms |
| snapshot changes within 018 (five) | no gap | | |
| program change to the preset already loaded | no gap | | |

The silence is exact: the output falls to float denormals (−155 to −187
dBFS), so during the rebuild the codec buffer holds zeros, not a stale
block. The fade-out, per 440 Hz period (2.3 ms), goes −2, −5, −10, −25 dB
and then straight to the floor: a ramp of about 7–9 ms that is cut off,
closer to a linear 7.5 ms ramp than to the one-pole we assumed. The 25 ms
difference between the two directions is the chain contents (019's blocks
take longer to build). The gap seen from the ARM's MIDI input to the first
audible effect is 25–70 ms of USB and processing latency and does not
matter here.

So the loop has to cover 60–90 ms for these presets; the 64 ms loop and the
170 ms ring are enough, and `G_LEN` can grow to 128 ms if the ping-pong is
audible.

## 3. Hook, signal, memory, cycles

**Hook point** (high): the audio task's run step. Patch the `CJUMP 0x8cb20`
at `0x176463` (parcels `1804 0008 cb20`) to call an ISA stub of ours, and make
the two skip branches reach it as well: `0x176400` (`IF SV JUMP (PC,
0x176408) (DB)`, parcels `070e 0400 0008`) → target `0x176463` (offset
`0x63`), and `0x176405` (`IF NOT SV JUMP (PC, 0x176463)`, `072e 0000 005e`) →
unconditional (`073e 0000 005e`). The stub then decides itself: if
`DM(0xc4d14)` and `DM(0xc4cf7)` are set it calls `0x8cb20` as a C function
and records "ran", otherwise "skipped"; then it calls the fill with I1 =
`DM(0xc5b45)` (channel 1's half for this block). All three patches keep
instruction sizes, and the stub runs inside the task, under the mutex, with
the codec buffer still unsent.

**Gap signal** (certain): `DM(0xc4cf5)`: 1 between the ARM's two `0x0f`
commands. The loop should start fading in when the mute starts, which is
11 ms earlier; see section 4 for how the order is handled.

**Memory** (high): heap 4 is `0x328000..0xed7000` in DDR2, set by the
immediates at `0x17884b` (`M0 = 0x328000`), `0x178855` (`I4 = 0xed7000`,
its end) and `0x17885b` (`R12 = 0xbaf000`, its size); a second copy of the
end, `R12 = 0xed7000` at `0x6098b7`, feeds `0x608bba` at init and must be
changed with it. Lowering the end to `0xec7000` frees `0x10000` words: the
prototype uses `0xec6f00` for its state and `0xec7000..0xecb000` for two
8192-frame float rings. Whether DDR2 extends above `0xed7000` (which would
make the move unnecessary) depends on the chip size the DDR2 controller is
set up for (`0x1812..0x181e` at `0x8c0f8..0x8c110`), not decoded. Internal
RAM: block 3 above `0xe4842` is taken by the NAM kernel, the rest is heaps
and BSS; a short mono 16-bit ring would fit in block 2's tail only if the NAM
kernel moved, so DDR2 it is. The ring in external memory costs about 70
DDR2 accesses per block.

**Cycles** (certain for the simulator): 762 cycles per block for the fill
in the simulator, 216 instructions of code. The NAM kernel's budget note is
150,000 cycles per block.

## 4. The fill

`gapfill_kernel.asm`, called once per block with I1 = the channel-1 block
(16 frames × 8 TDM slots, Q31, bit 0 preserved), inputs `G_HOLD` and `G_RAN`
written by the stub:

- **Live** (`G_MODE` 0): the ring records the two main slots of every fresh
  block (`G_RAN = 1`) as floats, `G_W` advancing; stale blocks are not
  recorded.
- **Hold starts**: the loop is the `G_LEN` frames (3072, 64 ms) that end
  `G_MARGIN` frames (528, 11 ms) before `G_W`: the margin leaves out what
  the stock mute had already faded by the time the hold command arrived
  (without it the loop carried the fade and dipped at every turn). It plays
  forward and then backward (the value is continuous at each
  turn: a ping-pong at the sample level, no seam click), mixed in as
  `out = live * ran + loop * g`, with `g` following a one-pole ramp towards 1
  using `G_COEF`. Set `G_COEF` to the mute ramp's coefficient: the loop then
  fades in exactly as the stock mute fades the live signal out, and their
  sum stays level for the correlated content the loop is. While the graph is
  skipped (`G_RAN = 0`) the slots are overwritten with `loop * g`, which
  also replaces the stale block the codec would otherwise repeat.
- **Hold ends**: `g` ramps to 0 while the new preset's output fades in
  through the stock unmute ramp; below `G_THRESH` (−60 dB) the ring records
  again. A hold during the fade-out restarts the hold with the same loop.
  Silence before the switch gives a silent loop; at power-up the ring is
  zero.
- Length: 64 ms is long enough for the ping-pong to read as a sustain rather
  than a flutter and short enough to track a fast passage; it is a parameter.
  Equal-power fades are not needed because the loop starts as a copy of the
  signal it replaces; the one-pole matches the stock ramps.
- Limiting the fill to switches between identical copies is not possible on
  the DSP (it does not know the presets); if the ping-pong sounds odd for a
  very different preset, the ARM could pass a flag in the hold command's
  payload word (`{2, 1}` has a spare byte), which the stub would copy into
  `G_HOLD`.

**The 11 ms problem.** The mute parameters leave the ARM 11 ms before the
hold command, so with the stock order the loop would start fading in when
the live signal is already 11 ms into its 7.5 ms fade: a dip of about 10 ms.
Two ways out, both small:

1. ARM: move the command before the sleep. In `0x28e007f8`'s suspend path
   (`0x28e00864..`) the call to `0x28dfa504` (mute + sleep) precedes the
   hold and the command; reordering the three calls keeps every address.
   Then the loop fades in as the live fades out and the teardown starts
   11 ms later, when both are done; `G_MARGIN` becomes 0. The resume path
   needs no change.
2. DSP only: trigger the fill from the mute parameter instead. The mute
   reaches the ramp primitive's control function `0x60f636` (registry entry
   `0x2713`, `fn_ctrl` at `0x200000 + 8k + 4`); pointing that entry at a
   wrapper that sets a "mute requested" flag and tail-calls the original
   gives the fill the earlier signal. It fires for every `0x2713` instance,
   so the wrapper would check for the App's instance (the one whose target
   goes to 0 while `DM(0xc4cf5)` is clear). Medium confidence; option 1 is
   simpler.

The DSP's own mute ramp stays as it is in both cases. The measurement in
section 2.3 shows it is nearly linear over about 7.5 ms rather than a
one-pole, so the kernel's fade is a linear ramp of `G_RAMP` per sample
(1/360: 7.5 ms). With the stock call order the simulation shows what is
left: the mute is complete 3–4 ms before the hold command arrives, so the
loop starts from silence that late (`gapfill.py` renders it; `measure_gap.py`
reports a 3–4 ms gap on the filled output). Option 1 removes it.

## 5. Prototype results

`python3 tools/sharc/gapfill.py --wav <dir>` assembles the kernel, runs it
in `ssim.py` on 0.7 s of each test signal with a preset change at 0.25 s
(mute ramp, 11 ms, hold, graph skipped for `--gap-ms`, release, unmute),
compares every block with the float32 reference `GapFill` and writes
before/after `.wav` files.

| signal | gap | largest sample jump, stock | with the fill | blocks differing from the reference | cycles per block |
|---|---|---|---|---|---|
| 220 Hz sine, 0.5 | 80 ms | 0.0144 | 0.0144 | 0 | 762 |
| decaying pluck | 80 ms | 0.0233 | 0.0233 | 0 | 762 |
| three-note chord | 80 ms | 0.0193 | 0.0193 | 0 | 762 |
| silence | 80 ms | 0 | 0 | 0 | 762 |
| sine, 20 ms ramp | 200 ms | 0.0271 | 0.0263 | 0 | 762 |

The largest jump equals the signal's own largest slope: the fill adds no
discontinuity, and the stock output is a fade to silence, a repeated stale
block or silence, then a fade back. The flag bit of every word survives.

`measure_gap.py` on the rendered files: the stock output of the sine shows
a 9 ms fade-out, an 82 ms gap and a 3 ms fade-in (pluck 8 / 82 / 3 ms, chord
12 / 82 / 3 ms); on the filled output it finds no gap at all (nothing below
−40 dB for more than 3 ms) for any of the three. The files were written to
`/private/tmp/claude-502/-Users-joris-Projects-hx-stomp-nam-lite/409fed1e-5057-4161-917e-a4e04eedc082/scratchpad/wav3/`
(`sine_80ms_before.wav`, `sine_80ms_after.wav`, and the same for `pluck`,
`chord`, `silence`); rerun the command to regenerate them anywhere.

Two earlier runs are worth knowing about: with the fill's ramp at 0.9976
against a faster mute, the live signal was still at 28% when the hold
arrived and dropped at the teardown, hence `G_COEF` is a parameter; and
without `G_MARGIN` the loop carried the faded tail and `measure_gap.py`
found a 4 ms dip to −56 dB at every turn of the loop.

## 6. How it is injected (not wired into build.py)

`tools/research/gapfill_patch.py <in.hxf> <out.hxf> [--fill]` does it on a
built image (the NAM A2 Lite build, so a RAM run matches the installed
firmware):

1. The hook `tools/sharc/gapfill_stub.asm` as an INIT_L48 block after the
   last injected kernel: a C function that tests `DM(0xc4d14)` and
   `DM(0xc4cf7)`, calls `0x8cb20` (CJUMP with the frame stores) or not, sets
   `G_RAN`, copies `DM(0xc4cf5)` to `G_HOLD`, and with `--fill` sets I1 =
   `DM(0xc5b45)` and calls the kernel; it returns through `I12 = DM(M7, I6);
   JUMP (M14, I12) (DB); NOP; RFRAME`. Checked in the simulator with an
   emulated graph runner: the frame and stack come back as the caller left
   them in all four flag combinations.
2. Three VISA parcel patches in block 1's INIT_L16 block (two parcels per
   stream word, high half first): `0x176400` `070e 0000 0008 → 070e 0000
   0063`, `0x176405` `072e 0000 005e → 073e 0000 005e`, `0x176463` `1804
   0008 cb20 → 1804 000c xxxx` (the stub).
3. With `--fill`: the kernel's `.section pm` and its state (INIT_L32 at
   `0xec6f00`), and the heap-end immediates `0x178855`, `0x17885b` (block 1)
   and `0x6098b7` (DDR2) moved from `0xed7000` to `0xec7000`.
4. Not done: the ARM reorder (section 4) in Entry 0.

Both images (`build/HX_Stomp_gaphook.hxf`, the hook alone, and
`build/HX_Stomp_gapfill.hxf`) pass `l6ia_verify.py`. They are meant to be run
from RAM with upstream main's `tools/hxf_ram.py` (RAM_RUN.md there), which
writes nothing to the eMMC, and measured with `record_gap.py`.

## 7. Risks, open questions, hardware tests

- **Channel and slots** (medium): that channel 1 is the main output and
  which TDM slots are L/R (and whether phones/send slots should be filled
  too; the kernel can take more slot pairs). Test: fill slots 0/1 only and
  listen on main out and phones.
- **Mute ramp** (measured): about 7.5 ms and close to linear (section 2.3);
  the kernel's one-pole fade should be replaced by a linear ramp of the same
  length before the hardware test.
- **The stale block during edits**: HX Edit block adds and removes also skip
  the graph for a few blocks without any mute; the fill overwrites skipped
  blocks with `loop * g`, which with `g = 0` is silence rather than the
  stale repeat. That changes stock behaviour slightly (silence instead of a
  repeated block) and should be inaudible; worth a listen.
- **Kernels that read the hold flag** (`0x8dc1c`, `0x8dcf3`, `0x8dd0a`:
  likely delay or reverb lines): if they clear their state on hold, tails are
  gone after the switch anyway, which is what the loop stands in for.
- **DDR2 size** above `0xed7000`: if memory exists there the heap patch is
  unnecessary.
- **Interrupt safety**: the fill runs in the audio task, not in an ISR, with
  the mutex held; it saves and restores every register it touches, including
  MODE1, and the DMA half it writes is the one the stock output primitive
  just wrote.
- **Latency**: none added; the fill writes the same half the graph wrote.
- **A second switch during the gap**: the stub sees `DM(0xc4cf5)` stay 1 and
  keeps looping; a hold during the fade-out restarts it. Tested in the
  reference only for the restart case, not in the simulator scenario.
- **HX Edit preset loads** go through the same `0x28deb4e8` path and get the
  same fill.
- The prototype's `G_LEN` (64 ms) and fade are parameters to tune by ear on
  the unit.
