// gapfill_kernel.asm - fill the preset-change gap with a ping-pong loop of
// the last output. A prototype for research/gapless-preset-switch-findings.md;
// tools/sharc/gapfill.py is its reference and simulator test. Not wired into
// build.py.
//
// Called once per 16-frame block from the DSP's audio task, after the graph
// ran (or was skipped), with I1 = the block in the codec TX buffer: 16 frames
// of 8 TDM slots, 32-bit fixed point (Q31; the output primitive keeps bit 0
// as a flag, so bit 0 is preserved). Slots G_SLOT_L / G_SLOT_R are processed.
//
// Inputs, set by the hook before the call (DM, see G_*):
//   G_HOLD  1 while the ARM holds the DSP for a rebuild (command 0x0f), else 0
//   G_RAN   1 if the graph ran this block (the slots hold fresh audio),
//           0 if it was skipped (the slots hold a stale block: overwrite)
// State: G_MODE 0 = live (the ring records the output), 1 = holding (the loop
// plays and fades in), 2 = resuming (the loop fades out, then live again).
//
// Live: ring[W] = out, W++. Hold: the loop is the G_LEN frames that end
// G_MARGIN frames before W (the margin skips what the stock mute already
// faded before the hold arrived), played forward then backward (continuous
// at each turn), mixed in as out = live * ran + loop * g, with g ramping
// linearly to 1 by G_RAMP per sample (1/360: the stock mute's 7.5 ms, as
// measured on the unit, so the loop fades in as the stock mute fades the
// live signal out). Resume: g ramps to 0 while the new preset's output fades
// in; below -60 dB the ring records again.

.equ CBUFEN,   0x1000000
.equ N,        8192             // ring frames (170 ms at 48 kHz)
.equ RING_L,   0xec7000         // DDR2, carved from the end of Heap 4 (see the findings)
.equ RING_R,   0xec9000         // RING_L + N
.equ G_BASE,   0xecb000         // state, DDR2
.equ G_MODE,   0xecb000
.equ G_HOLD,   0xecb001
.equ G_RAN,    0xecb002
.equ G_SLOT_L, 0xecb003
.equ G_SLOT_R, 0xecb004
.equ G_GAIN,   0xecb005         // float, the loop's gain g
.equ G_RAMP,   0xecb006         // float, the fade's step per sample (1/360 = 7.5 ms)
.equ G_TARGET, 0xecb007         // float, 0.0 or 1.0
.equ G_W,      0xecb008         // ring write position, absolute address in RING_L
.equ G_P,      0xecb009         // loop play position, absolute address in RING_L
.equ G_DIR,    0xecb00a         // +1 / -1
.equ G_S,      0xecb00b         // steps into the loop, 0 .. G_LEN-1
.equ G_LEN,    0xecb00c         // loop length in frames (<= N - G_MARGIN)
.equ G_THRESH, 0xecb00d         // float, g below this ends the resume (1e-3)
.equ G_MARGIN, 0xecb00e         // frames before W left out of the loop (the ARM's 11 ms)
.equ G_SAVE,   0xecb010         // 32 words: the caller's registers
.equ G_STEP,   8                // TDM slots per frame

.section pm 0x0c5200

GAPFILL_ENTRY:
    DM(G_SAVE + 0) = MODE1;
    DM(G_SAVE + 1) = R0;
    DM(G_SAVE + 2) = R1;
    DM(G_SAVE + 3) = R2;
    DM(G_SAVE + 4) = R3;
    DM(G_SAVE + 5) = R4;
    DM(G_SAVE + 6) = R5;
    DM(G_SAVE + 7) = R6;
    DM(G_SAVE + 8) = R7;
    DM(G_SAVE + 9) = R8;
    DM(G_SAVE + 10) = R9;
    DM(G_SAVE + 11) = R10;
    DM(G_SAVE + 12) = R11;
    DM(G_SAVE + 13) = R12;
    DM(G_SAVE + 14) = R13;
    DM(G_SAVE + 15) = R14;
    DM(G_SAVE + 16) = R15;
    DM(G_SAVE + 17) = B3;
    DM(G_SAVE + 18) = I3;
    DM(G_SAVE + 19) = L3;
    DM(G_SAVE + 20) = B4;
    DM(G_SAVE + 21) = I4;
    DM(G_SAVE + 22) = L4;
    DM(G_SAVE + 23) = M3;
    DM(G_SAVE + 24) = M4;
    DM(G_SAVE + 25) = I5;
    DM(G_SAVE + 26) = L5;
    DM(G_SAVE + 27) = I6;
    DM(G_SAVE + 28) = L6;
    BIT CLR MODE1 0x600000;                 // no SIMD, no broadcast
    BIT SET MODE1 CBUFEN;
    L5 = 0;
    L6 = 0;

    // ---- mode transitions
    R0 = DM(G_HOLD);
    R1 = DM(G_MODE);
    R0 = PASS R0;
    IF EQ JUMP g_nohold;
    R2 = 1;
    COMP(R1, R2);
    IF EQ JUMP g_process;                   // holding already
    // start (or restart) the hold: loop = the G_LEN frames before W - G_MARGIN
    R1 = 1;
    DM(G_MODE) = R1;
    F0 = 1.0;
    DM(G_TARGET) = F0;
    R0 = DM(G_W);
    R1 = DM(G_LEN);
    R0 = R0 - R1;
    R1 = DM(G_MARGIN);
    R0 = R0 - R1;                           // start, absolute
    R1 = RING_L;
    COMP(R0, R1);
    R2 = N;
    IF LT R0 = R0 + R2;                     // wrapped
    DM(G_P) = R0;
    R0 = 0;
    DM(G_S) = R0;
    R0 = 1;
    DM(G_DIR) = R0;
    JUMP g_process;
g_nohold:
    R2 = 1;
    COMP(R1, R2);
    IF NE JUMP g_process;                   // live, or resuming already
    R1 = 2;                                 // holding -> resuming
    DM(G_MODE) = R1;
    F0 = 0.0;
    DM(G_TARGET) = F0;

    // ---- per block
g_process:
    R13 = -31;                              // Q31 -> float
    R12 = 31;                               // float -> Q31
    R15 = 1;
    R14 = -1;
    R2 = 0xfffffffe;
    R11 = 0x3f7ffffe;                       // clip, as the stock output primitive
    I5 = I1;
    R0 = DM(G_SLOT_L);
    M3 = R0;
    MODIFY(I5, M3);                         // left slot of frame 0
    I6 = I1;
    R0 = DM(G_SLOT_R);
    M3 = R0;
    MODIFY(I6, M3);                         // right slot of frame 0
    M4 = G_STEP;
    R1 = DM(G_MODE);
    R1 = PASS R1;
    IF NE JUMP g_play;

    // ---- live: record the output (only fresh blocks)
    R0 = DM(G_RAN);
    R0 = PASS R0;
    IF EQ JUMP g_done;
    B3 = RING_L;
    L3 = N;
    I3 = DM(G_W);
    B4 = RING_R;
    L4 = N;
    R0 = DM(G_W);
    R1 = N;
    R0 = R0 + R1;
    I4 = R0;
    M3 = 1;
    LCNTR = 16, DO g_rec UNTIL LCE;
    R0 = DM(0, I5);
    F0 = FLOAT R0 BY R13;
    DM(I3, M3) = R0;
    R0 = DM(0, I6);
    F0 = FLOAT R0 BY R13;
    DM(I4, M3) = R0;
    MODIFY(I5, M4);
g_rec: MODIFY(I6, M4);
    R0 = I3;
    DM(G_W) = R0;
    JUMP g_done;

    // ---- holding or resuming: out = live * ran + loop * g
g_play:
    B3 = RING_L;
    L3 = N;
    I3 = DM(G_P);
    B4 = RING_R;
    L4 = N;
    R0 = DM(G_P);
    R1 = N;
    R0 = R0 + R1;
    I4 = R0;
    F10 = DM(G_GAIN);
    F9 = DM(G_TARGET);
    F8 = DM(G_RAMP);
    R0 = DM(G_RAN);
    F4 = FLOAT R0;                          // 1.0 or 0.0: the live signal's gain
    R5 = DM(G_LEN);
    R5 = R5 - 1;
    R6 = DM(G_DIR);
    R7 = DM(G_S);
    LCNTR = 16, DO g_frame UNTIL LCE;
    F0 = F9 - F10;                          // g moves towards the target by at most G_RAMP
    F0 = CLIP F0 BY F8;
    F10 = F10 + F0;
    F1 = DM(0, I3);                         // loop L, R
    F3 = DM(0, I4);
    COMP(R7, R5);                           // ping-pong at the ends
    IF EQ R6 = R14;
    R0 = PASS R7;
    IF EQ R6 = R15;
    M3 = R6;
    MODIFY(I3, M3);
    MODIFY(I4, M3);
    R7 = R7 + R6;
    R0 = DM(0, I5);                         // left
    F0 = FLOAT R0 BY R13;
    F1 = F1 * F10;
    F0 = F0 * F4;
    F0 = F0 + F1;
    F0 = CLIP F0 BY F11;
    R0 = FIX F0 BY R12;
    R1 = DM(0, I5);
    R1 = R1 AND R15;
    R0 = R0 AND R2;
    R0 = R0 OR R1;
    DM(0, I5) = R0;
    R0 = DM(0, I6);                         // right
    F0 = FLOAT R0 BY R13;
    F3 = F3 * F10;
    F0 = F0 * F4;
    F0 = F0 + F3;
    F0 = CLIP F0 BY F11;
    R0 = FIX F0 BY R12;
    R1 = DM(0, I6);
    R1 = R1 AND R15;
    R0 = R0 AND R2;
    R0 = R0 OR R1;
    DM(0, I6) = R0;
    MODIFY(I5, M4);
g_frame: MODIFY(I6, M4);
    DM(G_GAIN) = F10;
    R0 = I3;
    DM(G_P) = R0;
    DM(G_DIR) = R6;
    DM(G_S) = R7;
    R1 = DM(G_MODE);
    R2 = 2;
    COMP(R1, R2);
    IF NE JUMP g_done;
    F0 = DM(G_THRESH);                      // resuming and faded out: live again
    COMP(F10, F0);
    IF GE JUMP g_done;
    R0 = 0;
    DM(G_MODE) = R0;
    F0 = 0.0;
    DM(G_GAIN) = F0;

g_done:
    R0 = DM(G_SAVE + 1);
    R1 = DM(G_SAVE + 2);
    R2 = DM(G_SAVE + 3);
    R3 = DM(G_SAVE + 4);
    R4 = DM(G_SAVE + 5);
    R5 = DM(G_SAVE + 6);
    R6 = DM(G_SAVE + 7);
    R7 = DM(G_SAVE + 8);
    R8 = DM(G_SAVE + 9);
    R9 = DM(G_SAVE + 10);
    R10 = DM(G_SAVE + 11);
    R11 = DM(G_SAVE + 12);
    R12 = DM(G_SAVE + 13);
    R13 = DM(G_SAVE + 14);
    R14 = DM(G_SAVE + 15);
    R15 = DM(G_SAVE + 16);
    L3 = DM(G_SAVE + 19);
    B3 = DM(G_SAVE + 17);
    I3 = DM(G_SAVE + 18);
    L4 = DM(G_SAVE + 22);
    B4 = DM(G_SAVE + 20);
    I4 = DM(G_SAVE + 21);
    M3 = DM(G_SAVE + 23);
    M4 = DM(G_SAVE + 24);
    L5 = DM(G_SAVE + 26);
    I5 = DM(G_SAVE + 25);
    L6 = DM(G_SAVE + 28);
    I6 = DM(G_SAVE + 27);
    MODE1 = DM(G_SAVE + 0);
    NOP;
    RTS;

.section dm 0xecb000
    .word 0, 0, 1, 0, 1             // G_MODE, G_HOLD, G_RAN, G_SLOT_L, G_SLOT_R
    .float 0.0                      // G_GAIN
    .word 0x3b360b61                // G_RAMP: 1/360, a 7.5 ms ramp
    .float 0.0                      // G_TARGET
    .word RING_L, RING_L, 1, 0      // G_W, G_P, G_DIR, G_S
    .word 3072                      // G_LEN: 64 ms
    .float 0.001                    // G_THRESH: -60 dB
    .word 528                       // G_MARGIN: 11 ms, the ARM's sleep between mute and hold
