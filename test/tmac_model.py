# SPDX-License-Identifier: Apache-2.0
"""Bit-exact Python reference model of the TMAC chip.

This is the golden model the cocotb tests compare against, and the source of
the BIST signature baked into the RTL:

    python test/tmac_model.py --write-rtl     # rewrites the constants in src/tt_um_msohair_tmac.v

Every rule here mirrors one in the RTL; the comments name the file.
It runs unchanged under MicroPython on the devkit (devkit/tmac_bringup.py
imports it), so the host-only imports live inside the functions that use them.
"""

ROWS, COLS, ACC_W = 8, 5, 24
BPR = COLS // 5                     # packed bytes per row
NBYTES = ROWS * BPR
ACC_MAX, ACC_MIN = (1 << (ACC_W - 1)) - 1, -(1 << (ACC_W - 1))
BIST_ITERS, BIST_SEED = 32, 0xACE1

# 2-bit weight codes (tmac_core.v / FPGA ternary_pe.sv)
CODE = {-1: 0b11, 0: 0b00, +1: 0b01}
WEIGHT = {v: k for k, v in CODE.items()}


# ---------------- PACKING_FORMAT v1.0 ----------------
def pack5(ws):
    """Five weights in {-1,0,+1} -> one byte, d = w + 1, lowest index = least-significant digit."""
    assert len(ws) == 5
    return sum((w + 1) * 3 ** i for i, w in enumerate(ws))


def decode(byte):
    """trit_decoder.v: byte -> (five weights, illegal). Illegal bytes decode to all zero."""
    if byte > 242:
        return [0] * 5, True
    ws = []
    for _ in range(5):
        ws.append(byte % 3 - 1)
        byte //= 3
    return ws, False


def pack_matrix(W):
    """ROWS x COLS weights -> NBYTES packed bytes in load order (row-major, 5 columns per byte)."""
    out = []
    for r in range(ROWS):
        for b in range(BPR):
            out.append(pack5(W[r][5 * b:5 * b + 5]))
    return out


# ---------------- arithmetic ----------------
def sat(v, lo, hi):
    return (min(max(v, lo), hi), v > hi or v < lo)


def sat16_shift(acc, shift):
    """Readout requantisation, as the FPGA's sat16_shift: arithmetic shift
    (Python >> on ints is floor division, matching Verilog >>> on signed), then clamp."""
    v = acc >> shift
    return sat(v, -32768, 32767)


def to_s8(b):
    return b - 256 if b >= 128 else b


# ---------------- the chip ----------------
class TMAC:
    def __init__(self):
        self.reset()

    def reset(self):
        self.W = [[0] * COLS for _ in range(ROWS)]
        self.a = [0] * COLS
        self.acc = [0] * ROWS
        self.sat_flags = [False] * ROWS
        self.illegal = False
        self.rsat = False                   # sticky: a READ_ACC returned a clamped value
        self.shift = 0
        self.bist_done = self.bist_pass = False
        self.signature = 0

    # core operations
    def write_wbyte(self, idx, byte):
        ws, bad = decode(byte)
        r, b = divmod(idx % NBYTES, BPR)
        self.W[r][5 * b:5 * b + 5] = ws
        self.illegal |= bad

    def write_act(self, idx, byte):
        self.a[idx % COLS] = to_s8(byte)

    def dot(self, r):
        return sum(w * x for w, x in zip(self.W[r], self.a))

    def compute(self, clear):
        for r in range(ROWS):
            v = self.dot(r) + (0 if clear else self.acc[r])
            self.acc[r], s = sat(v, ACC_MIN, ACC_MAX)
            self.sat_flags[r] |= s

    def clear(self):
        self.acc = [0] * ROWS
        self.sat_flags = [False] * ROWS
        self.illegal = False
        self.rsat = False

    # readout views
    def read_acc16(self):
        return [sat16_shift(x, self.shift)[0] for x in self.acc]

    def any_rsat(self):
        """Would any row clamp on READ_ACC at the current shift?"""
        return any(sat16_shift(x, self.shift)[1] for x in self.acc)

    def note_read_acc(self):
        """Call after the chip has served a full READ_ACC: sets the sticky RSAT flag."""
        self.rsat |= self.any_rsat()

    def status(self, busy=False):
        return (0x80 | busy << 6 | self.bist_done << 5 | self.bist_pass << 4
                | self.illegal << 3 | any(self.sat_flags) << 2 | self.rsat << 1 | 1)

    def sat_byte(self):
        return sum(1 << r for r in range(ROWS) if self.sat_flags[r])

    def weight_codes(self):
        flat = [CODE[w] for row in self.W for w in row]
        out = []
        for j in range(0, len(flat), 4):
            grp = flat[j:j + 4] + [0] * (4 - len(flat[j:j + 4]))
            out.append(grp[0] | grp[1] << 2 | grp[2] << 4 | grp[3] << 6)
        return out

    # BIST (tmac_bist.v)
    def run_bist(self, expected=None):
        lfsr, sig = BIST_SEED, 0
        self.clear()                        # BIST start pulses acc_rst

        def nxt(x):
            return (x >> 1) ^ 0xB400 if x & 1 else x >> 1

        for it in range(BIST_ITERS):
            for i in range(NBYTES):
                raw = lfsr & 0xFF
                self.write_wbyte(i, raw - 243 if raw > 242 else raw)
                lfsr = nxt(lfsr)
            for i in range(COLS):
                self.write_act(i, lfsr & 0xFF)
                lfsr = nxt(lfsr)
            self.compute(clear=(it % 4 == 0))
            fold = 0
            for r in range(ROWS):
                u = self.acc[r] & ((1 << ACC_W) - 1)
                row16 = (u & 0xFFFF) ^ (u >> 16)
                k = r % 16
                fold ^= ((row16 << k) | (row16 >> ((16 - k) % 16))) & 0xFFFF
            fb = ((sig >> 15) ^ (sig >> 13) ^ (sig >> 12) ^ (sig >> 10)) & 1
            sig = (((sig << 1) & 0xFFFF) | fb) ^ fold
        self.signature = sig
        self.bist_done = True
        self.bist_pass = expected is None or sig == expected
        return sig


# ---------------- v2: weights streamed from QSPI flash ----------------
FLASH_ERASED = 0xFF                  # an erased flash byte (illegal as a weight byte)


def pack_stream(W):
    """Weight matrix (N x K, entries in {-1,0,+1}) -> flash bytes in the order LOAD_FC consumes them.

    Rows are taken in groups of ROWS (zero rows pad N), K is cut into chunks of COLS (zero columns
    pad K; the host pads the activations with zeros to match). For each group, for each chunk,
    one array load = NBYTES bytes in LOAD_W order. Returns (bytes, n_groups, n_chunks)."""
    n, k = len(W), len(W[0])
    groups = (n + ROWS - 1) // ROWS
    chunks = (k + COLS - 1) // COLS
    out = []
    for g in range(groups):
        for c in range(chunks):
            blk = []
            for r in range(ROWS):
                row = W[g * ROWS + r] if g * ROWS + r < n else [0] * k
                seg = row[c * COLS:(c + 1) * COLS]
                blk.append(seg + [0] * (COLS - len(seg)))
            out += pack_matrix(blk)
    return out, groups, chunks


class TMAC2(TMAC):
    """TMAC plus the flash weight stream (qspi_fetch.v, tt_um_msohair_tmac2.v)."""

    def __init__(self, flash=None):
        self.flash = dict(flash or {})        # byte address -> value; anything else reads erased
        super().__init__()

    def reset(self):
        super().reset()
        self.f_open = False
        self.f_ptr = 0
        self.f_ready = False
        self.f_pend = False
        self.f_overrun = False
        self.f_div, self.f_samp = 1, 1

    def _fetch_chunk(self):
        for i in range(NBYTES):
            self.write_wbyte(i, self.flash.get(self.f_ptr + i, FLASH_ERASED))
        self.f_ptr += NBYTES
        self.f_ready = True

    def fstart(self, page):
        self.f_open, self.f_ptr = True, page << 8
        self.f_pend = self.f_overrun = False
        self._fetch_chunk()

    def fstop(self):
        self.f_open = self.f_ready = self.f_pend = self.f_overrun = False

    def load_fc(self, acts):
        """LOAD_FC with activations that arrive slower than chunks (no overrun)."""
        for i, b in enumerate(acts):
            self.write_act(i, b)
            if i % COLS == COLS - 1:
                assert self.f_ready, "model: LOAD_FC with no open stream"
                self.compute(clear=False)
                self._fetch_chunk()

    def clear(self):
        super().clear()
        self.f_pend = self.f_overrun = False

    def fstat(self):
        """FSTAT byte when the chip is quiet (no fetch in flight)."""
        busy = self.f_open and not self.f_ready
        return (self.f_open << 7 | busy << 6 | self.f_ready << 5
                | self.f_pend << 4 | self.f_overrun << 3 | 0b010)

    def fcfg(self):
        return self.f_samp << 2 | self.f_div


def write_rtl_constants(sig):
    import pathlib
    import re
    top = pathlib.Path(__file__).resolve().parent.parent / "src" / "tt_um_msohair_tmac2.v"
    text = top.read_text()
    block = (
        "  // BEGIN GENERATED BIST CONSTANTS (test/tmac_model.py --write-rtl; do not edit)\n"
        f"  localparam        BIST_ITERS    = {BIST_ITERS};\n"
        f"  localparam [15:0] BIST_SEED     = 16'h{BIST_SEED:04X};\n"
        f"  localparam [15:0] BIST_EXPECTED = 16'h{sig:04X};\n"
        "  // END GENERATED BIST CONSTANTS\n"
    )
    new, n = re.subn(r"  // BEGIN GENERATED BIST CONSTANTS.*?// END GENERATED BIST CONSTANTS\n", block, text, flags=re.S)
    assert n == 1, "marker block not found"
    top.write_text(new, newline="\n")
    return top


def bist_expected():
    return TMAC().run_bist()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-rtl", action="store_true")
    args = ap.parse_args()
    sig = bist_expected()
    print(f"BIST signature: 0x{sig:04X}")
    if args.write_rtl:
        print("updated", write_rtl_constants(sig))
