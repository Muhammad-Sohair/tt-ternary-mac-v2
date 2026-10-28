# v2 Step 3 — A: verification and hardening log

Design: `tt_um_msohair_tmac2` = v1 datapath + `qspi_fetch.v` (Fast Read Quad Output from the QSPI Pmod
flash). Tiles 2x2. Flow: LibreLane 3.0.14 via `tt-gds-action@ttsky26d`, sky130A, 50 MHz.

## Verification

**RTL: 14/14** (the 9 v1 tests unchanged, plus 5 flash tests; `test/README.md`). The flash tests run a
behavioural W25Q128JV that checks the 6Bh protocol (instruction, address, IO0 released before data, PSRAM
chip selects high, mode 0, /CS gap) and puts **random garbage on the data lines between t_hold and t_valid
after every SCK fall**, so a wrong sample point reads wrong data rather than passing by luck.

`test_flash_timing_windows` checks 10 (divider, sample delay, round-trip) cases against the timing budget of
notes/v2_02, **in both directions**: the 5 settings the budget calls safe read correctly, and the 5 it calls
unsafe read garbage. So the test can tell a good sample point from a bad one.

### Bugs and mistakes found along the way

1. **Testbench: the flash model swallowed its first pin change.** It used the first value-change event as
   its baseline. When FSTART was the first command, that event was the /CS fall, so the model never saw the
   command and the data lines stayed 0 (every weight decoded as −1, all rows equal). Fix: take the baseline
   from the pins at construction. RTL was not involved.
2. **My first mutation batch was invalid.** The harness ran `make` with the caller's `PWD`, so every build
   failed with "file not found", and the script counted each failure as a caught mutant: 10/10 "caught",
   including mutants that cannot affect the test that "caught" them. Adding a **no-mutation control**
   exposed it (the control "failed" too). Fixed (PWD = the test dir, forward slashes), re-validated with the
   control, re-run. Lesson kept: a mutation batch without a passing control proves nothing.
3. **Area:** the first fetcher was 3,318 µm². Selecting IO0 bits straight from the page register instead of
   copying them into a 32-bit shift register, and dropping output registers that the core's registered
   write port already provides, brought it to 1,929 µm² (whole design +11 % over v1 instead of +16 %).

### Mutation batch (shipped RTL, control passes all 5 flash tests)

| mutant | caught by | message |
|---|---|---|
| instruction 6Bh → 6Ch | test_flash_matvec | flash protocol: unexpected instruction 0x6c |
| nibble order swapped | test_flash_matvec | raw acc [-43, -198, 76, -51] != model [-95, -123, 20, -260] |
| 7 dummy clocks instead of 8 | test_flash_matvec | raw acc [-200, -362, -201, -94] != model […] |
| sample point one clk early | test_flash_timing_windows | div 0 samp 1 t_valid 26.0: read bad, budget predicts ok |
| compute does not wait for weights | test_flash_wait_and_overrun | after one vector: FSTAT 0x02 |
| OVERRUN never set | test_flash_wait_and_overrun | after an extra activation: FSTAT 0x12 |
| IO0 not released for the dummy clocks | test_flash_matvec | IO0 still driven by the chip during data out |
| PSRAM A /CS driven low | test_flash_pins_reset | uio_out 0x81 |
| write address steps on every nibble | test_flash_matvec | raw acc [0, 20, 0, -260] != model […] |
| next chunk one nibble short | test_flash_matvec | FLASH_READY never rose |

**10 of 10 caught.** The v1 mutation batch (8/8) covers the unchanged datapath.

**Gate level: 14/14** on the run-a netlist (`final/pnl`, UNIT_DELAY, sky130_fd_sc_hd models), including
the 24-bit internal-saturation test (`TMAC_GL_SLOW=1`, 10 min). CI `gl_test` passes on every run.

## Hardening

| run | change | util | setup WNS | hold WNS | max-slew pins nom_ss / max_ss | fanout | DRC/LVS/ant |
|---|---|---|---|---|---|---|---|
| 1 | v1's V7 flow settings | 63.3 % | +4.88 | +0.102 | 5 / 10 | 3 | 0/0/0 |
| a | post-GRT repair slew margin 40 → 55 % | 63.8 % | +4.88 | +0.103 | **0 / 5** | 2 | 0/0/0 |
| b | `nor2_1` excluded | 63.3 % | +4.84 | +0.099 | 5 / 5 | 2 | 0/0/0 |
| c | run 1 at **3x2** | 42.2 % | +5.25 | +0.108 | 16 / 20 | 2 (+1 cap) | 0/0/0 |
| clean-up | run a + `set_load` on net `_1543_` (PnR only) + `CTS_DISTANCE_BETWEEN_BUFFERS` 70 | 64.1 % | +4.89 | +0.101 | 0 / 5 (another net) | **0** | 0/0/0 |

**Run 1's slew violation, diagnosed.** One net, 0.31 ns over at nom ss: a decoder output bit
(`u_core.u_dec` code for column 3) fanning out to the weight registers of all four rows (`w[3]`, `w[8]`,
`w[13]`, `w[18]`). Through mid-PnR it was driven by a `nor2_2` with no violation. The post-global-route
repair step (41) then **downsized it to `nor2_1`**, to relieve a violation on the net feeding it. With the
40 % margin the global-route estimate (0.43 ns) still looked safe, but detailed routing made the wire
longer and the real slew 1.06 ns. Raising the post-GRT repair margin to 55 % (run a) stops that downsizing.
`nor2_1` exclusion (b) shifts the problem to the sibling bit.

**3x2 is worse, not better** (c): as with v1's fallback, a bigger die spreads the same logic and lengthens
the wires. More tiles would cost 140 € and buy more violations.

**Reproducibility.** Run a's setting was committed to `main` (one key changed:
`GRT_DESIGN_REPAIR_MAX_SLEW_PCT` 40 → 55) and re-run in CI (run 40677816085): **0 of 318 metrics differ**
from eval run 40673894596, the LEF is identical, and the GDS is identical once its 91 timestamp fields
(BGNLIB/BGNSTR dates) are masked.

All runs: setup and hold met at every corner (0 violations), Magic DRC 0, routing DRC 0, LVS 0, antenna 0,
precheck passes.

### Clock tree of the signed-off build (run a, `main`), every corner

Insertion delay = clock network latency from the `clk` pin to the flops (min / max over all 439 sinks); latency
skew = max − min. STA setup/hold skew are OpenSTA's worst-path figures, which include the 0.25 ns clock
uncertainty and CRPR.

| corner | insertion delay min / max (ns) | latency skew (ns) | STA setup skew | STA hold skew | hold WS (ns) | setup WS (ns) |
|---|---|---|---|---|---|---|
| nom_tt_025C_1v80 | 0.661 / 0.771 | 0.109 | 0.323 | -0.397 | 0.310 | 12.653 |
| nom_ss_100C_1v60 | 1.141 / 1.335 | 0.194 | 0.378 | -0.505 | 0.866 | 5.005 |
| nom_ff_n40C_1v95 | 0.457 / 0.534 | 0.077 | 0.302 | -0.352 | 0.106 | 13.864 |
| min_tt_025C_1v80 | 0.640 / 0.745 | 0.104 | 0.319 | -0.392 | 0.307 | 12.720 |
| min_ss_100C_1v60 | 1.107 / 1.292 | 0.185 | 0.372 | -0.496 | 0.860 | 5.124 |
| min_ff_n40C_1v95 | 0.443 / 0.515 | 0.072 | 0.298 | -0.348 | 0.106 | 13.892 |
| max_tt_025C_1v80 | 0.689 / 0.805 | 0.116 | 0.330 | -0.404 | 0.314 | 12.582 |
| max_ss_100C_1v60 | 1.181 / 1.382 | 0.200 | 0.384 | -0.514 | 0.872 | 4.883 |
| max_ff_n40C_1v95 | 0.472 / 0.558 | 0.086 | 0.309 | -0.360 | 0.103 | 13.833 |

Healthy: physical skew is at most 0.20 ns (1 % of the 20 ns period), hold is met at every corner with at
least +0.103 ns (ff corners) *after* the 0.25 ns uncertainty, and setup has at least +4.88 ns.

### The owner's clean-up run (28 Oct) and its outcome

Rule (owner, 28 Oct): one combined run with (a) the slew net's repair over-constrained by about 10–20 % and
(b) CTS settings that keep the clock-root fanout at or below 8, everything else identical. If clean on both,
with utilisation, timing, DRC/LVS and all tests still passing: tag that build. If not: sign off the current
build, accepting both residuals, provided skew and hold are healthy at every corner.

How it was implemented (branch `eval/a-cleanup`, commit 004f84e, run 40692931119):

- **(a)** OpenSTA cannot put a max-transition limit on one net: `set_max_transition` accepts only clocks,
  ports and the design (checked in its `sdc/Sdc.tcl`). The per-net lever that does exist is `set_load` on a
  net, which in delay calculation takes precedence over the net's parasitics (`GraphDelayCalc::parasiticLoad`).
  `src/pnr.sdc` = the base SDC plus `set_load 0.0354 [get_nets _1543_]`, i.e. 115 % of that net's max-RC
  extracted wire cap (0.0308 pF). It is read by PnR steps only. `src/signoff.sdc` = the base SDC alone, set as
  `SIGNOFF_SDC_FILE`, so signoff STA saw the real extracted parasitics.
- **(b)** The tree had 4 H-tree levels but buffers only at level 4: the upper segments were short, so CTS put
  no buffer on them and the root drove all 16 level-4 buffers. `CTS_DISTANCE_BETWEEN_BUFFERS` = 70 µm forces
  buffers along the H-tree wires (it enables TritonCTS's simple-segment mode, `set_distance_between_buffers`).

| | run a (signed off) | clean-up run |
|---|---|---|
| utilisation | 63.8 % | 64.1 % |
| setup / hold WS | +4.883 / +0.103 ns | +4.891 / +0.101 ns |
| max insertion delay, max_ss | 1.382 ns | 1.663 ns (path depth 3 → 5 buffers) |
| latency skew, max_ss | 0.200 ns | 0.220 ns |
| clock-root fanout | 16 | **2** |
| max fanout violations | 2 | **0** |
| targeted net `_1543_` | +50.5 ps at max_ss | **fixed** (driver upsized `a21oi_2` → `a21oi_4`) |
| max slew violations nom_ss / max_ss | 0 / 5 pins | 0 / **5 pins**: a different net, `_1539_` (`nor2_1 _4426_`, a sibling decoder code bit, 4 × `a22o_2` loads), +14.6 ps at max_ss |
| DRC / LVS / antenna, precheck, gl_test | 0 / 0 / 0, pass, pass | 0 / 0 / 0, pass, pass |

**Not clean on both**: the slew moved to the neighbouring decoder bit rather than going away, the same
pattern as eval b (excluding `nor2_1`). Per the rule, **run a is signed off as `signoff-a`** with both
residuals accepted. The clean-up build stays on `eval/a-cleanup` as evidence and is not used.

### Residuals accepted in `signoff-a` (owner's rule 4)

1. **Max slew, one data net.** Net `_1543_`, driver `_4435_` (`sky130_fd_sc_hd__a21oi_2`), loads `_4436_/B1`,
   `_4448_/A1`, `_4456_/B1`, `_4468_/A1` (4 × `a22o_2`), which write weight bits `w[3]/w[8]/w[13]/w[18]` (the
   decoder code bit for column 3). **0.8005 ns vs 0.75 ns: +50.5 ps (6.7 %) at max_ss_100C_1v60 only; 0 at
   nom_ss and at the other 7 corners.**
   Why it is functionally negligible: it is a data net (not clock, not reset). The 0.75 ns limit is the
   TT flow's `MAX_TRANSITION_CONSTRAINT`, half the library's own `default_max_transition` of 1.5 ns (checked
   in the tt library), so the cells are still inside their characterised range. Slew only matters through
   delay, and STA has already timed the path with this slew: worst setup slack at that corner is +4.88 ns.
   **Comparison with v1, corrected:** the earlier text said "about the size of v1's", without naming the
   corner. v1's accepted residual was one net at **+4.8 ps at nom_ss and +47.5 ps at max_ss**, plus a second
   net at +15.2 ps at max_ss. Corner for corner, v2 is 0 vs 4.8 ps at nom_ss and **+50.5 vs +47.5 ps at
   max_ss** (6 % larger). Measured against v1's headline nom_ss figure (4.8 ps), v2's 50.5 ps is about
   10× larger. Either way it is still acceptable under the owner's rule, for the reasons above.
2. **Max fanout, clock root and one data buffer.** `clkbuf_0_clk` (`sky130_fd_sc_hd__clkbuf_16`) drives 16
   loads vs our `MAX_FANOUT_CONSTRAINT` of 8 (over by 8, 100 %); fanout is the same at every corner.
   `fanout407` (`buf_2`, net `u_core.a[2][0]`) drives 9 (over by 1). The limit is our own, not a library limit.
   Why it is functionally negligible: fanout is only a proxy for slew and load. The root's output slew is
   0.294 ns at max_ss and 0.149 ns at max_ff (limit 0.75), its load is 0.182 pF, and there are no max-cap
   violations anywhere. The clock tree it drives has the skew and hold margins in the table above at every
   corner. `fanout407` has no slew or cap violation at any corner.
