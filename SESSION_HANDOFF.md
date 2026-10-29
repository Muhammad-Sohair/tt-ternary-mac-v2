# Session handoff — 2026-10-29 (final for the SKY 26d shuttle)

**Decision (owner, 29 Oct): A (`signoff-a`) is the TT SKY 26d submission. B is stopped. Nothing has been
submitted or paid yet; the owner does both.**

## Status against the owner's dates (notes/v2_00)

| gate | date | outcome |
|---|---|---|
| A signed off (`signoff-a`) | 14 Nov 2026 | **done 28 Oct** |
| B (8 rows, 3x2) may start | only if A signed off by 14 Nov | started 28 Oct on `b-dev` |
| B signed off, else submit A | 22 Nov 2026 | **B stopped 29 Oct by the owner → submit A** |
| shuttle closes | 30 Nov 2026 | submission and payment: owner |

## The two signed-off designs

| | v1 (`tt-ternary-mac`, tag `signoff-v7`) | **A (`tt-ternary-mac-v2`, tag `signoff-a`) — submitting** |
|---|---|---|
| design | 4×5 ternary MAC array, SPI, BIST | v1 + QSPI flash weight streaming (`FSTART`/`FSTOP`/`FCFG`) |
| top / tiles | `tt_um_msohair_tmac`, 2x2 | `tt_um_msohair_tmac2`, 2x2 |
| hardening run | V7 (notes/04 in v1) | 40677816085 (design = bf91db5) |
| utilisation | 57.7 % | 63.8 % |
| setup / hold WS | +5.69 / +0.107 ns | +4.88 / +0.103 ns |
| DRC / LVS / antenna | 0 / 0 / 0 | 0 / 0 / 0 |
| accepted residuals | 1 data net +4.8 ps at nom_ss (+47.5 ps max_ss) | net `_1543_` +50.5 ps at max_ss only; clock root fanout 16 and `fanout407` 9 vs our limit 8 (notes/v2_03) |
| verification | RTL 9/9, GL 9/9 incl. slow, mutation | RTL 14/14, GL 14/14 incl. slow, mutation 10/10 + control, devkit script 2/2 on the RTL |
| devkit script | reset-per-stage fix after the tag (v1 fee5df7), 1/1 on the RTL | flash path F1–F5 written and run against the RTL |

Skew ≤ 0.20 ns and hold ≥ +0.103 ns at all nine corners for A (table in notes/v2_03).

## A on `main`

`main` is ahead of `signoff-a` by notes-only commits. `src/`, `info.yaml` and `docs/` are byte-identical to
bf91db5, the commit of GDS run 40677816085. Every commit after bf91db5 was `[skip ci]`, so on 29 Oct the
GDS, test and docs actions were re-run on the tip of `main`, so that the newest commit has green actions.
The flow is deterministic: check that the new run's metrics match run 40677816085 before submitting.

## What the owner does on app.tinytapeout.com (not done by me)

1. **Before paying: ask TT whether they ship to Pakistan.** [UNKNOWN]: not in the terms' sanctions list,
   no positive list found (v1 handoff, checked 27 Oct).
2. Sign in with GitHub at https://app.tinytapeout.com/projects/create.
3. **Create a new project**, GitHub field: `https://github.com/Muhammad-Sohair/tt-ternary-mac-v2`, shuttle
   **TT SKY 26d** (the TT guides link `?shuttle=ttsky26d`).
4. Tiles: **2x2** (from `info.yaml`; 4 tiles). Price checked 27 Oct: 70 €/tile → 280 €, plus optional
   devkit and 15 € shipping. Re-check on the page.
5. Pay (or coupon) on the same page, then **Submit Project**.
6. **Click "Submit a new revision"**: creating the project does not put it in the tapeout. Which commit a
   revision takes is [UNKNOWN] from the docs (most likely the latest commit with green actions): confirm it
   shows the tip of `main` with a green GDS run.
7. Deadline 30 Nov 2026; no revisions after it.

v1 is not submitted: it was the fallback only if A could not be.

## B — stopped

8×5 array, 3x2, branch `b-dev` (left as is). RTL 16/16, but slew never approached A's standard: best run
42 pins / 16 nets, worst +403 ps at max_ss (A: 5 pins, one net, +50.5 ps). Post-GRT repair reports 0
violations while extraction shows up to 1.2 ns, so the flow's knobs cannot close it. Full record, the four
runs and the v3 input: `notes/v2_05_b.md` on `b-dev`.

## v3 plan (after the owner's paper deadline)

1. **B findings first.** Root cause of the repair-vs-extraction slew mismatch is [UNKNOWN]: re-run the
   post-GRT repair locally on the B run's ODB and compare its estimate for net `_03411_` with RCX.
   **Local Docker on this PC is broken and must be fixed first** (LibreLane runs in Docker).
2. **C** (flash weights + PSRAM activations: the host sends each vector once). It needs the 8-row array to
   harden cleanly, i.e. item 1.
3. **PSRAM datasheet check**, C's last open item: APS6404L maximum CS-low time (believed ~8 µs, for refresh;
   [UNKNOWN]). If it holds, bursts must be split. Flash quad-read timing is already verified (notes/v2_02).

## Gotchas learned

- OpenSTA cannot constrain slew per net; `set_load` on a net overrides its parasitics; set
  `SIGNOFF_SDC_FILE` too if you use a PnR-only SDC.
- TritonCTS builds no buffers on short H-tree levels; `CTS_DISTANCE_BETWEEN_BUFFERS` fixes the root fanout.
- At 3x2, post-GRT repair underestimates long-wire slew by > 3×; `GRT_DESIGN_REPAIR_MAX_WIRE_LENGTH` 100 µm
  makes detailed routing run past the 6 h runner limit.
- A fake `tt` plus `cocotb.task.bridge`/`resume` runs blocking MicroPython board code against the RTL: it
  found the stage-state bug in both v1's and A's devkit scripts before any hardware existed.
- In the local v2 clone, branch `main` is stale (v1's commit); work on `v2-dev`, which tracks `origin/main`.
- Redirected Python stdout is buffered: use `python -u` for background watchers.
