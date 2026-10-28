# TMAC2 tests

`tmac_model.py` is the bit-exact Python reference model (`TMAC` for the v1 datapath, `TMAC2` adds the flash
stream; `pack_stream` builds flash images). `test.py` drives the chip's pins through a cocotb SPI master and
compares every observable value with the model. The flash tests add `FlashModel`, a behavioural W25Q128JV on
the `uio` pins that checks the command protocol and puts random garbage on the data lines between `t_hold`
and `t_valid` after every SCK fall, so a wrong sample point reads wrong weights instead of passing by luck.

| test | what it proves |
|---|---|
| test_reset_state | status 0x81; weights, accumulators and flags are zero after reset |
| test_decoder_exhaustive | all 256 byte values: PACKING_FORMAT v1.0 decode, illegal 243–255 → zeros + sticky flag, CLEAR |
| test_random_matvec | random weights and activations (incl. −128/127), CMP vs CMP_CLR, random shifts 0–31 |
| test_streamed_matvec | LOAD_AC streaming: K-chunked matrix-vector product |
| test_shift_floor_semantics | readout uses floor division: −1 >>> 1 = −1, −3 >>> 1 = −2 |
| test_readout_saturation | int16 clamp on READ_ACC, RSAT status bit, READ_RAW still exact |
| test_internal_saturation | 24-bit clamp and sticky SAT flags (RTL only: ~6,600 transactions) |
| test_bist | BIST via opcode and pin, signature = model, SPI writes ignored while busy |
| test_spi_abort_and_reset | a half-sent byte writes nothing; rst_n mid-frame restores the reset state |
| test_flash_pins_reset | flash and both PSRAM /CS driven high, SCK low, IO0 released; FSTAT/FCFG reset values |
| test_flash_matvec | y = W·x with weights streamed from flash, ragged N and K, all four SCK dividers, protocol checked |
| test_flash_timing_windows | for 10 (divider, sample delay, round trip) cases, the weights read correctly exactly when the timing budget says they should |
| test_flash_wait_and_overrun | a compute waits for its weights (PEND); an early activation sets OVERRUN; CLEAR clears both |
| test_flash_stop_restart_bist | FSTOP, restart at another page, BIST closes the stream and passes, v1 LOAD_W still works |

`test_devkit.py` runs the board script `devkit/tmac_bringup.py` unmodified against the RTL (a cocotb bridge
thread plus a fake `tt` object with the firmware API the script uses). With project 0 selected, the script
programs `W25Q`, a bit-level single-SPI W25Q128JV (WREN, sector erase, page program with page wrap, BUSY,
QE). With TMAC2 selected, `FlashModel` serves the chip's reads from the same memory. Not in the default
run (about 2 min): `mingw32-make COCOTB_TEST_MODULES=test_devkit`.

| test | what it proves |
|---|---|
| test_devkit_s_stages | the v1 stages S1–S3, S5 (12 trials), S6, S7 pass on TMAC2 |
| test_devkit_flash_stages | F1 stops on QE = 0, then sets it; programming, F2 hand example, F3 random matvecs at every divider, F4's 50 MHz sample window equals the timing budget, F5 FSTOP/BIST; no pin is driven by both the RP2 and the chip |

## Running on Windows

```
cd test
mingw32-make                        # RTL
mingw32-make GATES=yes              # gate level: copy the hardened netlist to gate_level_netlist.v, set PDK_ROOT
python tmac_model.py --write-rtl    # regenerate the BIST signature in src/ after changing the model or BIST
```
