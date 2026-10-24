"""LZX decompression for Xbox 360 XMemCompress streams (zip method 21).

A Python port of the LZX decoder in libmspack / MonoGame's LzxDecoder.
XMemCompress frames the LZX bitstream in chunks:

    FF, u16be uncompressed_size, u16be compressed_size   (short chunk)
    u16be compressed_size                                (32768-byte chunk)

Each chunk is a fresh bit reader over the same decoder state (window and
repeated offsets persist).
"""

from __future__ import annotations

MIN_MATCH = 2
NUM_CHARS = 256
VERBATIM, ALIGNED, UNCOMPRESSED = 1, 2, 3
PRETREE_SYMBOLS = 20
ALIGNED_SYMBOLS = 8
LENGTH_SYMBOLS = 249 + 1
POSITION_SLOTS = {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}

EXTRA_BITS = []
_j = 0
for _i in range(0, 52, 2):
    EXTRA_BITS += [_j, _j]
    if _i != 0 and _j < 17:
        _j += 1
POSITION_BASE = []
_j = 0
for _i in range(51):
    POSITION_BASE.append(_j)
    _j += 1 << EXTRA_BITS[_i]


class LzxError(ValueError):
    pass


class _Bits:
    """16-bit little-endian words, read most significant bit first."""

    __slots__ = ("buf", "data", "left", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos, self.buf, self.left = data, pos, 0, 0

    def ensure(self, n: int) -> None:
        while self.left < n:
            lo = self.data[self.pos] if self.pos < len(self.data) else 0
            hi = self.data[self.pos + 1] if self.pos + 1 < len(self.data) else 0
            self.pos += 2
            self.buf = (self.buf << 16) | (hi << 8) | lo
            self.left += 16

    def peek(self, n: int) -> int:
        self.ensure(n)
        return (self.buf >> (self.left - n)) & ((1 << n) - 1)

    def remove(self, n: int) -> None:
        self.left -= n
        self.buf &= (1 << self.left) - 1

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        value = self.peek(n)
        self.remove(n)
        return value


def _build_table(lengths: list[int]) -> tuple[list[int], int]:
    """Canonical Huffman decode table indexed by the next max_len bits.
    Entries are symbol | (length << 16); 0 means an invalid code."""
    max_len = max(lengths) if lengths else 0
    if max_len == 0:
        return [], 0
    table = [0] * (1 << max_len)
    code = 0
    for length in range(1, max_len + 1):
        for symbol, symbol_len in enumerate(lengths):
            if symbol_len != length:
                continue
            start = code << (max_len - length)
            end = (code + 1) << (max_len - length)
            if end > len(table):
                raise LzxError("over-subscribed Huffman table")
            entry = symbol | (length << 16)
            for k in range(start, end):
                table[k] = entry
            code += 1
        code <<= 1
    return table, max_len


def _read_symbol(bits: _Bits, table: list[int], max_len: int) -> int:
    entry = table[bits.peek(max_len)]
    if entry == 0:
        raise LzxError("invalid Huffman code")
    bits.remove(entry >> 16)
    return entry & 0xFFFF


class LzxDecoder:
    def __init__(self, window_bits: int = 17):
        if window_bits not in POSITION_SLOTS:
            raise LzxError(f"unsupported window size 2^{window_bits}")
        self.window_size = 1 << window_bits
        self.window = bytearray(self.window_size)
        self.window_pos = 0
        self.frame_pos = 0  # window position where the next frame's output starts
        self.r0 = self.r1 = self.r2 = 1
        self.main_elements = NUM_CHARS + (POSITION_SLOTS[window_bits] << 3)
        self.header_read = False
        self.block_type = 0
        self.block_length = 0
        self.block_remaining = 0
        self.intel_filesize = 0
        self.intel_curpos = 0
        self.intel_started = False
        self.main_lengths = [0] * self.main_elements
        self.length_lengths = [0] * LENGTH_SYMBOLS
        self.main_table = self.length_table = self.aligned_table = None

    def _read_lengths(self, bits: _Bits, lengths: list[int], first: int, last: int) -> None:
        pre = [bits.read(4) for _ in range(PRETREE_SYMBOLS)]
        table, max_len = _build_table(pre)
        x = first
        while x < last:
            z = _read_symbol(bits, table, max_len)
            if z == 17:
                for _ in range(bits.read(4) + 4):
                    lengths[x] = 0
                    x += 1
            elif z == 18:
                for _ in range(bits.read(5) + 20):
                    lengths[x] = 0
                    x += 1
            elif z == 19:
                run = bits.read(1) + 4
                z = _read_symbol(bits, table, max_len)
                value = (lengths[x] - z) % 17
                for _ in range(run):
                    lengths[x] = value
                    x += 1
            else:
                lengths[x] = (lengths[x] - z) % 17
                x += 1

    def _read_block_header(self, bits: _Bits) -> None:
        if self.block_type == UNCOMPRESSED and self.block_length & 1:
            bits.pos += 1  # uncompressed blocks are padded to even length
        self.block_type = bits.read(3)
        self.block_length = self.block_remaining = (bits.read(16) << 8) | bits.read(8)
        if self.block_type == ALIGNED:
            self.aligned_table = _build_table([bits.read(3) for _ in range(ALIGNED_SYMBOLS)])
        if self.block_type in (VERBATIM, ALIGNED):
            self._read_lengths(bits, self.main_lengths, 0, 256)
            self._read_lengths(bits, self.main_lengths, 256, self.main_elements)
            self.main_table = _build_table(self.main_lengths)
            if self.main_lengths[0xE8]:
                self.intel_started = True
            self._read_lengths(bits, self.length_lengths, 0, 249)
            self.length_table = _build_table(self.length_lengths)
        elif self.block_type == UNCOMPRESSED:
            self.intel_started = True
            # Realign to the byte stream: drop the buffered bits.
            bits.ensure(16)
            if bits.left > 16:
                bits.pos -= 2
            bits.buf, bits.left = 0, 0
            data = bits.data
            self.r0 = int.from_bytes(data[bits.pos:bits.pos + 4], "little")
            self.r1 = int.from_bytes(data[bits.pos + 4:bits.pos + 8], "little")
            self.r2 = int.from_bytes(data[bits.pos + 8:bits.pos + 12], "little")
            bits.pos += 12
        else:
            raise LzxError(f"bad block type {self.block_type}")

    def decompress_chunk(self, data: bytes, out_len: int) -> bytes:
        bits = _Bits(data)
        if not self.header_read:
            if bits.read(1):
                self.intel_filesize = (bits.read(16) << 16) | bits.read(16)
            self.header_read = True

        window, mask = self.window, self.window_size - 1
        # A match that ran past the previous frame's end already decoded the
        # first bytes of this one.
        start_pos = self.frame_pos
        togo = out_len - ((self.window_pos - start_pos) & mask)
        while togo > 0:
            if self.block_remaining == 0:
                self._read_block_header(bits)
            this_run = min(self.block_remaining, togo)
            togo -= this_run
            self.block_remaining -= this_run
            wp = self.window_pos

            if self.block_type == UNCOMPRESSED:
                chunk = data[bits.pos:bits.pos + this_run]
                if len(chunk) < this_run:
                    raise LzxError(f"uncompressed block: {this_run} bytes wanted, {len(chunk)} left in the chunk")
                for b in chunk:
                    window[wp] = b
                    wp = (wp + 1) & mask
                bits.pos += this_run
                self.window_pos = wp
                continue

            main_table, main_len = self.main_table
            length_table, length_len = self.length_table
            aligned = self.block_type == ALIGNED
            if aligned:
                aligned_table, aligned_len = self.aligned_table
            r0, r1, r2 = self.r0, self.r1, self.r2
            while this_run > 0:
                element = _read_symbol(bits, main_table, main_len)
                if element < NUM_CHARS:
                    window[wp] = element
                    wp = (wp + 1) & mask
                    this_run -= 1
                    continue
                element -= NUM_CHARS
                match_length = element & 7
                if match_length == 7:
                    match_length += _read_symbol(bits, length_table, length_len)
                match_length += MIN_MATCH
                slot = element >> 3
                if slot > 2:
                    extra = EXTRA_BITS[slot]
                    if not aligned:
                        offset = POSITION_BASE[slot] - 2 + bits.read(extra) if slot != 3 else 1
                    else:
                        offset = POSITION_BASE[slot] - 2
                        if extra > 3:
                            offset += bits.read(extra - 3) << 3
                            offset += _read_symbol(bits, aligned_table, aligned_len)
                        elif extra == 3:
                            offset += _read_symbol(bits, aligned_table, aligned_len)
                        elif extra > 0:
                            offset += bits.read(extra)
                        else:
                            offset = 1
                    r2, r1, r0 = r1, r0, offset
                elif slot == 0:
                    offset = r0
                elif slot == 1:
                    offset = r1
                    r1, r0 = r0, offset
                else:
                    offset = r2
                    r2, r0 = r0, offset
                src = (wp - offset) & mask
                for _ in range(match_length):
                    window[wp] = window[src]
                    wp = (wp + 1) & mask
                    src = (src + 1) & mask
                this_run -= match_length
            self.r0, self.r1, self.r2 = r0, r1, r2
            self.window_pos = wp
            if this_run < 0:
                # A match ran past the end of the frame: its extra bytes belong to
                # the block and to the next frame (frame_pos, below).
                self.block_remaining -= -this_run
                togo += this_run

        self.frame_pos = (start_pos + out_len) & mask
        if start_pos + out_len <= self.window_size:
            out = bytearray(window[start_pos:start_pos + out_len])
        else:
            out = bytearray(window[start_pos:]) + window[:out_len - (self.window_size - start_pos)]
        if self.intel_filesize and self.intel_started and out_len > 10:
            self._undo_e8(out)
        self.intel_curpos += out_len
        return bytes(out)

    def _undo_e8(self, out: bytearray) -> None:
        curpos, size = self.intel_curpos, self.intel_filesize
        i, end = 0, len(out) - 10
        while i < end:
            if out[i] != 0xE8:
                i += 1
                continue
            absolute = int.from_bytes(out[i + 1:i + 5], "little", signed=True)
            if -curpos - i <= absolute < size:
                relative = absolute - (curpos + i) if absolute >= 0 else absolute + size
                out[i + 1:i + 5] = (relative & 0xFFFFFFFF).to_bytes(4, "little")
            i += 5


def xmem_decompress(data: bytes, size: int, window_bits: int = 17) -> bytes:
    """Decompress an XMemCompress chunked LZX stream to `size` bytes."""
    decoder = LzxDecoder(window_bits)
    out = bytearray()
    pos = 0
    while len(out) < size and pos < len(data):
        if data[pos] == 0xFF:
            frame = (data[pos + 1] << 8) | data[pos + 2]
            chunk = (data[pos + 3] << 8) | data[pos + 4]
            pos += 5
        else:
            frame = 32768
            chunk = (data[pos] << 8) | data[pos + 1]
            pos += 2
        if chunk == 0 or frame == 0:
            break
        frame = min(frame, size - len(out))
        out += decoder.decompress_chunk(data[pos:pos + chunk], frame)
        pos += chunk
    if len(out) != size:
        raise LzxError(f"decompressed {len(out)} bytes, expected {size}")
    return bytes(out)
