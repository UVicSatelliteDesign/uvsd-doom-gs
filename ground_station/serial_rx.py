"""Serial receiver for the nRF5340 frame-UART packet stream.

Packet format (see nrfdoom/source/n_frame_uart.c in uvsd-nrf-doom):
    SYNC(2B: 0xAA,0x55) | TYPE(1B) | LENGTH(4B LE) | PAYLOAD(NB) | CRC16(2B LE)

LENGTH is 4 bytes, not 2: RLE is a worst-case 2x expansion (not a guaranteed
compression), so a 320x200 frame can encode up to 128000 bytes -- too big for
a 16-bit length. An earlier version of this protocol used a 2-byte LENGTH and
silently wrapped on busy frames; don't reintroduce that.

The only type currently sent is DOOM_FRAME (0x02), whose payload is:
    PLAYPAL index(1B) | RLE-encoded 320x200 8-bit palette indices

CRC16 (CCITT-FALSE) covers TYPE, LENGTH and PAYLOAD.
"""

import struct

import serial
import serial.tools.list_ports
from PyQt6.QtCore import QThread, pyqtSignal

SYNC0 = 0xAA
SYNC1 = 0x55
TYPE_DOOM_FRAME = 0x02

SCREEN_WIDTH = 320
SCREEN_HEIGHT = 200
NUM_PIXELS = SCREEN_WIDTH * SCREEN_HEIGHT

# RLE's own worst case is 2 bytes per pixel (see n_frame_uart.c), plus the
# 1-byte PLAYPAL index -- a LENGTH bigger than this can only be a corrupted
# field (a real link fault we've observed on real hardware, not just a
# theoretical one). Treat it like a CRC mismatch so read_exact() never blocks
# for a huge read that will never complete.
MAX_PAYLOAD_LEN = 1 + 2 * NUM_PIXELS

# 460800 and 1M both corrupt/drop bytes on the DK's shared J-Link VCOM1
# bridge (confirmed on real hardware: every frame failed CRC at those rates
# even though the firmware's encoded length was provably correct). 115200
# is the only rate that has decoded cleanly so far -- see uvsd-nrf-doom's
# CLAUDE.md bring-up checklist before raising this.
DEFAULT_BAUD = 115_200


def list_serial_ports() -> list[str]:
    """Names of serial ports currently present on the system.

    Excludes ports with hwid == "n/a": on Linux, comports() always reports
    the 32 legacy /dev/ttyS0-31 platform ports whether or not any hardware
    is attached to them, and they'd otherwise drown out the handful of real
    ports (e.g. the DK's two J-Link CDC ports) in the dropdown.
    """
    return [
        port.device
        for port in serial.tools.list_ports.comports()
        if port.hwid != "n/a"
    ]


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def decode_rle(payload: bytes, expected_len: int) -> bytes:
    out = bytearray()
    i = 0
    while i + 1 < len(payload) and len(out) < expected_len:
        count = payload[i]
        value = payload[i + 1]
        out.extend([value] * count)
        i += 2
    return bytes(out[:expected_len])


def read_packet(read_exact) -> tuple[int, bytes] | None:
    """Reads one packet using read_exact(n) -> bytes.

    Returns (type, payload), or None on CRC mismatch (caller resyncs by
    simply calling again — the sync scan discards the bad bytes).
    """
    prev = 0
    while True:
        b = read_exact(1)[0]
        if prev == SYNC0 and b == SYNC1:
            break
        prev = b

    type_len = read_exact(5)
    ptype = type_len[0]
    length = struct.unpack_from("<I", type_len, 1)[0]
    if length > MAX_PAYLOAD_LEN:
        return None
    payload = read_exact(length)
    received_crc = struct.unpack("<H", read_exact(2))[0]

    if received_crc != crc16(type_len + payload):
        return None
    return ptype, payload


class SerialFrameReader(QThread):
    """Reads DOOM_FRAME packets off a serial port in the background.

    Emits frame_received(playpal_index, pixels) with pixels being
    NUM_PIXELS bytes of 8-bit palette indices, ready for an Indexed8 QImage.
    """

    frame_received = pyqtSignal(int, bytes)
    error_occurred = pyqtSignal(str)

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, parent=None):
        super().__init__(parent)
        self._port = port
        self._baud = baud
        self._running = False

    def stop(self):
        self._running = False
        self.wait(2_000)

    def run(self):
        self._running = True
        try:
            ser = serial.Serial(self._port, self._baud, timeout=0.5)
        except serial.SerialException as e:
            self.error_occurred.emit(str(e))
            return

        class _Stopped(Exception):
            pass

        def read_exact(n: int) -> bytes:
            # serial reads return short on timeout; loop so packet parsing
            # only ever sees complete reads, but stay responsive to stop()
            buf = bytearray()
            while len(buf) < n:
                if not self._running:
                    raise _Stopped()
                chunk = ser.read(n - len(buf))
                buf += chunk
            return bytes(buf)

        try:
            while self._running:
                packet = read_packet(read_exact)
                if packet is None:
                    continue  # CRC mismatch: drop frame, resync on next call
                ptype, payload = packet
                if ptype != TYPE_DOOM_FRAME or not payload:
                    continue

                pixels = decode_rle(payload[1:], NUM_PIXELS)
                if len(pixels) != NUM_PIXELS:
                    continue  # short frame

                self.frame_received.emit(payload[0], pixels)
        except _Stopped:
            pass
        except serial.SerialException as e:
            self.error_occurred.emit(str(e))
        finally:
            ser.close()
