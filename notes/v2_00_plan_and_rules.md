# v2 plan and the owner's rules (decided 2026-10-28)

These rules are the owner's, verbatim in substance. They override any schedule pressure.

1. **Build A first, to the full v1 standard**: timing at all corners, DRC/LVS, gate-level simulation,
   mutation tests. Tag it **`signoff-a`** and freeze it. **From that moment A is the default submission.**
2. **B** (A with 8 rows, 3x2) starts **only if A is signed off by 14 Nov 2026**, on a separate branch. If B is
   not fully signed off to the same standard by **22 Nov 2026**, submit A. **No partial verification to make
   B fit.**
3. **C is not attempted for this shuttle.** If time remains, only verify its two open items against
   datasheets (flash quad-read timing, PSRAM chip-select hold limit) and write them into the v3 plan.
4. Top module **`tt_um_msohair_tmac2`**. Create the public repo **`Muhammad-Sohair/tt-ternary-mac-v2`**
   when hardening starts.
5. If neither A nor B is signed off by 22 Nov 2026, **submit v1** (`signoff-v7`).

Process: **verify the flash quad-read timing against the datasheet before writing any RTL.** Report
progress against these dates at every session handoff.

## Date gates

| date | gate | outcome if missed |
|---|---|---|
| 14 Nov 2026 | A signed off (`signoff-a`) | B is not started |
| 22 Nov 2026 | B signed off to the same standard | submit A |
| 22 Nov 2026 | A signed off | submit v1 |
| 30 Nov 2026 | shuttle closes (purchase page: 1 Dec) | — |

## Progress log

| date | status |
|---|---|
| 28 Oct | Rules recorded. Spec v2_01 written. |
