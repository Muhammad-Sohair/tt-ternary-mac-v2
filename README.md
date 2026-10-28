![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# TMAC2: a ternary (1.58-bit) MAC array that streams its weights from QSPI flash

Version 2 of [tt-ternary-mac](https://github.com/Muhammad-Sohair/tt-ternary-mac) for the Tiny Tapeout
**SKY 26d** shuttle (SkyWater 130 nm). The v1 datapath is unchanged: a 4 × 5 array of multiplier-free
ternary PEs with the FPGA's base-3 weight packing and `sat16(acc >>> shift)` requantisation
([LLM-on-a-FPGA](https://github.com/Muhammad-Sohair/LLM-on-a-FPGA)). New in v2: a QSPI master that reads
packed weights straight from the TT QSPI Pmod's W25Q128JV flash, so a whole weight matrix (all of
TriLM-99M's ternary weights fit in the 16 MB flash) streams through the array while the host only sends
activations. Every v1 command still works, so without the flash the chip behaves exactly like v1.

**Status:** RTL verified (14/14 cocotb tests); hardening in progress. Sign-off rules and dates:
[notes/v2_00](notes/v2_00_plan_and_rules.md). Not submitted. The datasheet is in [docs/info.md](docs/info.md).

## v2 notes

- [v2_00 plan and rules](notes/v2_00_plan_and_rules.md): the owner's go/no-go rules and the progress log.
- [v2_01 spec](notes/v2_01_spec.md): options A/B/C, two corrections to earlier estimates.
- [v2_02 flash timing](notes/v2_02_flash_timing.md): the W25Q128JV quad-read timing, verified against the
  datasheet before any RTL, and why the sample point is programmable.

The v1 sign-off numbers and notes below describe the v1 chip (tag `signoff-v7`), kept for the history.

## How the design works

```
SPI (mode 0) -> command decoder -> packed weight bytes -> trit decoder -> W[4][5] (2-bit codes)
                                -> int8 activations a[5] ------------------------------+
BIST (LFSR -> core -> MISR) -mux->  4 rows x 5 ternary PEs: dot = sum (a XOR neg)&nz + popcount(neg)
                                    pipelined 24-bit saturating accumulators
MISO <- status / sat16(acc >>> shift) / raw 24-bit / weights / BIST signature
```

## The engineering story (the useful part)

The `notes/` folder is a log of what happened, including the mistakes:

1. [Flow walkthrough](notes/01_flow_walkthrough.md): RTL → cocotb → GDS → reading timing/DRC/LVS reports,
   on a practice counter. The first gate-level run failed because the testbench sampled too early.
2. [Spec and options](notes/02_spec.md): three sized options. My area estimate turned out to be 1.55× too
   optimistic, and why.
3. [RTL and verification](notes/03_rtl_and_verification.md): the owner's design decisions, 8 bugs found
   and what each taught (one synthesis-only bug Vivado would have accepted), mutation testing.
4. [Hardening and sign-off](notes/04_signoff.md): 22 hardening runs. Pipelining and a reset synchroniser to
   close 50 MHz at ss/100 °C. Chasing max-slew from 1,325 violations down to one net, including a wrong
   theory I had to retract, and every warning explained.
5. [Silicon test plan](notes/05_silicon_test_plan.md): staged bring-up on the TT devkit, with what each
   failure would mean. The script is [devkit/tmac_bringup.py](devkit/tmac_bringup.py) (MicroPython, reuses
   the same Python model). For TMAC2 it also programs the QSPI Pmod flash and runs flash stages F1–F5
   ([notes/v2_04](notes/v2_04_devkit_bringup.md)); `test/test_devkit.py` runs the script against the RTL.

## Running it (Windows, no WSL or Docker needed)

```
python -m venv .venv && .venv\Scripts\pip install -r test/requirements.txt
cd test && mingw32-make                       # RTL simulation (iverilog 12 + cocotb 2.1)
mingw32-make GATES=yes                        # gate level: netlist from the gds action, PDK_ROOT set
python tmac_model.py --write-rtl              # regenerate the BIST signature after model/BIST changes
```

Hardening runs in GitHub Actions (`.github/workflows/gds.yaml`). The SKY130 cell models for gate-level
simulation come from `ciel` (`ciel enable --pdk-family sky130 -l sky130_fd_sc_hd <version>`).

`practice/counter/` holds the Step 1 design. The `acc24-shift-ref` tag and the `eval/4x10-acc24-3x2` branch
keep the 4 × 10 versions for a later multi-tile chip.

## License

Apache-2.0. Built on the Tiny Tapeout SKY template (Apache-2.0).
