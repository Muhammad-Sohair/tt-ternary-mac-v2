# SPDX-License-Identifier: Apache-2.0
"""Runs the board script devkit/tmac_bringup.py, unmodified, against the RTL.

The script is blocking MicroPython-style code. It runs in a cocotb bridge thread and
talks to a FakeTT that has the slice of the tt-micropython-firmware API the script uses.
Every pin access becomes a short coroutine on the simulator thread.

The Pmod is modelled on both sides:
  * with TMAC2 selected, test.FlashModel answers the chip's 6Bh reads (and checks the protocol);
  * with project 0 selected (uio_oe = 0), W25Q is a bit-level single-SPI W25Q128JV that the
    script programs. Both share one memory dict, so the chip reads what the script wrote.
FakeTT records any bidir pin that the RP2 and TMAC2 would both drive.

Run: make COCOTB_TEST_MODULES=test_devkit   (RTL only; about 10 min with icarus)
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.task import bridge, resume
from cocotb.triggers import Timer

from test import FlashModel
from tmac_model import FLASH_ERASED

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "devkit"))
import tmac_bringup as B  # noqa: E402


# ---------------- simulator-side primitives (called from the script's thread) ----------------
@resume
async def _sleep_ns(ns):
    await Timer(max(int(ns), 1), unit="ns")


@resume
async def _write(sig, v):
    sig.value = v


@resume
async def _read(sig):
    return sig.value


class W25Q:
    """Single-SPI W25Q128JV, mode 0, enough for programming: 9Fh, 05h, 35h, 06h, 20h, 02h, 03h, 31h.
    Erase/program/status writes need WEL (06h) and act on /CS rising after whole bytes, like the part.
    Page program wraps inside its 256-byte page. BUSY stays set for a few status reads."""

    JEDEC = (0xEF, 0x40, 0x18)

    def __init__(self, mem, qe=True):
        self.mem, self.qe = mem, qe
        self.errors, self.log = [], []
        self.wel, self.busy = False, 0
        self.cs, self.sck, self.do = 1, 0, 1

    def pins(self, cs, sck, io0):
        if cs != self.cs:
            if cs == 0:
                self.rx, self.nbits, self.out, self.cmd = [], 0, [], None
                if sck:
                    self.errors.append("SCK high at /CS fall (not mode 0)")
            else:
                self._end()
            self.cs = cs
        elif not cs and sck != self.sck:
            if sck:                                   # rising: sample IO0
                if self.nbits % 8 == 0:
                    self.rx.append(0)
                self.rx[-1] = (self.rx[-1] << 1) | io0
                self.nbits += 1
                if self.nbits % 8 == 0:
                    self._byte()
            else:                                     # falling: shift out
                if not self.out:
                    self._more()
                self.do = self.out.pop(0) if self.out else 1
        self.sck = sck

    def _bits(self, byte):
        self.out += [(byte >> i) & 1 for i in range(7, -1, -1)]

    def _byte(self):
        n, cmd = len(self.rx), self.rx[0]
        if n == 1:
            self.cmd = cmd
            if self.busy and cmd not in (0x05, 0x35):
                self.errors.append(f"command 0x{cmd:02x} while BUSY")
            if cmd == 0x9F:
                for b in self.JEDEC:
                    self._bits(b)
        elif n == 4 and cmd == 0x03:
            self.addr = self.rx[1] << 16 | self.rx[2] << 8 | self.rx[3]

    def _more(self):                                 # continuous output for status and read
        if self.nbits < 8:                           # still inside the instruction byte
            return
        if self.cmd == 0x05:
            self._bits((1 if self.busy else 0) | (2 if self.wel else 0))
            self.busy = max(0, self.busy - 1)
        elif self.cmd == 0x35:
            self._bits(0x02 if self.qe else 0x00)
        elif self.cmd == 0x03 and self.nbits >= 32:
            self._bits(self.mem.get(self.addr, FLASH_ERASED))
            self.addr = (self.addr + 1) & 0xFFFFFF

    def _end(self):
        if self.nbits % 8:
            self.errors.append(f"/CS rose after {self.nbits} bits (not a whole byte)")
            return
        if not self.rx:
            return
        cmd, args = self.rx[0], self.rx[1:]
        self.log.append(cmd)
        if cmd == 0x06:
            self.wel = True
        elif cmd in (0x20, 0x02, 0x31):
            if not self.wel:
                self.errors.append(f"0x{cmd:02x} without WRITE ENABLE")
                return
            self.wel, self.busy = False, 3
            if cmd == 0x20 and len(args) == 3:
                base = (args[0] << 16 | args[1] << 8) & 0xFFF000
                for a in range(base, base + 0x1000):
                    self.mem.pop(a, None)
            elif cmd == 0x02 and len(args) >= 3:
                a0 = args[0] << 16 | args[1] << 8 | args[2]
                for i, b in enumerate(args[3:]):
                    a = (a0 & ~0xFF) | ((a0 + i) & 0xFF)
                    self.mem[a] = self.mem.get(a, FLASH_ERASED) & b
            elif cmd == 0x31 and len(args) == 1:
                self.qe = bool(args[0] & 0x02)


class Port:
    def __init__(self, get=None, put=None):
        self.get, self.put = get, put

    def __getitem__(self, n):
        return (self.get() >> n) & 1

    def __setitem__(self, n, v):
        self.put(n, v)

    @property
    def value(self):
        return self.get()

    @value.setter
    def value(self, v):
        self.put(None, v)


class FakeTime:
    """sleep_us/sleep_ms in simulated time. Above 2.5 MHz time is compressed so that the script's
    20 us SPI phase stays 50 project clocks (the chip needs >= 8); below it, time is real."""

    def __init__(self, tt):
        self.tt = tt

    def _scale(self):
        return min(1.0, 2.5e6 / self.tt.hz) if self.tt.hz else 1.0

    def sleep_us(self, us):
        _sleep_ns(us * 1000 * self._scale())

    def sleep_ms(self, ms):
        _sleep_ns(ms * 1e6 * self._scale())


class FakeTT:
    def __init__(self, dut, w25q):
        self.dut, self.w25q = dut, w25q
        self.selected, self.hz, self.clk_task = False, None, None    # clk_task: the running Clock
        self.ui, self.pico_v, self.pico_oe = 0b0010, 0, 0
        self.errors = []
        self.ui_in = Port(lambda: self.ui, self._put_ui)
        self.uo_out = Port(lambda: int(_read(dut.uo_out)))
        self.uio_in = Port(self._get_uio, self._put_uio)
        self.uio_out = Port(self._get_uio)
        self.uio_oe_pico = Port(lambda: self.pico_oe, self._put_oe)
        tt = self

        class Shuttle:
            def disable(self):
                tt.selected = False
                tt.pico_oe = 0                        # firmware: safe_bidir()
                _write(dut.ena, 0)

        class Design:
            def enable(self):
                Shuttle().disable()
                tt.selected = True
                _write(dut.ena, 1)

        self.shuttle = Shuttle()
        setattr(self.shuttle, B.PROJECT, Design())

    # -- pins --
    def _put_ui(self, n, v):
        self.ui = v if n is None else (self.ui & ~(1 << n)) | (v << n)
        _write(self.dut.ui_in, self.ui)

    def _put_uio(self, n, v):
        self.pico_v = v if n is None else (self.pico_v & ~(1 << n)) | (v << n)
        self._pmod()

    def _put_oe(self, n, v):
        self.pico_oe = v
        self._pmod()

    def _chip(self):
        """(uio_oe, uio_out) of TMAC2 as the pads see them: nothing unless it is selected."""
        if not self.selected:
            return 0, 0
        return int(_read(self.dut.uio_oe)), int(_read(self.dut.uio_out))

    def _pmod(self):
        if self.selected:
            oe, _ = self._chip()
            if oe & self.pico_oe:
                self.errors.append(f"RP2 and TMAC2 both drive uio 0x{oe & self.pico_oe:02x}")
            return
        v = self._get_uio()
        self.w25q.pins((v >> B.FL_CS) & 1, (v >> B.FL_SCK) & 1, (v >> B.FL_IO0) & 1)

    def _get_uio(self):
        oe, out = self._chip()
        v = 0xFF                                       # undriven pins read high (Pmod pull-ups assumed)
        v = (v & ~oe) | (out & oe)
        v = (v & ~self.pico_oe) | (self.pico_v & self.pico_oe)
        if not self.selected and not self.w25q.cs and not (self.pico_oe >> B.FL_IO1) & 1:
            v = (v & ~(1 << B.FL_IO1)) | (self.w25q.do << B.FL_IO1)
        return v

    # -- clock and reset --
    def clock_project_PWM(self, hz):
        self.hz = hz
        _restart_clock(self, hz)

    def reset_project(self, on):
        _write(self.dut.rst_n, 0 if on else 1)


@resume
async def _restart_clock(tt, hz):
    if tt.clk_task is not None:
        tt.clk_task.stop()
    tt.clk_task = Clock(tt.dut.clk, round(1e9 / hz), unit="ns")
    tt.clk_task.start()


def expected_window(t_valid, t_hold=2.0, clk_ns=20):
    """The timing budget of notes/v2_02 (as in test.TIMING_CASES): sample at (samp+1) clk after
    the SCK fall; good if the data is valid by then and the next fall has not replaced it."""
    return {(d, s): t_valid <= (s + 1) * clk_ns < (2 << d) * clk_ns + t_hold
            for d in range(4) for s in range(4)}


async def board(dut, qe=True):
    dut.ui_in.value = 0b0010
    dut.uio_in.value = 0
    dut.ena.value = 0
    dut.rst_n.value = 0
    mem = {}
    w25q = W25Q(mem, qe=qe)
    tt = FakeTT(dut, w25q)
    B.time = FakeTime(tt)
    flash = FlashModel(dut, {})
    flash.image = mem                                 # the chip reads what the script programmed
    return tt, w25q, flash


@cocotb.test()
async def test_devkit_s_stages(dut):
    """The v1 stages of the board script on the TMAC2 RTL (S4/S5 shortened, S8 skipped: no Fmax in RTL)."""
    tt, _, flash = await board(dut)

    def run():
        B.setup(tt)
        out = {}
        for name, fn in (("S1", B.s1_alive), ("S2", B.s2_bist_pin), ("S3", B.s3_hand_example),
                         ("S5", lambda t: B.s5_random(t, trials=12)), ("S6", B.s6_saturation),
                         ("S7", B.s7_streamed)):
            B.reset(tt)                                # as run_all does
            out[name] = fn(tt)
        return out

    out = await bridge(run)()
    dut._log.info(f"S stages: {out}")
    assert all(out.values()), out
    assert not tt.errors, tt.errors
    assert not flash.commands, "the chip touched the flash during the v1 stages"


@cocotb.test()
async def test_devkit_flash_stages(dut):
    """F1-F5 end to end: QE found clear stops the run; set_qe=True sets it; then programming,
    the flash hand example, random matvecs at every divider, the 50 MHz sample window (which must
    match the timing budget for the model's t_valid = 26 ns), and FSTOP/BIST closing the stream."""
    tt, w25q, flash = await board(dut, qe=False)

    first = await bridge(B.run_flash)(tt)
    assert first == {"F1": False}, first
    out = await bridge(B.run_flash)(tt, set_qe=True)
    dut._log.info(f"F stages: {out}")
    want = expected_window(flash.t_valid)
    assert out["F4"] == want, f"F4 grid {out['F4']} != budget {want}"
    assert any(want.values()) and not all(want.values())   # the grid can tell good from bad
    for k in ("F1", "F2", "F3", "F5"):
        assert out[k], f"{k} failed: {out}"
    assert w25q.qe and 0x31 in w25q.log, "QE was not set through WRITE STATUS REGISTER-2"
    assert not w25q.errors, w25q.errors[:5]
    assert not tt.errors, tt.errors[:5]
    assert not flash.errors, flash.errors[:5]
    assert {c for c, _ in flash.commands} == {0x6B}
