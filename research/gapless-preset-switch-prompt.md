# Prompt: hide the HX Stomp's preset-switch gap with a reverse loop

Give this whole file to a coding agent running locally, in a clone of this
repository, with the stock firmware file available.

---

## Goal

When the HX Stomp changes preset, the audio drops out briefly while the
firmware rebuilds the DSP chain. Delay and reverb tails are cut off too.
The owner wants to **fill that gap**:

1. Keep a short ring buffer of the last output audio, for example 50–200 ms.
2. When a preset change mutes the output, play that buffer as a **ping-pong
   loop**: forward, then reversed, and so on. Reversing at each turn avoids
   the click a plain loop makes at its seam.
3. When the new preset's audio returns, **crossfade** from the loop into it.

If this works, the owner can keep identical preset copies (snapshots 1–3 in
preset N, "snapshots" 4–6 in preset N+1) and switch between them from a MIDI
controller without an audible gap. That gives more than 3 snapshots per song
without touching the snapshot code. Read `research/more-snapshots-prompt.md`
for that background; this prompt replaces it as the first route to explore.

Your job is **research and design**, plus a prototype in the simulator if
that is feasible. Do not build or flash anything.

## Context

Read `README.md` and skim `tools/` first. The firmware is the stock HX Stomp
3.80.0 (`HX_Stomp_v3.80.0_0x103966115.hxf`, MD5
`a2cd8f4ae56f49415380270ebc484bf1`). The owner will give you its path; if
not, ask.

What the repo already has:

- `tools/hxf.py` and `tools/l6ia.py` unpack the firmware. It holds 15 entries,
  word-swapped; `patch_amp.get_entry_swapped(l6ia, idx)` returns a payload in
  its native form.
- **Entry 0** is the main application (ARM, Thumb-2). Its segments map to
  load addresses through `patch_amp._parse_e0_segments` and
  `_e0_vaddr_to_foff`.
- **Entry 2** is the DSP program for the **SHARC ADSP-21469**, as an LDR boot
  stream. `patch_amp.inject_sharc_ldr_blocks` already adds our own code to it.
  `tools/sharc/sasm.py` is an assembler and `tools/sharc/ssim.py` a simulator.
- **Entry 4** is the DSP graph library: the "composites" made of primitives
  that form each block. The NAM kernel replaces one primitive.
- Audio runs at 48 kHz in **blocks of 16 samples**. The NAM kernel
  (`tools/sharc/nam_lite_kernel.asm`) has its own fades in it (`N_GAIN`,
  `N_G1`); use that as an example of a fade on this DSP.

## Rules

- **Never commit Line 6 code or data**: no firmware bytes, extracted entries,
  disassembly listings or decompiled code. Commit only your own code, notes,
  offsets and short descriptions (an instruction's address and encoding is
  fine).
- Don't write to the device, run Line 6 Updater, or produce a `.hxf` that is
  meant to be installed.
- Put temporary files outside the repo, or in a git-ignored folder.
- Give a confidence level for every conclusion that isn't certain.

## Questions to answer

### 1. What happens to the audio during a preset change?

Trace it on both processors.

- **ARM side (Entry 0):** find the preset-load path and the messages it sends
  to the DSP: mute or fade commands, "tear down graph", "load graph", "set
  parameters", "unmute". In what order, and with what timing or waits?
- **SHARC side (Entry 2):** find out:
  - What keeps running while a new graph is being built? Look at the codec DMA
    and audio interrupt, and the main loop.
  - Where is the final output written, after the graph and before the codec?
    Is there a fixed I/O stage that sits outside every graph?
  - Is the gap explicit silence (a mute flag, zeroed buffers), or does the
    audio interrupt stop running?
  - How long is the gap? Is it fixed, or does it depend on the preset's size?
    Is there a fade-out and fade-in, and how long are they?

### 2. Is there a place for the fill?

- A hook point that runs **every block, including during the gap**, and can
  read and write the final output. Examples: the audio interrupt, an output
  copy loop, or a mute routine.
- A way to know when the gap starts and ends: a mute flag, a "graph valid"
  flag, a fade gain, or a message from the ARM.
- Memory for the ring buffer. Stereo float at 48 kHz is 384 bytes per ms.
  Find out what is free in internal RAM, and whether there is external memory
  the DSP can use. Can the buffer be shorter, mono, or 16-bit if space is
  tight?
- Spare DSP cycles in that hook. Ring-buffer writes plus a crossfade cost very
  little, but check there is headroom while a graph is loading, if anything
  runs then.
- If the SHARC has no suitable hook: can the ARM side stretch the fade, or
  keep the old graph running until the new one is ready (double buffering)?
  Is there room for two graphs? This is the only alternative worth noting.

### 3. Design the fill

- Ring buffer length, and the loop segment length. Consider choosing the loop
  points at zero crossings, or using a short crossfade at each turn instead of
  a hard reverse.
- How the fill tracks the start of the gap: start looping **before** the old
  audio fades out, so the loop holds full-level audio.
- The crossfade into the new preset: its length, and whether it should be
  equal-power.
- Edge cases:
  - the gap is longer than expected;
  - the owner switches preset again during the gap;
  - silence before the switch;
  - presets loaded from HX Edit;
  - power-up, when there is no old audio.
- How the fill could be limited to switches between **identical copies**, if
  that matters. A ping-pong loop might sound odd for unrelated presets. It may
  be fine everywhere.

### 4. Prototype, if questions 1 and 2 show a clear hook

- Write the fill routine in SHARC assembly (`sasm.py`) and a Python reference
  next to it, as `nam_lite.py` does for the kernel.
- Test it in `ssim.py` with synthetic input: a sine, a decaying pluck, a
  chord, and silence, with a simulated gap. Check that there are no
  discontinuities (look at the largest jump between samples) and that the
  crossfade is correct.
- Write a script that renders the before and after as `.wav` files, so the
  owner can listen.
- Describe how it would be injected (LDR blocks, hook site, data addresses),
  **but don't wire it into `build.py` yet**. That needs the owner's go-ahead.

### 5. Measuring on the hardware

The owner can record the Stomp over USB audio. Write
`tools/research/measure_gap.py`: given a recording of a held note across a
preset switch, it reports the length of the gap and of the fade-out and
fade-in. Include short instructions for the owner. The real gap length
decides the buffer size, so ask the owner for a measurement as soon as you
have this script.

## Method tips

- For Entry 0, use a disassembler with Thumb-2 support, such as Ghidra in
  headless mode or `capstone`. Follow the strings around preset loading and
  the messages sent to the DSP (look for a mailbox or SPI/link-port
  protocol).
- For Entry 2, extend `ssim.py` or `sasm.py` with a disassembler pass if
  needed. Start from the interrupt vector table and find the codec or SPORT
  DMA interrupt.
- The NAM kernel's hook into an unused primitive (see `patch_amp.py`,
  `cmd_patch`) shows how this repo already adds code to the DSP program.

## Deliverable

Write `research/gapless-preset-switch-findings.md` with:

1. A short summary: is the fill feasible, where would it go, and how
   confident are you?
2. The preset-change sequence on both processors, with addresses and timing.
3. The hook point, the gap-detection signal, the memory and the cycle budget,
   each with addresses.
4. The fill design, with its parameters.
5. Prototype results, if you built one: the test signals, the largest sample
   jump, and paths to the rendered `.wav` files (not committed).
6. Risks, open questions, and what needs a test on hardware.

Commit your own scripts (under `tools/research/`, or `tools/sharc/` for
SHARC code) and the findings file, then push the branch. Scripts must read
from the owner's own firmware file and must not embed its bytes.
