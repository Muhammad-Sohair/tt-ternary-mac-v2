# Session handoff — 2026-10-28 (v2)

## Progress against the owner's dates (notes/v2_00)

| gate | date | status |
|---|---|---|
| A signed off (`signoff-a`) | 14 Nov 2026 | **Done 28 Oct.** Tag `signoff-a` on `main`; A is now the default submission. |
| B (8 rows, 3x2) may start | only if A signed off by 14 Nov | **Started 28 Oct** on branch `b-dev`: RTL 16/16, first 3x2 hardening run 40698174384 in CI |
| B fully signed off, else submit A | 22 Nov 2026 | — (25 days left) |
| shuttle closes | 30 Nov 2026 | — |

## A (`signoff-a`, frozen)

- Evidence: GDS run 40677816085 (design = commit bf91db5), reproduced from eval run 40673894596.
  RTL 14/14, gate level 14/14 incl. slow test, mutation 10/10 with control, devkit script 2/2 against the RTL.
- Owner's clean-up run (`eval/a-cleanup`, run 40692931119) fixed both targets but moved the slew to a
  sibling decoder net (+14.6 ps at max_ss), so it was not clean. Per rule 4 run a was signed off with both
  residuals accepted and documented in notes/v2_03: net `_1543_` +50.5 ps at max_ss only; clock root
  `clkbuf_0_clk` fanout 16 and `fanout407` 9 vs our limit 8. Skew ≤ 0.20 ns and hold ≥ +0.103 ns at every corner.
- v2_03 now compares the slew with v1 corner for corner: v2 0 vs 4.8 ps at nom_ss, +50.5 vs +47.5 ps at max_ss.
- Devkit: `devkit/tmac_bringup.py` has flash stages F1–F5 (the RP2 programs the Pmod with project 0
  selected). `test/test_devkit.py` runs the script unmodified against the RTL (notes/v2_04).

## Next

1. B (`b-dev`): read the 3x2 run; then timing/slew to A's standard, gate level incl. slow test, a new
   mutation batch with a control (qspi_fetch changed), docs. Deadline 22 Nov, else submit A.
2. **v1's devkit script has the state-leak bug** the RTL harness found (S5–S7 compare against a fresh model
   after S2's BIST and S5's SHIFT). v1 is frozen; if v1 is ever submitted, add a reset before each stage.
3. C: only the PSRAM APS6404L CS-low limit datasheet check for the v3 plan.

## Gotchas learned this session

- OpenSTA cannot constrain slew per net (`set_max_transition`: clocks, ports, design only). `set_load` on a
  net overrides its parasitics. If you use it in a PnR SDC, set `SIGNOFF_SDC_FILE` too, or signoff STA
  reads the PnR SDC.
- TritonCTS builds no buffers on short H-tree levels, so the root can drive all leaf-level buffers;
  `CTS_DISTANCE_BETWEEN_BUFFERS` fixes that at the cost of insertion delay (+0.28 ns here).
- Fixing one decoder net's slew moved it to a sibling bit, twice (eval b and the clean-up run).
- A fake `tt` object plus `cocotb.task.bridge`/`resume` runs blocking MicroPython board code against the
  RTL; it found three bugs before any hardware existed.
- A mutation harness needs a no-mutation control; cocotb's Makefile uses `$(PWD)` (forward slashes).
