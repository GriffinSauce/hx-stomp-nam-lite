# Gapless preset switch: handoff

Where the work in `research/gapless-preset-switch-prompt.md` stands, how to
reproduce every step, and what to do next. The analysis itself is in
`research/gapless-preset-switch-findings.md` (section numbers below refer to
it). Branch: `claude/presets-snapshots-tradeoff-732jj9`.

## State (2026-11-08)

| piece | where | status |
|---|---|---|
| Preset-change sequence, ARM and DSP | findings §2 | done |
| DSP disassemblers (ISA, VISA) | `tools/research/sharc_ldr.py`, `sharc_dis.py`, `sharc_visa.py` | done; ISA round-trips through sasm, VISA reaches 99% |
| Gap measurement over USB + MIDI | `tools/research/record_gap.py` (analysis in `measure_gap.py`) | done, used on the unit |
| Fill kernel + float32 reference + simulator test | `tools/sharc/gapfill_kernel.asm`, `tools/sharc/gapfill.py` | done; agree word for word |
| Hook between the audio task and the graph runner | `tools/sharc/gapfill_stub.asm` | **works on the unit** |
| Test-image builder | `tools/research/gapfill_patch.py` | done |
| Fill on the unit | findings §6a | **works: 52–82 ms of silence → 3–4 ms dip** |
| ARM reorder (removes the last dip) | findings §4, option 1 | **next** |
| Wiring into `build.py` | — | not started; needs the owner's go-ahead |

Measured on the unit (from RAM, 440 Hz tone, presets 018 ↔ 019):

| image | preset change | snapshots |
|---|---|---|
| installed firmware | 5–7 ms fade-out, 59–85 ms silence, 5 ms fade-in | no gap |
| hook only | 5–6 ms fade-out, 52–82 ms silence, 5–6 ms fade-in | no gap |
| hook + fill | 4–7 ms fade-out, 3–4 ms dip, 5–6 ms fade-in | no gap |

## How the pieces fit

- The ARM mutes (two parameters on the App graph, ~7.5 ms linear ramp), sleeps
  11 ms, sends DSP command `0x0f` with `{2, 1}` (hold), rebuilds the chain,
  sends `0x0f` with `{2, 0}`, unmutes (findings §2.1).
- On the DSP the hold lands in `DM(0xc4cf5)`. The audio task (`0x1763a4`) runs
  the graph only when `DM(0xc4d14)` (graph valid) and `DM(0xc4cf7)`
  (processing enabled) are set (findings §2.2).
- Three parcel patches in the audio task (`0x176400`, `0x176405`, `0x176463`)
  send every block through the stub. The stub runs the graph under the stock
  conditions, records `G_RAN` and `G_HOLD`, and calls the fill on channel 1's
  transmit block, `DM(0xc5b45)` (8 TDM slots per frame; slots 0/1 are the main
  pair, confirmed by the USB measurement).
- The fill records the main pair into a ring and, during a hold, plays the last
  64 ms (ending 11 ms, `G_MARGIN`, before the hold) forward and backward with a
  linear 7.5 ms fade (`G_RAMP`).
- Memory: ring and state at DDR2 `0x2f8000..0x2fc040` (between the object at
  `0x2eb5ee` and the DSP's external code at `0x300000`). Kernel and stub code in
  block 2 after the NAM kernel (`0xc51ae..`).

## Reproduce

Environment (macOS, Apple Silicon):

```sh
python3.13 -m venv .venv-research        # or anywhere outside the repo
.venv-research/bin/pip install numpy scipy capstone matplotlib sounddevice mido python-rtmidi pyusb
brew install dfu-util libusb
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib   # so pyusb finds libusb
git fetch upstream && git worktree add ../hx-upstream upstream/main   # RAM-run tools
```

The stock `HX_Stomp_v3.80.0_0x103966115.hxf` sits git-ignored at the repo
root. The first USB audio use from a terminal triggers a macOS microphone
permission prompt; until it is accepted, opening the Stomp's audio device
hangs.

Build the test images:

```sh
python3 build.py HX_Stomp_v3.80.0_0x103966115.hxf --no-check      # build/HX_Stomp_v3.80.0_NAM_A2_Lite.hxf
python3 tools/research/gapfill_patch.py build/HX_Stomp_v3.80.0_NAM_A2_Lite.hxf build/HX_Stomp_gaphook.hxf
python3 tools/research/gapfill_patch.py build/HX_Stomp_v3.80.0_NAM_A2_Lite.hxf build/HX_Stomp_gapfill.hxf --fill
python3 tools/hxf.py unpack build/HX_Stomp_gapfill.hxf /tmp/x.l6ia && python3 tools/l6ia_verify.py /tmp/x.l6ia
python3 tools/sharc/gapfill.py --wav /tmp/wav     # simulator test + before/after .wav files
```

Run from RAM (nothing is written to the eMMC; a power cycle returns to the
installed firmware). HX Edit and Line 6 Updater must be closed. From the
upstream worktree:

```sh
python3 tools/hx_dfu_reboot.py                    # running Stomp -> DFU (1fc9:000c)
dfu-util -d 1fc9:000c -a 0 -D tools/ramboot/firmware-ramboot.dfu   # an I/O error at the end is normal
python3 tools/hxf_ram.py run <repo>/build/HX_Stomp_gapfill.hxf --stock <repo>/HX_Stomp_v3.80.0_0x103966115.hxf
```

After it boots:

1. **Re-plug the USB cable.** A RAM-run Stomp first enumerates at 12 Mb/s with
   only its vendor interface; after a re-plug it is at 480 Mb/s with audio and
   MIDI.
2. **Rebuild the test presets.** Every RAM run on this unit starts with empty
   presets and lost settings, and the block picker misbehaves on the first two
   slots (also with the plain NAM build: a RAM-run issue, probably because the
   loader cannot start the eMMC here, status `0x10b`). Set up 018 with the Input
   block on **USB 5/6** and a delay, and save the same to 019. Saves only go to
   SDRAM.

Measure (the unit is on **MIDI channel 2**; numbered presets, so `pc18` is 018):

```sh
python3 tools/research/record_gap.py --midi-channel 2 --out-pair 5 \
  --events "0.2:pc18,2.0:pc19,3.5:pc18,5.0:pc19,6.5:pc18,8.0:cc69=1,9.0:cc69=2,10.0:cc69=0,11.0:cc69=2,12.0:cc69=0,13.5:pc18" \
  --wav /tmp/run.wav
```

`--probe --out-pair N` prints each USB input's level, to find the routing.

## Gotchas met

- **Lowering heap 4's end hangs the DSP at boot**, even with the hook alone
  (`--heap-move` reproduces it). `R12 = 0xed7000` at `0x6098b7` is not the heap
  end but the base of two buffers above heap 4.
- VISA parcels: internal-memory INIT_L16 blocks pack two parcels per 32-bit
  stream word with the high half at the lower address; external memory has the
  low half first (`sharc_ldr.py`, `gapfill_patch.parcel_word`).
- Data for external memory goes in INIT_EXT32 (tag 10) blocks, as the stock
  stream does; INIT_L32 is for internal memory.
- The analysis of the RAM-run measurement is only valid with the tone routed
  into the chain; without it the script times the noise floor.
- `0x2eb5ee` is an object of 0x800 units with a semaphore, used by the
  graph-building path. That the units are words (so the ring at `0x2f8000` is
  clear of it) is not proven; nothing has misbehaved because of it so far.
- A process named `Updater` on this Mac is Arc's background updater, not Line 6
  Updater.

## Next steps

1. **ARM reorder** (findings §4, option 1). In `0x28e007f8`'s suspend path
   (`0x28e00864..`), the call to `0x28dfa504` (mute + 11 ms sleep, at
   `0x28e00866`) comes before `hold(1)` (`0x28da7c84` at `0x28e00870`) and the
   command `0x0f` block (`0x28e00880..0x28e008a2`, sent only when the counter
   at `engine+0x64ae` is 0). Send the hold command first, then mute and sleep.
   The return value of `0x28dfa504` is checked (`subs r6, r0, #0; blt`), and
   the counter branch at `0x28e0087e` must keep working; a small code cave with
   the reordered sequence and a branch to it may be simpler than rearranging
   in place. Then set `G_MARGIN` to 0 in the kernel's data and re-measure: the
   3–4 ms dip should go. Entry 0 changes are loaded whole by the RAM run, so
   this tests the same way. Use `tools/research/e0_xref.py` (cache with
   `E0_CACHE=<dir>`).
2. Listen on the analog outputs and phones during a switch (slots beyond 0/1
   may carry the phones or send; the kernel can take more slot pairs).
3. Tune `G_LEN` (64 ms) by ear; 128 ms fits in the ring.
4. Zero the ring at start (it holds whatever was in DDR2 at boot; a switch in
   the first 75 ms after boot would replay that, clipped).
5. With the owner's go-ahead: wire `gapfill_patch.py`'s steps into `build.py`,
   with a check in the build like the NAM kernel's.
6. The HX Stomp XL and HX Effects builds have other addresses; the patch
   checks every parcel it changes and refuses other firmware.
