# v2 Step 4 — devkit bring-up of the flash path

Script: `devkit/tmac_bringup.py` (MicroPython on the TT demo board; copy it and `test/tmac_model.py` to the
board). `run_all(tt)` runs the v1 stages S1–S8 (no Pmod needed), and `run_flash(tt)` runs the flash stages
F1–F5 with the TT QSPI Pmod on the bidirectional header.

## How the board reaches the flash

TMAC2 drives flash /CS, SCK and both PSRAM /CS whenever it is selected (`uio_oe` = `0b11001001`, constant).
So the RP2 cannot program the flash while TMAC2 is selected without fighting the chip. `flash_board()`
calls `tt.shuttle.disable()`, which selects project 0 (`tt_um_chip_rom`, whose `uio_oe` is `8'h00` in its
source, TinyTapeout/tt-chip-rom) and holds `ena` low. The RP2 then drives the Pmod pins itself
(`tt.uio_oe_pico`) and bit-bangs single-SPI flash commands. `flash_chip()` releases the pins and selects
TMAC2 again. TT's own QSPI Pmod flasher app (TinyTapeout/tinytapeout-flasher) is an alternative for writing
images; the script does not need it.

Firmware calls used, checked against tt-micropython-firmware `main` (src/ttboard): `shuttle.disable()`,
`shuttle.<name>.enable()`, `uio_in` (write), `uio_out` (read; both read the same physical byte),
`uio_oe_pico`, `clock_project_PWM`, `reset_project`.

| stage | what it does | a failure means |
|---|---|---|
| F1 | JEDEC ID (want EF 40/70 18) and SR2 QE; `set_qe=True` sets QE with WRITE STATUS REGISTER-2 (31h) | no Pmod, wrong header, or QE = 0 (6Bh then returns garbage) |
| F2 | programs `F2 79 79 79` at 0x000100, FSTART, LOAD_FC 1..5: row 0 = 15 | the stream does not work at all at 1 MHz: pins, protocol, or flash content |
| F3 | three random matrices (ragged N, K), every SCK divider, bit-exact against `TMAC2` and plain arithmetic | stream ordering, chunk boundaries, padding |
| F4 | at 50 MHz, all 16 (divider, sample delay) cells; prints the passing window | where the real pad/mux round trip lands; choose FCFG inside the window |
| F5 | FSTOP closes the stream (/CS high), a BIST pulse closes it too, the stream works again after | stream control |

F4 is the measurement the RTL cannot make: the pass/fail grid on silicon is the round trip through the TT
mux and pads. The simulated grid, with the datasheet's 6 ns plus an assumed 20 ns round trip (`t_valid` =
26 ns), is

```
   /2  --  ok  --  --
   /4  --  ok  ok  ok
   /8  --  ok  ok  ok
   /16 --  ok  ok  ok
```

Each stage writes its image to its own 4 KB sector (pages 0x0001, 0x0010, 0x0040), because `fl_write`
erases whole sectors.

## Tested against the RTL

`test/test_devkit.py` runs the script unmodified in a cocotb bridge thread, with a fake `tt` object.
Project 0 selected: the script programs `W25Q`, a bit-level single-SPI model that needs WREN before
erase/program/status writes, wraps page programs inside the page, holds BUSY for a few status reads, and
reports QE. TMAC2 selected: `FlashModel` (the same model as the flash tests in `test.py`) serves the chip's
6Bh reads from the same memory and checks the protocol. The fake board also flags any pin driven by both
the RP2 and the chip. Above 2.5 MHz the harness compresses time so that the script's 20 µs SPI phase stays
50 project clocks, which keeps the 50 MHz run short.

Result: **2/2 pass** (S1–S3, S5 ×12, S6, S7; F1 stopping on QE = 0, then F1–F5 with `set_qe=True`). The F4
grid equals the timing-budget prediction cell for cell, and it contains both passing and failing cells, so
the stage can tell a good sample point from a bad one.

### Bugs it found in the script before any hardware

1. **Stage state leaked between stages (also in v1's script).** S5–S7 compared against a fresh model, but
   S2's BIST leaves DONE/PASS set in the status byte, and S5's last `SHIFT` stays in force for S6/S7
   (`CLEAR` resets neither). On silicon, S5–S7 would have reported failures on a working chip. Fix: reset
   the chip before every stage. The v1 devkit script had the same bug; on the owner's instruction it is fixed
   in the v1 repo (commit fee5df7, after `signoff-v7`, script only), and the same harness run on the v1 RTL
   shows it: unfixed S5 200/200 trials, S6 and S7 fail on status 0xb1 vs 0x81; fixed, S1–S7 pass.
2. **F4 erased F2's image.** `fl_write` erases whole 4 KB sectors, and F4's image was in the same sector as
   F2's, so F5 (which reuses F2's image) read erased bytes. Fix: one sector per stage.
3. A harness bug, not a script bug: my first `W25Q` model served read data before the last address byte
   had arrived. Found by the same run, fixed in the model.

### What this does not cover

The firmware itself is not simulated: if `tt.shuttle.disable()` or `uio_oe_pico` behave differently on the
shipped firmware version than in its source today, F1 will show it (no ID). Real MicroPython timing is
slower than the simulation, which only makes the SPI phases longer (safe for the chip's f_SCK ≤ f_clk/16).
Whether project 0 on SKY 26d is `tt_um_chip_rom` with `uio_oe = 0` is taken from its current source:
[UNKNOWN] for the 26d tapeout until the shuttle's project list is published.
