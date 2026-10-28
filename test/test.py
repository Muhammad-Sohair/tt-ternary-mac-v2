# SPDX-License-Identifier: Apache-2.0
"""cocotb tests for TMAC2. Every check is bit-exact against tmac_model.TMAC / TMAC2.

The SPI master below drives the pins the way the devkit's RP2040 will:
mode 0, MSB first, SCK half-period = HALF clk cycles (f_SCK = f_clk / 16),
sampling MISO just before each rising edge.

The flash tests run a behavioural W25Q128JV (FlashModel) on the uio pins. It
answers only Fast Read Quad Output (6Bh), checks the command protocol, and
models the pad/mux round trip: after each SCK fall the data lines carry
garbage from t_hold until t_valid, so sampling outside the valid window fails.

GATES=yes runs the same tests on the hardened netlist. The 24-bit internal
saturation test needs ~6,600 transactions; it runs in RTL only (see SLOW).
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer
from cocotb.utils import get_sim_time

from tmac_model import (ACC_MAX, ACC_MIN, BIST_ITERS, COLS, FLASH_ERASED, NBYTES, ROWS,
                        TMAC, TMAC2, bist_expected, decode, pack_matrix, pack_stream,
                        sat16_shift)

HALF = 8                                   # clk cycles per SCK phase
GL = os.environ.get("GATES") == "yes"

OP_STATUS, OP_LOAD_W, OP_LOAD_A, OP_LOAD_AC = 0x00, 0x10, 0x20, 0x21
OP_CMP, OP_CMP_CLR, OP_READ_A, OP_READ_R = 0x30, 0x31, 0x40, 0x41
OP_READ_W, OP_BIST, OP_READ_S, OP_CLEAR, OP_SHIFT = 0x50, 0x60, 0x61, 0x70, 0x80
OP_LOAD_FC, OP_FCFG, OP_FSTART, OP_FSTOP, OP_FSTAT = 0x22, 0x90, 0x91, 0x92, 0x93

UO_MISO, UO_FRDY, UO_DONE, UO_PASS, UO_ILL, UO_SAT, UO_BUSY, UO_ACT = 0, 1, 2, 3, 4, 5, 6, 7


# ---------------- pin-level helpers ----------------
_ui = {}


def set_ui(dut, sck=None, csn=None, mosi=None, bist=None):
    # Keep a Python shadow of ui_in. Reading dut.ui_in.value back is wrong here:
    # cocotb applies writes at the end of the timestep, so two set_ui() calls in
    # the same timestep would read the same stale value and the second would
    # undo the first (this kept SCK stuck high in the first version).
    v = _ui.get("v", 0)
    for bit, val in ((0, sck), (1, csn), (2, mosi), (3, bist)):
        if val is not None:
            v = (v & ~(1 << bit)) | (val << bit)
    _ui["v"] = v
    dut.ui_in.value = v


def uo_bit(dut, bit):
    return (int(dut.uo_out.value) >> bit) & 1


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())   # 50 MHz
    dut.ena.value = 1
    dut.uio_in.value = 0
    _ui["v"] = 0b0010
    dut.ui_in.value = 0b0010                                   # CS_n high, SCK low
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)


async def xfer(dut, tx, nread=0):
    """One CS_n-framed transaction. Sends tx (+ nread zero bytes); returns all MISO bytes."""
    out = list(tx) + [0] * nread
    rx = []
    set_ui(dut, csn=0)
    await ClockCycles(dut.clk, HALF, rising=False)
    for byte in out:
        got = 0
        for i in range(7, -1, -1):
            set_ui(dut, mosi=(byte >> i) & 1)
            await ClockCycles(dut.clk, HALF, rising=False)
            got = (got << 1) | uo_bit(dut, UO_MISO)                # sample just before the rise
            set_ui(dut, sck=1)
            await ClockCycles(dut.clk, HALF, rising=False)
            set_ui(dut, sck=0)
        rx.append(got)
    await ClockCycles(dut.clk, HALF, rising=False)
    set_ui(dut, csn=1, mosi=0)
    await ClockCycles(dut.clk, HALF, rising=False)
    return rx


async def read_status(dut):
    return (await xfer(dut, [OP_STATUS], 2))


async def read_acc16(dut):
    rx = await xfer(dut, [OP_READ_A], 2 * ROWS)
    vals = []
    for r in range(ROWS):
        v = rx[1 + 2 * r] << 8 | rx[2 + 2 * r]
        vals.append(v - 65536 if v & 0x8000 else v)
    return vals


async def read_raw(dut):
    rx = await xfer(dut, [OP_READ_R], 3 * ROWS)
    vals = []
    for r in range(ROWS):
        v = rx[1 + 3 * r] << 16 | rx[2 + 3 * r] << 8 | rx[3 + 3 * r]
        vals.append(v - (1 << 24) if v & 0x800000 else v)
    return vals


async def read_codes(dut):
    n = (ROWS * COLS + 3) // 4
    return (await xfer(dut, [OP_READ_W], n))[1:]


async def check_all(dut, m, tag=""):
    """Compare every observable register with the model."""
    raw = await read_raw(dut)
    assert raw == m.acc, f"{tag}: raw acc {raw} != model {m.acc}"
    acc16 = await read_acc16(dut)
    m.note_read_acc()
    assert acc16 == m.read_acc16(), f"{tag}: sat16 {acc16} != model {m.read_acc16()} (shift {m.shift})"
    sat_b, status = (await read_status(dut))[1:]
    assert sat_b == m.sat_byte(), f"{tag}: sat flags 0x{sat_b:02x} != 0x{m.sat_byte():02x}"
    assert status == m.status(), f"{tag}: status 0x{status:02x} != 0x{m.status():02x}"


def rand_weights(rng, p_zero=1 / 3):
    return [[0 if rng.random() < p_zero else rng.choice((-1, 1)) for _ in range(COLS)] for _ in range(ROWS)]


# ---------------- tests ----------------
@cocotb.test()
async def test_reset_state(dut):
    """After reset: status 0x81, accumulators/weights zero, BIST pins low."""
    await start(dut)
    m = TMAC()
    rx = await xfer(dut, [OP_STATUS], 2)
    assert rx[0] == 0x81, f"status during opcode byte: 0x{rx[0]:02x}"
    assert await read_codes(dut) == m.weight_codes()
    await check_all(dut, m, "reset")
    for b in (UO_DONE, UO_PASS, UO_ILL, UO_SAT, UO_BUSY):
        assert uo_bit(dut, b) == 0, f"uo[{b}] not 0 after reset"


@cocotb.test()
async def test_decoder_exhaustive(dut):
    """All 256 byte values through the decoder: legal bytes decode per PACKING_FORMAT,
    243..255 decode to zero and raise the sticky ILLEGAL flag (pin and status)."""
    await start(dut)
    for b in range(256):
        await xfer(dut, [OP_LOAD_W, b])                         # lands in weight byte 0 = row 0, cols 0..4
        codes = (await xfer(dut, [OP_READ_W], 2))[1:]
        ws, bad = decode(b)
        exp = 0
        for k, w in enumerate(ws):
            exp |= {-1: 3, 0: 0, 1: 1}[w] << (2 * k)
        assert (codes[0] | codes[1] << 8) & 0x3FF == exp, f"byte {b}: codes 0x{codes[1]:02x}{codes[0]:02x} want 0x{exp:03x}"
        status = (await read_status(dut))[2]
        assert bool(status & 0x08) == bad and uo_bit(dut, UO_ILL) == int(bad), f"byte {b}: ILLEGAL flag wrong"
        if bad:
            await xfer(dut, [OP_CLEAR])
            assert uo_bit(dut, UO_ILL) == 0, "CLEAR did not clear ILLEGAL"


@cocotb.test()
async def test_random_matvec(dut):
    """Random weights/activations (incl. -128/127 edges), CMP_CLR and CMP, random shifts."""
    await start(dut)
    rng = random.Random(2026)
    m = TMAC()
    for trial in range(40 if not GL else 12):
        W = rand_weights(rng)
        await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
        for i, byte in enumerate(pack_matrix(W)):
            m.write_wbyte(i, byte)
        assert await read_codes(dut) == m.weight_codes(), f"trial {trial}: weight readback"
        edge = rng.random() < 0.3
        acts = [rng.choice((-128, 127, -1, 0)) if edge else rng.randint(-128, 127) for _ in range(COLS)]
        await xfer(dut, [OP_LOAD_A] + [x & 0xFF for x in acts])
        for i, x in enumerate(acts):
            m.write_act(i, x & 0xFF)
        clear = trial % 3 == 0
        await xfer(dut, [OP_CMP_CLR if clear else OP_CMP])
        m.compute(clear)
        m.shift = rng.choice((0, 0, 1, 3, 7, 8, 15, 23, 31))
        await xfer(dut, [OP_SHIFT, m.shift | (rng.randint(0, 7) << 5)])   # upper bits must be ignored
        await check_all(dut, m, f"trial {trial}")


@cocotb.test()
async def test_streamed_matvec(dut):
    """LOAD_AC streaming: y = W x for K = 10*nvec in one transaction per weight chunk,
    as a K-chunked matrix-vector product would run."""
    await start(dut)
    rng = random.Random(7)
    m = TMAC()
    await xfer(dut, [OP_CLEAR])
    for chunk in range(3):
        W = rand_weights(rng, p_zero=0.2)
        await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
        for i, byte in enumerate(pack_matrix(W)):
            m.write_wbyte(i, byte)
        nvec = 6 if not GL else 3
        stream = []
        for _ in range(nvec):
            acts = [rng.randint(-128, 127) for _ in range(COLS)]
            stream += [x & 0xFF for x in acts]
            for i, x in enumerate(acts):
                m.write_act(i, x & 0xFF)
            m.compute(clear=False)
        await xfer(dut, [OP_LOAD_AC] + stream)
        await check_all(dut, m, f"chunk {chunk}")


@cocotb.test()
async def test_shift_floor_semantics(dut):
    """Readout is floor(acc / 2^shift), not truncation: -1 >>> 1 = -1, -3 >>> 1 = -2."""
    await start(dut)
    m = TMAC()
    # rows 0-3 have a single +1 weight, in column r, so acc[r] = acts[r] = targets[r]; other rows are 0
    targets = [-1, -3, 5, -128] + [0] * (ROWS - 4)
    W = [[1 if c == r and r < 4 else 0 for c in range(COLS)] for r in range(ROWS)]
    acts = [targets[c] if c < ROWS else 0 for c in range(COLS)]
    await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
    await xfer(dut, [OP_LOAD_A] + [x & 0xFF for x in acts])
    await xfer(dut, [OP_CMP_CLR])
    for i, byte in enumerate(pack_matrix(W)):
        m.write_wbyte(i, byte)
    for i, x in enumerate(acts):
        m.write_act(i, x & 0xFF)
    m.compute(clear=True)
    assert m.acc == targets
    for sh in (0, 1, 2, 7, 8, 23, 31):
        m.shift = sh
        await xfer(dut, [OP_SHIFT, sh])
        got = await read_acc16(dut)
        assert got == [t >> sh for t in targets], f"shift {sh}: {got}"
        await check_all(dut, m, f"shift {sh}")


@cocotb.test()
async def test_readout_saturation(dut):
    """acc beyond int16 at shift 0 clamps to +32767 / -32768 on READ_ACC and sets the
    sticky RSAT status bit; READ_RAW still shows the exact 24-bit value. RSAT stays
    set after a rescale removes the clamp, until CLEAR."""
    await start(dut)
    m = TMAC()
    W = [[[1] * COLS, [-1] * COLS, [1] * COLS, [0] * COLS][r % 4] for r in range(ROWS)]
    acts = [127] * COLS
    await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
    await xfer(dut, [OP_LOAD_A] + acts)
    for i, byte in enumerate(pack_matrix(W)):
        m.write_wbyte(i, byte)
    for i, x in enumerate(acts):
        m.write_act(i, x)
    for n in range(32767 // (127 * COLS) + 2):            # just past int16 at shift 0
        await xfer(dut, [OP_CMP])
        m.compute(clear=False)
    assert m.read_acc16()[:2] == [32767, -32768] and m.any_rsat()
    await check_all(dut, m, "readout sat")
    assert m.rsat
    m.shift = 1                                           # rescaling removes the clamp...
    await xfer(dut, [OP_SHIFT, 1])
    assert not m.any_rsat()
    await check_all(dut, m, "readout sat, shift 1")
    assert m.rsat                                         # ...but RSAT is sticky
    await xfer(dut, [OP_CLEAR])
    m.clear()
    await check_all(dut, m, "after CLEAR")


# Skipped at gate level in CI (slow); set TMAC_GL_SLOW=1 to run it on the netlist too.
@cocotb.test(skip=GL and not os.environ.get("TMAC_GL_SLOW"))
async def test_internal_saturation(dut):
    """SLOW (RTL only): 24-bit accumulators clamp at +/-(2^23) and set sticky per-row SAT
    flags instead of wrapping; CLEAR resets them."""
    await start(dut)
    m = TMAC()
    # per compute: row 0 +127*COLS, rows 1-2 -127*COLS, row 3 about half (control row); repeats every 4 rows
    W = [[[1] * COLS, [-1] * COLS, [-1] * COLS, [1] * (COLS // 2) + [0] * (COLS - COLS // 2)][r % 4]
         for r in range(ROWS)]
    acts = [127] * COLS
    await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
    await xfer(dut, [OP_LOAD_A] + [x & 0xFF for x in acts])
    for i, byte in enumerate(pack_matrix(W)):
        m.write_wbyte(i, byte)
    for i, x in enumerate(acts):
        m.write_act(i, x & 0xFF)
    n = ACC_MAX // (127 * COLS) + 3
    for _ in range(n):
        await xfer(dut, [OP_CMP])
        m.compute(clear=False)
    assert m.acc[0] == ACC_MAX and m.acc[1] == ACC_MIN
    assert m.sat_flags == [r % 4 != 3 for r in range(ROWS)]
    await check_all(dut, m, "internal sat")
    assert uo_bit(dut, UO_SAT) == 1
    await xfer(dut, [OP_CLEAR])
    m.clear()
    await check_all(dut, m, "after CLEAR")
    assert uo_bit(dut, UO_SAT) == 0


@cocotb.test()
async def test_bist(dut):
    """BIST from the opcode and from the ui[3] pin: PASS, DONE, signature = model,
    accumulators left equal to the model's; SPI writes during BIST are ignored."""
    await start(dut)
    exp = bist_expected()
    for trigger in ("opcode", "pin"):
        if trigger == "opcode":
            await xfer(dut, [OP_BIST])
        else:
            set_ui(dut, bist=1)
            await ClockCycles(dut.clk, 4, rising=False)
            set_ui(dut, bist=0)
        # try to corrupt state while it runs: these must be ignored
        await xfer(dut, [OP_LOAD_W, 0x00])
        while uo_bit(dut, UO_BUSY):
            await ClockCycles(dut.clk, 16)
        assert uo_bit(dut, UO_DONE) == 1 and uo_bit(dut, UO_PASS) == 1, f"BIST ({trigger}) failed"
        sig = (await xfer(dut, [OP_READ_S], 2))[1:]
        assert sig[0] << 8 | sig[1] == exp, f"signature 0x{sig[0]:02x}{sig[1]:02x} != 0x{exp:04x}"
        m = TMAC()
        m.run_bist(exp)
        await check_all(dut, m, f"after BIST ({trigger})")


@cocotb.test()
async def test_spi_abort_and_reset(dut):
    """A transaction aborted mid-byte must not write anything; rst_n mid-transaction
    returns everything to the reset state."""
    await start(dut)
    m = TMAC()
    # abort: send LOAD_A opcode then only 4 bits of a data byte
    set_ui(dut, csn=0)
    await ClockCycles(dut.clk, HALF, rising=False)
    for byte, nbits in ((OP_LOAD_A, 8), (0x7F, 4)):
        for i in range(7, 7 - nbits, -1):
            set_ui(dut, mosi=(byte >> i) & 1)
            await ClockCycles(dut.clk, HALF, rising=False)
            set_ui(dut, sck=1)
            await ClockCycles(dut.clk, HALF, rising=False)
            set_ui(dut, sck=0)
    set_ui(dut, csn=1)
    await ClockCycles(dut.clk, HALF, rising=False)
    W = [[1] * COLS for _ in range(ROWS)]
    await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
    await xfer(dut, [OP_CMP_CLR])
    for i, byte in enumerate(pack_matrix(W)):
        m.write_wbyte(i, byte)
    m.compute(clear=True)                                  # activations still 0 -> acc 0
    await check_all(dut, m, "after abort")
    # now load something, then reset in the middle of a transaction
    await xfer(dut, [OP_LOAD_A] + [5] * COLS)
    await xfer(dut, [OP_CMP_CLR])
    await xfer(dut, [OP_SHIFT, 3])
    set_ui(dut, csn=0)
    await ClockCycles(dut.clk, 20, rising=False)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5, rising=False)
    dut.rst_n.value = 1
    set_ui(dut, csn=1)
    await ClockCycles(dut.clk, 10, rising=False)
    m.reset()
    assert await read_codes(dut) == m.weight_codes()
    await check_all(dut, m, "after mid-transaction reset")


# ================= v2: QSPI flash weight stream =================
class FlashModel:
    """Behavioural W25Q128JV on the TT QSPI Pmod pins (uio[0] /CS, [1] IO0, [2] IO1,
    [3] SCK, [4] IO2, [5] IO3). Fast Read Quad Output (6Bh) only. Protocol errors are
    collected in .errors; every flash test asserts it is empty."""

    IO_BITS = (1, 2, 4, 5)                 # uio bit for IO0..IO3

    def __init__(self, dut, image, t_valid=26.0, t_hold=2.0, seed=1):
        self.dut, self.image = dut, dict(image)
        self.t_valid, self.t_hold = t_valid, t_hold
        self.rng = random.Random(seed)
        self.errors, self.commands = [], []
        self.uio_in = 0
        self.t_desel = None
        self.task = cocotb.start_soon(self._run())

    def _drive(self, nib):
        v = self.uio_in
        for k, bit in enumerate(self.IO_BITS):
            v = (v & ~(1 << bit)) | (((nib >> k) & 1) << bit)
        self.uio_in = v
        self.dut.uio_in.value = v

    async def _later(self, delay, nib):
        await Timer(delay, unit="ns")
        self._drive(nib)

    async def _run(self):
        dut = self.dut
        # Start from the pins as they are now. (Taking the first *change* as the
        # baseline swallowed a /CS fall when FSTART was the first command.)
        vo = dut.uio_out.value
        prev = (int(vo) & 1, (int(vo) >> 3) & 1) if vo.is_resolvable else None
        sel = False
        while True:
            await dut.uio_out.value_change
            vo = dut.uio_out.value
            ve = dut.uio_oe.value
            if not (vo.is_resolvable and ve.is_resolvable):
                continue
            out, oe = int(vo), int(ve)
            csn, sck, io0 = out & 1, (out >> 3) & 1, (out >> 1) & 1
            if prev is None:
                prev = (csn, sck)
                continue
            pcsn, psck = prev
            prev = (csn, sck)
            if (oe & 0b11001001) != 0b11001001:
                self.errors.append(f"/CS, SCK or a RAM /CS not driven (oe 0x{oe:02x})")
            if ((out >> 6) & 3) != 3:
                self.errors.append("a PSRAM /CS went low")
            if pcsn != csn:
                now = get_sim_time("ns")
                if sck:
                    self.errors.append("SCK high on a /CS edge (not mode 0)")
                if csn == 0:
                    sel, self.nbits, self.sreg, self.falls = True, 0, 0, 0
                    if self.t_desel is not None and now - self.t_desel < 10:
                        self.errors.append("/CS high for < 10 ns between commands (tSHSL1)")
                else:
                    sel = False
                    self.t_desel = now
                continue
            if not sel or psck == sck:
                continue
            if sck:                            # rising edge: the flash samples IO0
                if self.nbits < 32:
                    if not (oe >> 1) & 1:
                        self.errors.append(f"IO0 not driven at command bit {self.nbits}")
                    self.sreg = (self.sreg << 1) | io0
                    self.nbits += 1
                    if self.nbits == 32:
                        cmd, addr = self.sreg >> 24, self.sreg & 0xFFFFFF
                        self.commands.append((cmd, addr))
                        if cmd != 0x6B:
                            self.errors.append(f"unexpected instruction 0x{cmd:02x}")
                        self.addr = addr
            else:                              # falling edge: the flash shifts out
                self.falls += 1
                if self.falls >= 40:
                    if (oe >> 1) & 1:
                        self.errors.append("IO0 still driven by the chip during data out")
                    n = self.falls - 40
                    byte = self.image.get(self.addr + n // 2, FLASH_ERASED)
                    nib = (byte >> 4) if n % 2 == 0 else (byte & 0xF)
                    cocotb.start_soon(self._later(self.t_hold, self.rng.randrange(16)))
                    cocotb.start_soon(self._later(self.t_valid, nib))


async def fstat(dut):
    rx = await xfer(dut, [OP_FSTAT], 2)
    return rx[1], rx[2]


async def wait_ready(dut, limit=20000):
    for _ in range(limit // 16):
        if uo_bit(dut, UO_FRDY):
            return
        await ClockCycles(dut.clk, 16)
    raise AssertionError("FLASH_READY never rose")


async def check_flash(dut, m, tag=""):
    await check_all(dut, m, tag)
    st, cfg = await fstat(dut)
    assert st == m.fstat(), f"{tag}: FSTAT 0x{st:02x} != model 0x{m.fstat():02x}"
    assert cfg == m.fcfg(), f"{tag}: FCFG 0x{cfg:02x} != model 0x{m.fcfg():02x}"
    assert uo_bit(dut, UO_FRDY) == int(m.f_ready), f"{tag}: FLASH_READY pin"


def rand_matrix(rng, n, k, p_zero=1 / 3):
    return [[0 if rng.random() < p_zero else rng.choice((-1, 1)) for _ in range(k)] for _ in range(n)]


def image_at(page, data):
    return {(page << 8) + i: b for i, b in enumerate(data)}


async def run_matvec(dut, m, flash, W, x, page, tag):
    """y = W x through the flash stream: one FSTART, then per row group CLEAR, LOAD_FC, read."""
    _, groups, chunks = pack_stream(W)
    xp = [v & 0xFF for v in x] + [0] * (chunks * COLS - len(x))
    await xfer(dut, [OP_FSTART, page >> 8, page & 0xFF])
    m.fstart(page)
    await wait_ready(dut)
    ys = []
    for g in range(groups):
        await xfer(dut, [OP_CLEAR])
        m.clear()
        await xfer(dut, [OP_LOAD_FC] + xp)
        m.load_fc(xp)
        await wait_ready(dut)
        await check_flash(dut, m, f"{tag} group {g}")
        ys += m.acc
    for i, row in enumerate(W):                      # the model against plain arithmetic
        assert ys[i] == sum(w * v for w, v in zip(row, x)), f"{tag}: model row {i}"
    assert not flash.errors, f"{tag}: flash protocol: {flash.errors[:3]}"
    return ys


@cocotb.test()
async def test_flash_pins_reset(dut):
    """After reset: flash /CS and both PSRAM /CS driven high, SCK low, IO0 released,
    FSTAT shows a closed stream and the default config (/4, sample 2 clk)."""
    await start(dut)
    m = TMAC2()
    assert int(dut.uio_oe.value) == 0b11001001, f"uio_oe 0x{int(dut.uio_oe.value):02x}"
    assert int(dut.uio_out.value) & 0b11001001 == 0b11000001, f"uio_out 0x{int(dut.uio_out.value):02x}"
    await check_flash(dut, m, "reset")


@cocotb.test()
async def test_flash_matvec(dut):
    """Matrix-vector products with weights streamed from flash, ragged N and K (padding),
    at every SCK divider; FSTART is issued right before LOAD_FC, so at the slow dividers
    the first compute has to wait for its weights."""
    await start(dut)
    rng = random.Random(42)
    shapes = [(8, 10), (10, 13), (4, 5)] if not GL else [(6, 7)]
    divs = (1, 0, 2, 3) if not GL else (1, 3)
    image, plan, page = {}, [], 0x0012
    for n, k in shapes:
        W = rand_matrix(rng, n, k)
        data, _, _ = pack_stream(W)
        image.update(image_at(page, data))
        plan.append((W, page))
        page += (len(data) + 255) // 256
    flash = FlashModel(dut, image)
    m = TMAC2(image)
    for div in divs:
        samp = 1 if div < 3 else 3                # 40 ns, or 80 ns at /16
        await xfer(dut, [OP_FCFG, samp << 2 | div])
        m.f_div, m.f_samp = div, samp
        for W, pg in plan:
            x = [rng.choice((-128, 127)) if rng.random() < 0.2 else rng.randint(-128, 127) for _ in W[0]]
            await run_matvec(dut, m, flash, W, x, pg, f"div {div} {len(W)}x{len(W[0])}")
    assert [c for c, _ in flash.commands] == [0x6B] * len(flash.commands)


# (div, samp, t_valid ns): sample time = (samp+1)*20 ns after the clock that drops SCK.
# It must be >= t_valid and < one SCK period + t_hold (2 ns), or the garbage is sampled.
TIMING_CASES = [(0, 0, 26.0), (0, 1, 26.0), (0, 2, 26.0), (1, 0, 26.0), (1, 1, 26.0),
                (1, 3, 26.0), (3, 1, 26.0), (0, 0, 10.0), (1, 2, 50.0), (1, 1, 50.0)]


@cocotb.test()
async def test_flash_timing_windows(dut):
    """The sample-delay setting is what makes the flash readable: for each SCK divider and
    round-trip delay, exactly the settings predicted by the timing budget read correct weights."""
    await start(dut)
    rng = random.Random(5)
    data = [rng.randrange(243) for _ in range(NBYTES)]
    for div, samp, t_valid in (TIMING_CASES if not GL else TIMING_CASES[:4]):
        period = (2 << div) * 20.0
        expect = t_valid <= (samp + 1) * 20.0 < period + 2.0
        flash = FlashModel(dut, image_at(1, data), t_valid=t_valid, seed=div * 7 + samp)
        m = TMAC2(image_at(1, data))
        await xfer(dut, [OP_FSTOP])
        await xfer(dut, [OP_FCFG, samp << 2 | div])
        await xfer(dut, [OP_FSTART, 0, 1])
        await wait_ready(dut)
        m.f_div, m.f_samp = div, samp
        m.fstart(1)
        ok = await read_codes(dut) == m.weight_codes()
        flash.task.cancel()
        assert ok == expect, (f"div {div} samp {samp} t_valid {t_valid}: read {'ok' if ok else 'bad'}, "
                              f"budget predicts {'ok' if expect else 'bad'}")
        assert not flash.errors, flash.errors[:3]
        await xfer(dut, [OP_CLEAR])                # garbage reads may have set ILLEGAL


@cocotb.test()
async def test_flash_wait_and_overrun(dut):
    """LOAD_FC with no open stream: the compute waits (PEND), accumulators do not change,
    and the next activation sets the sticky OVERRUN flag. CLEAR clears both."""
    await start(dut)
    m = TMAC2()
    await xfer(dut, [OP_LOAD_FC] + [1] * COLS)
    st, _ = await fstat(dut)
    assert st & 0x10 and not st & 0x08, f"after one vector: FSTAT 0x{st:02x}"
    await xfer(dut, [OP_LOAD_FC, 3])
    st, _ = await fstat(dut)
    assert st & 0x18 == 0x18, f"after an extra activation: FSTAT 0x{st:02x}"
    assert await read_raw(dut) == [0] * ROWS
    await xfer(dut, [OP_CLEAR])
    st, _ = await fstat(dut)
    assert st & 0x18 == 0, f"after CLEAR: FSTAT 0x{st:02x}"
    m.write_act(0, 3)
    for i in range(1, COLS):
        m.write_act(i, 1)
    await check_flash(dut, m, "after CLEAR")


@cocotb.test()
async def test_flash_stop_restart_bist(dut):
    """FSTOP closes the stream; FSTART at a new page restarts from that page's first chunk;
    BIST closes an open stream and still passes; v1 LOAD_W works after the stream closes."""
    await start(dut)
    rng = random.Random(9)
    A, B = rand_matrix(rng, 4, 10), rand_matrix(rng, 4, 10)
    da, _, _ = pack_stream(A)
    db, _, _ = pack_stream(B)
    image = {**image_at(0x0100, da), **image_at(0x0200, db)}
    flash = FlashModel(dut, image)
    m = TMAC2(image)
    x = [rng.randint(-128, 127) for _ in range(10)]
    await xfer(dut, [OP_FSTART, 0x01, 0x00])
    m.fstart(0x0100)
    await wait_ready(dut)
    await xfer(dut, [OP_LOAD_FC] + [v & 0xFF for v in x[:5]])     # first chunk only
    m.load_fc([v & 0xFF for v in x[:5]])
    await wait_ready(dut)
    await check_flash(dut, m, "A, one chunk")
    await xfer(dut, [OP_FSTOP])
    m.fstop()
    await check_flash(dut, m, "after FSTOP")
    ys = await run_matvec(dut, m, flash, B, x, 0x0200, "B after restart")
    assert ys[:4] == [sum(w * v for w, v in zip(row, x)) for row in B]
    # BIST while the stream is open
    await xfer(dut, [OP_BIST])
    while uo_bit(dut, UO_BUSY):
        await ClockCycles(dut.clk, 16)
    assert uo_bit(dut, UO_PASS) == 1, "BIST failed with the flash stream open"
    st, _ = await fstat(dut)
    assert not st & 0x80, f"BIST left the stream open: FSTAT 0x{st:02x}"
    # v1 path still works
    W = rand_weights(rng)
    await xfer(dut, [OP_LOAD_W] + pack_matrix(W))
    m2 = TMAC()
    for i, byte in enumerate(pack_matrix(W)):
        m2.write_wbyte(i, byte)
    assert await read_codes(dut) == m2.weight_codes()
    assert not flash.errors, flash.errors[:3]
