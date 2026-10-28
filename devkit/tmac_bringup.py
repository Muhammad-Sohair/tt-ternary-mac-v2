# SPDX-License-Identifier: Apache-2.0
"""TMAC2 silicon bring-up for the Tiny Tapeout demo board (MicroPython).

Copy this file and test/tmac_model.py to the board, plug the TT QSPI Pmod into the
bidirectional Pmod header, then:
    import tmac_bringup; tmac_bringup.run_all(tt)        # v1 stages S1-S8 (no Pmod needed)
    tmac_bringup.run_flash(tt)                           # flash stages F1-F5

Board API used (tt-micropython-firmware README and src/ttboard): tt.shuttle.<name>.enable(),
tt.shuttle.disable() (selects project 0, tt_um_chip_rom, whose uio_oe is 0), tt.ui_in[n] = v,
tt.uo_out[n], tt.uio_in[n] = v / tt.uio_in.value = v (drive a bidir pin), tt.uio_out[n] (read
it), tt.uio_oe_pico.value (which bidir pins the RP2 drives), tt.clock_project_PWM(hz),
tt.reset_project(bool). `tt` is the DemoBoard object the firmware creates at boot, in
RPMode.ASIC_RP_CONTROL.

SPI to the chip is bit-banged with explicit delays so that each SCK phase is >= 8 project
clocks (the chip requires f_SCK <= f_clk/16). The S stages map to notes/05_silicon_test_plan.md.

The F stages need the flash. TMAC2 drives flash /CS, SCK and both PSRAM /CS whenever it is
selected, so the RP2 can only reach the flash with another project selected: flash_board()
selects project 0 and drives the Pmod itself, flash_chip() hands the pins back and selects TMAC2.
test/test_devkit.py runs this file against the RTL (S1, S3, F2-F5) and a bit-level W25Q model.
"""

import time

import tmac_model as M

PROJECT = "tt_um_msohair_tmac2"
PROJECT_HZ = 1_000_000          # bring-up clock; raised in stages S8 and F4
HALF_US = 20                    # >= 8 clocks at 1 MHz, with margin for Python jitter

SCK, CSN, MOSI, BIST = 0, 1, 2, 3
MISO, FRDY, DONE, PASS_, ILL, SAT, BUSY, ACT = 0, 1, 2, 3, 4, 5, 6, 7

# QSPI Pmod on uio (TT QSPI Pmod pinout)
FL_CS, FL_IO0, FL_IO1, FL_SCK, FL_IO2, FL_IO3, RAM_A, RAM_B = range(8)
FL_IDLE = 1 << FL_CS | 1 << FL_IO2 | 1 << FL_IO3 | 1 << RAM_A | 1 << RAM_B   # /WP, /HOLD high
FL_DRIVE = 0xFF & ~(1 << FL_IO1)                                           # IO1 is the flash's DO

OP_LOAD_FC, OP_FCFG, OP_FSTART, OP_FSTOP, OP_FSTAT = 0x22, 0x90, 0x91, 0x92, 0x93
FCFG_RESET = 0x05               # sample 2 clk, SCK = clk/4
FLASH_PAGE = 0x0001             # F2 image at 0x000100 (as in docs/info.md); F3, F4 at pages 0x0010, 0x0040:
                                # each stage has its own 4 KB sector because fl_write erases whole sectors


def setup(tt, hz=PROJECT_HZ):
    getattr(tt.shuttle, PROJECT).enable()
    tt.ui_in.value = 0b0010                      # CS_n high, SCK low, BIST low
    tt.clock_project_PWM(hz)
    reset(tt)


def reset(tt):
    """Each stage starts from reset: BIST flags, the readout shift and the stream outlive CLEAR."""
    tt.reset_project(True)
    time.sleep_ms(10)
    tt.reset_project(False)
    time.sleep_ms(10)


def xfer(tt, tx, nread=0, half_us=HALF_US):
    rx = []
    tt.ui_in[CSN] = 0
    time.sleep_us(half_us)
    for byte in list(tx) + [0] * nread:
        got = 0
        for i in range(7, -1, -1):
            tt.ui_in[MOSI] = (byte >> i) & 1
            time.sleep_us(half_us)
            got = (got << 1) | int(tt.uo_out[MISO])
            tt.ui_in[SCK] = 1
            time.sleep_us(half_us)
            tt.ui_in[SCK] = 0
        rx.append(got)
    time.sleep_us(half_us)
    tt.ui_in[CSN] = 1
    tt.ui_in[MOSI] = 0
    time.sleep_us(half_us)
    return rx


def s16(v):
    return v - 65536 if v & 0x8000 else v


def s24(v):
    return v - (1 << 24) if v & 0x800000 else v


def read_raw(tt):
    rx = xfer(tt, [0x41], 3 * M.ROWS)
    return [s24(rx[1 + 3 * r] << 16 | rx[2 + 3 * r] << 8 | rx[3 + 3 * r]) for r in range(M.ROWS)]


def read_state(tt):
    raw = read_raw(tt)
    rx = xfer(tt, [0x40], 2 * M.ROWS)
    a16 = [s16(rx[1 + 2 * r] << 8 | rx[2 + 2 * r]) for r in range(M.ROWS)]
    sat_b, status = xfer(tt, [0x00], 2)[1:]
    return raw, a16, sat_b, status


def compare(tt, m, tag):
    raw, a16, sat_b, status = read_state(tt)
    m.note_read_acc()
    ok = raw == m.acc and a16 == m.read_acc16() and sat_b == m.sat_byte() and status == m.status()
    if not ok:
        print(tag, "MISMATCH raw", raw, "model", m.acc, "a16", a16, "model", m.read_acc16(),
              "sat 0x%02x/0x%02x status 0x%02x/0x%02x" % (sat_b, m.sat_byte(), status, m.status()))
    return ok


class Rng:                          # MicroPython has no random.Random; tiny LCG instead
    def __init__(self, seed):
        self.s = seed

    def next(self, n):
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return self.s % n


# ---------------- stages S1-S8: the v1 datapath (see the test plan) ----------------
def s1_alive(tt):
    rx = xfer(tt, [0x00], 2)
    print("S1 status during opcode: 0x%02x (want 0x81), pins uo=0x%02x" % (rx[0], int(tt.uo_out.value)))
    return rx[0] == 0x81


def s2_bist_pin(tt):
    tt.ui_in[BIST] = 1
    time.sleep_ms(1)
    tt.ui_in[BIST] = 0
    time.sleep_ms(5)                                 # BIST: 32 x (NBYTES+COLS+3) clocks, < 1 ms at 1 MHz
    done, ok = int(tt.uo_out[DONE]), int(tt.uo_out[PASS_])
    sig = xfer(tt, [0x61], 2)[1:]
    sig = sig[0] << 8 | sig[1]
    print("S2 BIST pin: DONE=%d PASS=%d signature 0x%04x (want 0x%04x)" % (done, ok, sig, M.bist_expected()))
    return done == 1 and ok == 1 and sig == M.bist_expected()


def s3_hand_example(tt):
    xfer(tt, [0x70])
    W = [[1] * M.COLS] + [[0] * M.COLS for _ in range(M.ROWS - 1)]   # bytes 0xF2 (all +1), 0x79 (all 0)
    xfer(tt, [0x10] + M.pack_matrix(W))
    xfer(tt, [0x20] + list(range(1, M.COLS + 1)))
    xfer(tt, [0x31])
    raw = read_raw(tt)
    want = [M.COLS * (M.COLS + 1) // 2, 0, 0, 0]
    print("S3 hand example: raw", raw, "(want %s)" % want)
    return raw == want


def s4_decoder(tt):
    bad = []
    for b in range(256):
        xfer(tt, [0x10, b])
        codes = xfer(tt, [0x50], 2)[1:]
        ws, illegal = M.decode(b)
        want = 0
        for k, w in enumerate(ws):
            want |= M.CODE[w] << (2 * k)
        st = xfer(tt, [0x00], 2)[2]
        if (codes[0] | codes[1] << 8) & 0x3FF != want or bool(st & 0x08) != illegal:
            bad.append(b)
        if illegal:
            xfer(tt, [0x70])
    print("S4 decoder: %d/256 bytes wrong %s" % (len(bad), bad[:16]))
    return not bad


def s5_random(tt, trials=200, seed=1):
    rng, m, fails = Rng(seed), M.TMAC(), 0
    xfer(tt, [0x70])
    for t in range(trials):
        W = [[rng.next(3) - 1 for _ in range(M.COLS)] for _ in range(M.ROWS)]
        wb = M.pack_matrix(W)
        xfer(tt, [0x10] + wb)
        for i, b in enumerate(wb):
            m.write_wbyte(i, b)
        acts = [rng.next(256) for _ in range(M.COLS)]
        xfer(tt, [0x20] + acts)
        for i, a in enumerate(acts):
            m.write_act(i, a)
        clear = t % 3 == 0
        xfer(tt, [0x31 if clear else 0x30])
        m.compute(clear)
        m.shift = rng.next(32)
        xfer(tt, [0x80, m.shift])
        if not compare(tt, m, "S5 trial %d" % t):
            fails += 1
    print("S5 random matvec: %d/%d trials failed" % (fails, trials))
    return fails == 0


def s6_saturation(tt):
    m = M.TMAC()
    xfer(tt, [0x70])
    W = [[1] * M.COLS, [-1] * M.COLS, [1] * M.COLS, [0] * M.COLS]
    xfer(tt, [0x10] + M.pack_matrix(W))
    xfer(tt, [0x20] + [127] * M.COLS)
    for i, b in enumerate(M.pack_matrix(W)):
        m.write_wbyte(i, b)
    for i in range(M.COLS):
        m.write_act(i, 127)
    for _ in range(32767 // (127 * M.COLS) + 2):      # just past int16 at shift 0
        xfer(tt, [0x30])
        m.compute(False)
    ok = compare(tt, m, "S6 readout sat")
    print("S6 readout saturation:", "ok" if ok else "FAIL")
    return ok


def s7_streamed(tt, chunks=4, nvec=8, seed=3):
    rng, m = Rng(seed), M.TMAC()
    xfer(tt, [0x70])
    for c in range(chunks):
        W = [[rng.next(3) - 1 for _ in range(M.COLS)] for _ in range(M.ROWS)]
        xfer(tt, [0x10] + M.pack_matrix(W))
        for i, b in enumerate(M.pack_matrix(W)):
            m.write_wbyte(i, b)
        stream = []
        for _ in range(nvec):
            acts = [rng.next(256) for _ in range(M.COLS)]
            stream += acts
            for i, a in enumerate(acts):
                m.write_act(i, a)
            m.compute(False)
        xfer(tt, [0x21] + stream)
    ok = compare(tt, m, "S7 streamed")
    print("S7 streamed K=%d matvec:" % (chunks * nvec * M.COLS), "ok" if ok else "FAIL")
    return ok


def s8_clock_sweep(tt, freqs=(1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000)):
    """BIST at increasing project clocks: BIST runs entirely on-chip, so the highest
    passing frequency is the datapath's real Fmax at this board's voltage/temperature."""
    results = {}
    for hz in freqs:
        tt.clock_project_PWM(hz)
        time.sleep_ms(5)
        tt.ui_in[BIST] = 1
        time.sleep_ms(1)
        tt.ui_in[BIST] = 0
        time.sleep_ms(5)
        results[hz] = (int(tt.uo_out[DONE]), int(tt.uo_out[PASS_]))
        print("S8 BIST @ %d Hz: DONE=%d PASS=%d" % ((hz,) + results[hz]))
    tt.clock_project_PWM(PROJECT_HZ)
    return results


def run_all(tt):
    """Pass the firmware's DemoBoard object: tmac_bringup.run_all(tt)."""
    setup(tt)
    out = {}
    for name, fn in (("S1", s1_alive), ("S2", s2_bist_pin), ("S3", s3_hand_example), ("S4", s4_decoder),
                     ("S5", s5_random), ("S6", s6_saturation), ("S7", s7_streamed)):
        reset(tt)
        out[name] = fn(tt)
    out["S8"] = s8_clock_sweep(tt)
    print("SUMMARY", out)
    return out


# ---------------- the RP2 as flash programmer (project 0 selected) ----------------
def flash_board(tt):
    """Select project 0 (uio_oe = 0) and drive the Pmod from the RP2: flash idle, PSRAMs deselected."""
    tt.shuttle.disable()
    tt.uio_in.value = FL_IDLE                    # MicroPython keeps the value for when the pins turn output
    tt.uio_oe_pico.value = FL_DRIVE
    tt.uio_in.value = FL_IDLE


def flash_chip(tt, hz=PROJECT_HZ):
    """Release the Pmod and select TMAC2 again (reset; FCFG back to 0x05)."""
    tt.uio_oe_pico.value = 0
    setup(tt, hz)


def fl_xfer(tt, tx, nread=0):
    """One single-SPI flash command, mode 0: IO0 changes while SCK is low, IO1 is read before the rise."""
    rx = []
    tt.uio_in[FL_CS] = 0
    for byte in list(tx) + [0] * nread:
        got = 0
        for i in range(7, -1, -1):
            tt.uio_in[FL_IO0] = (byte >> i) & 1
            got = (got << 1) | int(tt.uio_out[FL_IO1])
            tt.uio_in[FL_SCK] = 1
            tt.uio_in[FL_SCK] = 0
        rx.append(got)
    tt.uio_in[FL_CS] = 1
    return rx[len(tx):]


def fl_wait(tt, limit_ms=2000):
    t = 0
    while fl_xfer(tt, [0x05], 1)[0] & 1:           # SR1 bit 0 = BUSY
        time.sleep_ms(1)
        t += 1
        if t > limit_ms:
            raise RuntimeError("flash stays BUSY")


def fl_addr(a):
    return [(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF]


def fl_read(tt, addr, n):
    return fl_xfer(tt, [0x03] + fl_addr(addr), n)


def fl_write(tt, addr, data):
    """Erase the 4 KB sectors that addr..addr+len(data) touches, program by page, read back."""
    for sector in range(addr & ~0xFFF, addr + len(data), 0x1000):
        fl_xfer(tt, [0x06])                            # WRITE ENABLE
        fl_xfer(tt, [0x20] + fl_addr(sector))          # SECTOR ERASE 4 KB
        fl_wait(tt)
    i = 0
    while i < len(data):
        n = min(256 - ((addr + i) & 0xFF), len(data) - i)
        fl_xfer(tt, [0x06])
        fl_xfer(tt, [0x02] + fl_addr(addr + i) + list(data[i:i + n]))   # PAGE PROGRAM
        fl_wait(tt)
        i += n
    back = fl_read(tt, addr, len(data))
    if back != list(data):
        bad = [j for j in range(len(data)) if back[j] != data[j]]
        raise RuntimeError("flash verify failed at %d bytes, first offset %d" % (len(bad), bad[0]))


def fl_set_qe(tt):
    """Set QE (SR2 bit 1) non-volatile: needed by Fast Read Quad Output. TT store Pmods ship with it set."""
    sr2 = fl_xfer(tt, [0x35], 1)[0]
    fl_xfer(tt, [0x06])
    fl_xfer(tt, [0x31, sr2 | 0x02])                    # WRITE STATUS REGISTER-2
    fl_wait(tt)
    return fl_xfer(tt, [0x35], 1)[0]


# ---------------- TMAC2 side of the flash stream ----------------
def fstat(tt):
    st, cfg = xfer(tt, [OP_FSTAT], 2)[1:]
    return st, cfg


def wait_ready(tt, limit_ms=100):
    for _ in range(limit_ms):
        if int(tt.uo_out[FRDY]):
            return True
        time.sleep_ms(1)
    return False


def flash_matvec(tt, m, W, x, page, tag):
    """y = W x from the flash stream: FSTART page, then per row group CLEAR, LOAD_FC, READ_RAW.
    m is a TMAC2 model holding the same flash image; returns (ok, rows read)."""
    _, groups, chunks = M.pack_stream(W)
    xp = [v & 0xFF for v in x] + [0] * (chunks * M.COLS - len(x))
    xfer(tt, [OP_FSTART, page >> 8, page & 0xFF])
    m.fstart(page)
    ok, ys = wait_ready(tt), []
    for g in range(groups):
        xfer(tt, [0x70])
        m.clear()
        xfer(tt, [OP_LOAD_FC] + xp)
        m.load_fc(xp)
        ok = wait_ready(tt) and ok
        raw = read_raw(tt)
        st = fstat(tt)
        if raw != m.acc or st != (m.fstat(), m.fcfg()):
            print(tag, "group %d MISMATCH raw" % g, raw, "model", m.acc,
                  "FSTAT 0x%02x/0x%02x model 0x%02x/0x%02x" % (st + (m.fstat(), m.fcfg())))
            ok = False
        ys += raw
    for i, row in enumerate(W):                         # the model against plain arithmetic
        if ok and ys[i] != sum(w * v for w, v in zip(row, x)):
            print(tag, "row %d: model disagrees with arithmetic" % i)
            ok = False
    return ok, ys[:len(W)]


def rand_w(rng, n, k):
    return [[rng.next(3) - 1 for _ in range(k)] for _ in range(n)]


def rand_x(rng, k):
    return [rng.next(256) - 128 for _ in range(k)]


# ---------------- stages F1-F5: the flash path ----------------
def f1_probe(tt, set_qe=False):
    """JEDEC ID (W25Q128JV: EF 40 18 or EF 70 18) and the QE bit, with project 0 selected."""
    flash_board(tt)
    jid = fl_xfer(tt, [0x9F], 3)
    sr2 = fl_xfer(tt, [0x35], 1)[0]
    if set_qe and not sr2 & 0x02:
        sr2 = fl_set_qe(tt)
    qe = bool(sr2 & 0x02)
    print("F1 flash ID %02x %02x %02x (want ef 40/70 18), SR2 0x%02x, QE=%d%s"
          % (jid[0], jid[1], jid[2], sr2, qe, "" if qe else "  <- run f1_probe(tt, set_qe=True)"))
    return jid[0] == 0xEF and jid[2] == 0x18 and qe


def f2_hand_example(tt):
    """S3 again, with the four weight bytes read from flash instead of sent over SPI."""
    W = [[1] * M.COLS] + [[0] * M.COLS for _ in range(M.ROWS - 1)]
    data = M.pack_matrix(W)                                         # F2 79 79 79
    flash_board(tt)
    fl_write(tt, FLASH_PAGE << 8, data)
    flash_chip(tt)
    st0 = fstat(tt)
    xfer(tt, [OP_FSTART, FLASH_PAGE >> 8, FLASH_PAGE & 0xFF])
    rdy = wait_ready(tt)
    xfer(tt, [0x70])
    xfer(tt, [OP_LOAD_FC] + list(range(1, M.COLS + 1)))
    raw = read_raw(tt)
    want = [M.COLS * (M.COLS + 1) // 2, 0, 0, 0]
    print("F2 flash hand example: FSTAT after reset %02x %02x (want 02 05), READY=%d, raw" % (st0 + (rdy,)),
          raw, "(want %s)" % want)
    xfer(tt, [OP_FSTOP])
    return st0 == (0x02, FCFG_RESET) and rdy and raw == want


def f3_matvec(tt, seed=5, shapes=((8, 10), (10, 13), (4, 5)), page=0x0010):
    """Random matrices (ragged N and K) in flash; every SCK divider at its default sample point."""
    rng, plan, image = Rng(seed), [], {}
    for n, k in shapes:
        W = rand_w(rng, n, k)
        data = M.pack_stream(W)[0]
        plan.append((W, page))
        for i, b in enumerate(data):
            image[(page << 8) + i] = b
        page += (len(data) + 255) // 256
    flash_board(tt)
    lo = min(image)
    fl_write(tt, lo, [image.get(a, 0xFF) for a in range(lo, max(image) + 1)])
    flash_chip(tt)
    m, fails = M.TMAC2(image), 0
    for div in (1, 0, 2, 3):
        samp = 1 if div < 3 else 3
        xfer(tt, [OP_FCFG, samp << 2 | div])
        m.f_div, m.f_samp = div, samp
        for W, pg in plan:
            ok, _ = flash_matvec(tt, m, W, rand_x(rng, len(W[0])), pg, "F3 div %d %dx%d" % (div, len(W), len(W[0])))
            fails += not ok
    xfer(tt, [OP_FSTOP])
    m.fstop()
    print("F3 flash matvec: %d/%d runs failed" % (fails, 4 * len(plan)))
    return fails == 0


def f4_window(tt, hz=50_000_000, seed=7, page=0x0040):
    """The sample-point calibration: every (divider, sample delay) at project clock hz.
    Passing cells show where the pad/mux round trip lands; pick one inside the passing window.
    At 1 MHz everything passes; the window only means something near 50 MHz."""
    rng = Rng(seed)
    W = rand_w(rng, 8, 15)
    data = M.pack_stream(W)[0]
    image = {(page << 8) + i: b for i, b in enumerate(data)}
    flash_board(tt)
    fl_write(tt, page << 8, data)
    flash_chip(tt, hz)
    grid = {}
    for div in range(4):
        for samp in range(4):
            m = M.TMAC2(image)
            xfer(tt, [OP_FCFG, samp << 2 | div])
            m.f_div, m.f_samp = div, samp
            ok, _ = flash_matvec(tt, m, W, rand_x(rng, len(W[0])), page, "F4 div %d samp %d" % (div, samp))
            xfer(tt, [OP_FSTOP])
            grid[(div, samp)] = ok
    xfer(tt, [OP_FCFG, FCFG_RESET])
    tt.clock_project_PWM(PROJECT_HZ)
    print("F4 sample window @ %d Hz (rows: SCK clk/2 /4 /8 /16; columns: sample 1..4 clk)" % hz)
    for div in range(4):
        print("   /%-2d " % (2 << div) + " ".join("ok " if grid[(div, s)] else "-- " for s in range(4)))
    return grid


def f5_stop_and_bist(tt, page=FLASH_PAGE):
    """FSTOP closes the stream (/CS high); a BIST pulse closes it too and leaves the stream usable after."""
    xfer(tt, [OP_FSTART, page >> 8, page & 0xFF])
    wait_ready(tt)
    st_open = fstat(tt)[0]
    xfer(tt, [OP_FSTOP])
    st_stop = fstat(tt)[0]
    cs_high = int(tt.uio_out[FL_CS])
    xfer(tt, [OP_FSTART, page >> 8, page & 0xFF])
    wait_ready(tt)
    tt.ui_in[BIST] = 1
    time.sleep_ms(1)
    tt.ui_in[BIST] = 0
    time.sleep_ms(5)
    st_bist = fstat(tt)[0]
    bist_ok = int(tt.uo_out[PASS_])
    xfer(tt, [0x70])                                   # CLEAR, then the hand example again
    ok_after = f2_rerun(tt, page)
    print("F5 FSTAT open 0x%02x (want 0xa2), after FSTOP 0x%02x (want 0x02), /CS=%d, after BIST 0x%02x "
          "(want 0x02), BIST PASS=%d, stream after BIST %s" % (st_open, st_stop, cs_high, st_bist, bist_ok,
                                                              "ok" if ok_after else "FAIL"))
    return st_open == 0xA2 and st_stop == 0x02 and cs_high == 1 and st_bist == 0x02 and bist_ok and ok_after


def f2_rerun(tt, page):
    xfer(tt, [OP_FSTART, page >> 8, page & 0xFF])
    wait_ready(tt)
    xfer(tt, [OP_LOAD_FC] + list(range(1, M.COLS + 1)))
    raw = read_raw(tt)
    xfer(tt, [OP_FSTOP])
    return raw == [M.COLS * (M.COLS + 1) // 2, 0, 0, 0]


def run_flash(tt, set_qe=False, window_hz=50_000_000):
    """Flash stages. F1 must pass first: without the Pmod, or with QE = 0, the rest cannot work."""
    out = {"F1": f1_probe(tt, set_qe)}
    if not out["F1"]:
        flash_chip(tt)
        print("SUMMARY", out, "(stopped: fix F1 first)")
        return out
    out["F2"] = f2_hand_example(tt)
    out["F3"] = f3_matvec(tt)
    out["F4"] = f4_window(tt, window_hz)
    out["F5"] = f5_stop_and_bist(tt)
    print("SUMMARY", out)
    return out
