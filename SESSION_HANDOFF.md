# Session handoff — 2026-10-28 (v2)

## Progress against the owner's dates (notes/v2_00)

| gate | date | status |
|---|---|---|
| A signed off (`signoff-a`) | 14 Nov 2026 | **Everything done except the owner's decision on two residuals.** Not tagged yet. |
| B (8 rows, 3x2) may start | only if A signed off by 14 Nov | not started |
| submit A, else v1 | 22 Nov 2026 | — |
| shuttle closes | 30 Nov 2026 | — |

## Where A stands

- Repo **github.com/Muhammad-Sohair/tt-ternary-mac-v2** (public), `main` = A with
  `GRT_DESIGN_REPAIR_MAX_SLEW_PCT` 55. Local clone `E:\extras\portfolio\ternary-silicon\v2`, branch `v2-dev`
  tracks `origin/main`. v1 untouched in `..\v1`.
- RTL 14/14, gate level 14/14 (incl. slow test), mutation 10/10 with a passing control.
- 2x2, 63.8 % util, setup +4.88 ns, hold +0.103 ns, DRC/LVS/antenna 0, precheck passes. Re-run reproduces
  0/318 metrics and the same GDS (timestamps masked).
- **Residuals needing the owner's decision** (notes/v2_03): (1) one data net +50.5 ps over max-slew at
  max_ss only, 0 at nom_ss; (2) max fanout 16 on the clock root `clkbuf_16` (our own limit is 8; its slew is
  0.294 ns vs 0.75 ns) and 9 on one data buffer.
- Evidence branches `eval/a-grt55`, `eval/a-nonor21`, `eval/a-3x2`: keep (notes/v2_03 cites them).
  3x2 was worse (16/20 slew pins), so more tiles do not help.

## Next

1. Owner decides on the residuals. If accepted: tag `signoff-a` on `main`, freeze.
2. If A is signed off by 14 Nov: B on its own branch (A + 8 rows, 3x2), same standard, by 22 Nov.
3. C: only the two datasheet checks for the v3 plan (flash quad-read timing is already done in v2_02;
   PSRAM APS6404L CS-low limit still open).
4. Devkit bring-up script for the flash path (`devkit/`) is not written yet.

## Gotchas learned this session

- A mutation harness needs a no-mutation control: without it, a broken build counts as "caught".
- cocotb's Makefile uses `$(PWD)`: a subprocess must get `PWD` = the test dir with forward slashes.
- A testbench model that takes its first value-change as the baseline misses that change.
- LibreLane's post-GRT repair can *downsize* a gate to fix its input net; with a 40 % margin the GRT
  estimate can hide a post-DRT slew violation. 55 % stopped it here.
- GDS files differ run to run only in BGNLIB/BGNSTR timestamps; mask them to compare.
