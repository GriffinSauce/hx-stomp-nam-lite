// gapfill_stub.asm - the hook between the DSP's audio task and the graph
// runner (research/gapless-preset-switch-findings.md, section 3).
//
// The audio task (VISA, block 1) is patched to call this instead of the graph
// runner 0x8cb20, on every block, whether or not it would have run the graph.
// Called as a C function (CJUMP with the frame stores in the delay slots):
// on entry I6 is the frame, DM(-1, I6) the return address, I7 the stack top,
// M7 = -1, M14 = 1 as the C runtime keeps them. It runs the graph under the
// stock conditions (graph valid, DM(0xc4d14), and processing enabled,
// DM(0xc4cf7)), records whether it did in G_RAN, copies the hold flag
// DM(0xc4cf5) (command 0x0f) into G_HOLD and, when GAPFILL is 1, calls the
// fill on channel 1's transmit block, DM(0xc5b45). With GAPFILL 0 it is the
// hook alone, to prove the patch on the unit.

.equ GRAPH_VALID, 0xc4d14
.equ PROC_ENABLE, 0xc4cf7
.equ HOLD_FLAG,   0xc4cf5
.equ CH1_BLOCK,   0xc5b45
.equ GRAPH_RUN,   0x8cb20
.equ G_HOLD,      0x2fc001
.equ G_RAN,       0x2fc002

.section pm 0x0c5100

GAPSTUB_ENTRY:
    R0 = DM(GRAPH_VALID);
    R0 = PASS R0;
    IF EQ JUMP gs_skip;
    R0 = DM(PROC_ENABLE);
    R0 = PASS R0;
    IF EQ JUMP gs_skip;
    CJUMP GRAPH_RUN (DB);
    DM(I7, M7) = R2;
    DM(I7, M7) = PC;
    R0 = 1;
    JUMP gs_ran;
gs_skip:
    R0 = 0;
gs_ran:
    DM(G_RAN) = R0;
    R0 = DM(HOLD_FLAG);
    DM(G_HOLD) = R0;
    R0 = GAPFILL;
    R0 = PASS R0;
    IF EQ JUMP gs_out;
    I1 = DM(CH1_BLOCK);
    CALL GAPFILL_ENTRY;
gs_out:
    I12 = DM(M7, I6);
    JUMP (M14, I12) (DB);
    NOP;
    RFRAME;
