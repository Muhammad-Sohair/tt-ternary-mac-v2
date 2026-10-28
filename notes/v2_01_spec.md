# v2 Step 1 — spec: a ternary MAC that fetches its own weights

Written 2026-10-28. Shuttle TT SKY 26d closes **2026-11-30** (the purchase page shows 1 Dec 2026).
**Go/no-go for v2: 2026-11-22.** If v2 is not signed off by then, v1 (`signoff-v7`) is submitted instead.

Tags: `[VERIFIED]` read from a source named in the row · `[CALCULATED]` · `[ESTIMATE]` · `[UNKNOWN]`.

## 1. Two corrections to what I said in chat

1. **TriLM-99M's ternary weights are 9.20 MiB packed, not ~20 MB.** I counted all 99M parameters as
   ternary. They are not: the embedding and LM head are FP16 and untied (`[50304, 512]` each).
   Source: the FPGA repo, `QUANTIZATION_CONTRACT.md` §C and `PAPER_DATA`. `[VERIFIED]`
   ⇒ **all 112 ternary matrices of the 99M model fit in the QSPI Pmod's 16 MB flash.** 390M (55.2 MiB)
   does not.
2. **The devkit alone cannot run the whole 99M model.** The FP16 embedding + LM head are 98 MiB, far
   more than the RP2040 (or the Pmod) holds. The realistic demo is: **a PC does embedding, norms,
   attention, softmax and the LM head; the chip computes every ternary projection**, with the RP2040
   only relaying. In TriLM those projections are the 112 ternary linears, i.e. all of the ternary
   weights.

## 2. Hardware facts this spec relies on

| fact | value | source |
|---|---|---|
| QSPI Pmod parts | 1 × W25Q128JV flash (16 MB), 2 × APS6404L-3SQR PSRAM (8 MB each) | `mole99/qspi-pmod` schematic `[VERIFIED]` |
| Pmod pins | uio[0] CS flash, uio[1] SD0, uio[2] SD1, uio[3] SCK, uio[4] SD2, uio[5] SD3, uio[6] CS RAM A, uio[7] CS RAM B | Pmod README `[VERIFIED]` |
| Pmod header | plugs into the demoboard's **bidirectional** (uio) Pmod | TT QSPI guide `[VERIFIED]` |
| Flash quad mode | QE bit is set on Pmods bought from the TT store; flash ID reads `ef7018` | TT QSPI guide `[VERIFIED]` |
| Flash loading | TT's web Flasher writes a `.bin` to the Pmod through the demoboard | TT QSPI guide `[VERIFIED]` |
| TT output pin limit | ~33 MHz ⇒ SCK = clk/2 = 25 MHz at 50 MHz clk | tinytapeout.com/specs `[VERIFIED]` (v1 notes) |
| Quad read command, dummy cycles | W25Q128JV "Fast Read Quad Output" (0x6B) / "Quad I/O" (0xEB) | **[UNKNOWN]** until I read the datasheet, before RTL |
| PSRAM max CS-low time | I believe APS6404L limits CS low to ~8 µs (refresh) | **[UNKNOWN]**; it would force burst splitting, so it is checked before RTL for option C |
| Pmod price | — | **[UNKNOWN]**, check the TT store |

v1's pins already leave uio[7:0] unused, so **the Pmod takes the uio port and the v1 SPI/status pins stay
exactly as they are.**

## 3. The key insight: the bus, not the array, sets the speed

At SCK 25 MHz, 4 bits per clock, the flash delivers 12.5 MB/s = **62.5 M trits/s**, about **1.25 trits
per 50 MHz clock** `[CALCULATED]`. v1 already does 20 MACs per clock. So v2 does **not** need a bigger
array to keep up with the flash; it needs a way to feed it. The real cost driver is where the
**activations** live, because the chip has almost no memory (~40 bytes per tile in flip-flops,
tinytapeout.com/specs/memory).

**Dataflow common to all options (output-stationary):** the chip holds R row accumulators. For one group
of R output rows it streams through the whole input vector: for every 5 activations it reads the next
R packed weight bytes from flash, **in one continuous flash read** (one command + address per matrix,
then SCK simply pauses whenever the datapath is busy). Weights are laid out in flash in exactly the order
they are consumed, so no address logic beyond a start address is needed. After the group, the host reads
the R results with v1's `READ_ACC` (`sat16(acc >>> shift)`, unchanged) and the chip moves on.

**v2 is a superset of v1.** `LOAD_W` over SPI, `READ_W`, `BIST` and every v1 command stay. If the Pmod
path fails on silicon, the chip still behaves exactly like v1.

## 4. Options

Per token for TriLM-99M: ~48 M ternary MACs `[CALCULATED]` (9.20 MiB × 5 trits/byte).
Activation traffic for output-stationary R rows = MACs / R bytes.

| | A — flash streamer | B — flash streamer, 8 rows | C — flash + PSRAM activations |
|---|---|---|---|
| what changes vs v1 | + QSPI flash read master, `FETCH` command; activations still sent by the host over SPI for every row group | as A, with R = 8 rows | as A/B, plus: host sends a vector **once**, chip writes it to PSRAM, then re-reads it per row group on the same bus |
| array | 4 × 5 (v1) | 8 × 5 | 4 × 5 or 8 × 5 |
| host SPI traffic / token | 12 MB | 6 MB | ~0.1 MB |
| time / token, ternary part `[CALCULATED]` | ~30 s (host SPI ≤ 3.1 MHz bound) | ~15 s | ~1.3–1.7 s (QSPI bound, R = 8 / 4) |
| tiles `[ESTIMATE]` | 2x2 if ≤ ~60 % after the 1.55× correction, else 3x2 | 3x2 | 3x2 – 4x2 |
| tile cost | 280 € or 420 € | 420 € | 420 – 560 € |
| new risks | QSPI timing at the pad; flash command correctness | + area/slew like v1's 4×10 | + PSRAM write path, CS-low limit, bus turnaround between 3 chips, more states to verify |
| fits 25 days of work? | **yes, with margin** | yes, tighter (sign-off was the slow part of v1) | **only just**; the most likely to miss 22 Nov |

The speeds are for the ternary projections only and assume the host keeps up; MicroPython on the RP2040
will be slower than its hardware SPI limit `[ESTIMATE]`. None of these is "fast". The portfolio claim is
**"the chip fetches a real TriLM-99M weight matrix from external flash and computes the projection
bit-exactly on silicon"**, which A already delivers.

## 5. My recommendation

**A**, sized to whatever the real flow says (2x2 or 3x2). It adds the one feature that changes what the chip
*is* (it reads its own weights), keeps v1 inside it as a fallback, and leaves time for the same
verification depth as v1 (bit-exact model, gate level, mutation testing, an SPI-flash behavioural model in
cocotb). B is a cheap follow-on if A comes out small. C is the right v3, not something to rush.

## 6. Decisions I need from you

1. Option: **A**, B or C.
2. Top-module name for v2. It must differ from v1's on the shuttle. Suggestion: `tt_um_msohair_tmac2`.
3. **A new public GitHub repo** (suggestion: `Muhammad-Sohair/tt-ternary-mac-v2`). The TT hardening flow
   only runs in GitHub Actions (Docker does not work on this PC), so v2 cannot be signed off without it.
   I will not create it until you say yes.
4. Confirm the go/no-go date: **22 Nov 2026**.

## 7. Schedule (if A)

| dates | work |
|---|---|
| 28–30 Oct | datasheet facts (§2 UNKNOWNs), flash-image packer (TriLM matrix → `.bin` in consumption order) |
| 31 Oct – 6 Nov | RTL: QSPI master, FETCH sequencing; Python model extended; cocotb flash model |
| 7–13 Nov | tests incl. one real TriLM-99M 512×512 projection end to end in RTL sim; first hardening run |
| 14–21 Nov | sign-off (timing at ss, slew, DRC/LVS), gate-level sim, mutation batch, docs, devkit script |
| **22 Nov** | **go/no-go**: v2 signed off → submit v2; otherwise submit v1 |
| 23–30 Nov | buffer; purchase + submission (yours) |
