# RAK19714 CH341 userspace SPI/GPIO bridge.
# Python translation of libpinedio-usb from
# https://github.com/pine64/libch341-spi-userspace
# Copyright (C) 2024 Marek Kraus (upstream libpinedio-usb.c / .h)
# Copyright (C) 2011 asbokid, Copyright (C) 2014 Pluto Yang,
# Copyright (C) 2015-2016 Stefan Tauner, Copyright (C) 2015 Urja Rannikko
# (flashrom ch341a_spi.c, which libpinedio-usb is based on)
#
# Upstream libpinedio-usb.c and libpinedio-usb.h carry
# SPDX-License-Identifier: GPL-2.0-or-later. This translation is
# distributed under the GNU General Public License, version 3.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Userspace CH341 SPI/GPIO bridge for the RAK19714 SX1262.

Python translation of libpinedio-usb (pine64/libch341-spi-userspace,
libpinedio-usb.c), on top of pyusb. pyusb and libusb are runtime
dependencies only; their source is not included here.

Public API
----------
    Ch341Bridge(vid=0x1A86, pid=0x5512, serial=None)
        .open()
        .close()
        .transceive(out_bytes) -> bytes        # full-duplex SPI, no auto-CS
        .digital_write(pin, value)             # UIO OUT/DIR covers D0..D5
        .digital_read(pin) -> 0|1              # any pin in 0..7 of byte0
        .get_input_word() -> int               # 32-bit packed input word
        .set_pin_mode(pin, output: bool)       # cached, applied on next write

CH341 protocol primitives
-------------------------
* Bulk OUT  endpoint 0x02
* Bulk IN   endpoint 0x82
* USB packet length is 32 bytes, first byte = command, next 31 = payload.
* SPI stream  command 0xA8: each payload byte = bit-reversed SPI byte.
* UIO stream  command 0xAB with sub-commands:
      0x80 | bits  = STM_OUT  set D0..D5 levels
      0x40 | bits  = STM_DIR  set D0..D5 direction (1 = output)
      0x20         = STM_END
* Status read command 0xA0 returns 6 bytes of input/state.

Threading
---------
A single mutex serializes USB access so SPI and GPIO can interleave from
different threads (LoRaRF spawns a monitor thread for IRQ pins).
"""

from __future__ import annotations

import threading
import time
from typing import Iterable, Optional

import usb.core  # type: ignore
import usb.util  # type: ignore


CH341_DEFAULT_VID = 0x1A86
CH341_DEFAULT_PID = 0x5512

_EP_OUT = 0x02
_EP_IN = 0x82
_PACKET_LEN = 0x20

_CMD_SPI_STREAM = 0xA8
_CMD_UIO_STREAM = 0xAB
_CMD_UIO_STM_OUT = 0x80
_CMD_UIO_STM_DIR = 0x40
_CMD_UIO_STM_END = 0x20
_CMD_GET_STATUS = 0xA0

_USB_TIMEOUT_MS = 1000


def _reverse_byte(x: int) -> int:
    """Bit-reverse one byte (CH341 SPI stream is LSB-first on the wire)."""
    x &= 0xFF
    x = ((x >> 1) & 0x55) | ((x << 1) & 0xAA)
    x = ((x >> 2) & 0x33) | ((x << 2) & 0xCC)
    x = ((x >> 4) & 0x0F) | ((x << 4) & 0xF0)
    return x & 0xFF


_REVERSE_TABLE = bytes(_reverse_byte(i) for i in range(256))


class Ch341Error(RuntimeError):
    """Raised on any CH341 USB transfer error."""


class Ch341Bridge:
    """Single connection to one CH341 device.

    All SPI and GPIO ops on the same physical CH341 must go through one
    Ch341Bridge instance. Sharing across multiple LoRaSpi/LoRaGpio shims
    is intended.
    """

    def __init__(
        self,
        vid: int = CH341_DEFAULT_VID,
        pid: int = CH341_DEFAULT_PID,
        serial: Optional[str] = None,
    ) -> None:
        self._vid = vid
        self._pid = pid
        self._serial = serial
        self._dev: Optional[usb.core.Device] = None
        self._kernel_was_active: bool = False
        self._lock = threading.Lock()

        # Cached D0..D7 direction (1 = output) and last output level.
        # libpinedio keeps these in module-level globals; we keep them per-instance.
        self._d_mode: int = 0
        self._d_state: int = 0

    # ------------------------------------------------------------------ open/close
    def open(self) -> None:
        if self._dev is not None:
            return

        match_kwargs = dict(idVendor=self._vid, idProduct=self._pid)
        if self._serial is not None:
            match_kwargs["serial_number"] = self._serial

        dev = usb.core.find(**match_kwargs)
        if dev is None:
            raise Ch341Error(
                f"CH341 device not found (VID=0x{self._vid:04X} "
                f"PID=0x{self._pid:04X} serial={self._serial!r})"
            )

        # Detach kernel driver if it grabbed interface 0 (Linux usually
        # binds the i2c-ch341/usb-serial driver). On non-Linux this is a
        # no-op.
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                self._kernel_was_active = True
        except (NotImplementedError, usb.core.USBError):
            self._kernel_was_active = False

        # libpinedio-usb does not call libusb_set_configuration at all
        # (the CH341 boots into config 1 by default). Calling it here resets
        # internal SPI/UIO engine state, so only call it if the device has
        # no active configuration yet.
        try:
            cur_cfg = dev.get_active_configuration()
        except usb.core.USBError:
            cur_cfg = None
        if cur_cfg is None:
            try:
                dev.set_configuration()
            except usb.core.USBError as exc:
                raise Ch341Error(f"Could not set configuration: {exc}") from exc

        usb.util.claim_interface(dev, 0)
        self._dev = dev

        # D3 (SCK) and D5 (MOSI) must be outputs in the CH341 UIO direction
        # register before the SPI_STREAM (0xA8) engine will drive them.
        # Leaving them as inputs keeps SCK and MOSI high-Z, so every SPI
        # read comes back 0xFF. D0 (CS), D1 (RXen) and D2 (NRESET) are
        # marked at the same time so the first digital_write() pushes a
        # complete D0..D5 direction mask. The UIO DIR/OUT fields are 6 bits.
        self._d_mode |= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 5)
        self.digital_write(0, True)

    def close(self) -> None:
        if self._dev is None:
            return
        try:
            usb.util.release_interface(self._dev, 0)
        except usb.core.USBError:
            pass
        if self._kernel_was_active:
            try:
                self._dev.attach_kernel_driver(0)
            except (NotImplementedError, usb.core.USBError):
                pass
        try:
            usb.util.dispose_resources(self._dev)
        except usb.core.USBError:
            pass
        self._dev = None
        self._kernel_was_active = False

    def __enter__(self) -> "Ch341Bridge":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    # ------------------------------------------------------------------ low level
    def _require(self) -> usb.core.Device:
        if self._dev is None:
            raise Ch341Error("Ch341Bridge is not open")
        return self._dev

    def _bulk_write(self, data: bytes) -> None:
        dev = self._require()
        sent = dev.write(_EP_OUT, data, _USB_TIMEOUT_MS)
        if sent != len(data):
            raise Ch341Error(f"Short USB OUT write: {sent}/{len(data)}")

    def _bulk_read(self, length: int) -> bytes:
        dev = self._require()
        buf = dev.read(_EP_IN, length, _USB_TIMEOUT_MS)
        return bytes(buf)

    # ------------------------------------------------------------------ GPIO
    def set_pin_mode(self, pin: int, output: bool) -> None:
        """Mark D<pin> as output or input.

        libpinedio caches this and sends it bundled with the next
        digital_write(); we follow the same approach so a stand-alone
        set_pin_mode() doesn't burn a USB round trip.
        """
        if not 0 <= pin < 8:
            raise ValueError(f"CH341 only exposes D0..D7 as outputs, got pin={pin}")
        if output:
            self._d_mode |= 1 << pin
        else:
            self._d_mode &= ~(1 << pin)

    def digital_write(self, pin: int, value) -> None:
        """Set D<pin> output level. Implicitly marks the pin as output."""
        if not 0 <= pin < 8:
            raise ValueError(f"CH341 only exposes D0..D7 as outputs, got pin={pin}")
        active = bool(value)
        with self._lock:
            self._d_mode |= 1 << pin
            if active:
                self._d_state |= 1 << pin
            else:
                self._d_state &= ~(1 << pin)

            packet = bytes([
                _CMD_UIO_STREAM,
                _CMD_UIO_STM_OUT | (self._d_state & 0x3F),
                _CMD_UIO_STM_DIR | (self._d_mode & 0x3F),
                _CMD_UIO_STM_END,
            ])
            self._bulk_write(packet)

    def digital_read(self, pin: int) -> int:
        """Return 1 or 0 for D<pin> (0..7), as encoded in status byte 0."""
        if not 0 <= pin < 8:
            raise ValueError(f"digital_read pin must be 0..7, got {pin}")
        with self._lock:
            self._bulk_write(bytes([_CMD_GET_STATUS]))
            output = self._bulk_read(6)
        if len(output) < 1:
            raise Ch341Error("CH341 status read returned empty buffer")
        return 1 if (output[0] & (1 << pin)) else 0

    def get_input_word(self) -> int:
        """Return the packed 32-bit input bitmap (mirrors pinedio_get_input)."""
        with self._lock:
            self._bulk_write(bytes([_CMD_GET_STATUS]))
            output = self._bulk_read(6)
        if len(output) < 3:
            raise Ch341Error("CH341 status read returned short buffer")
        return ((output[2] & 0x80) << 16) | ((output[1] & 0xEF) << 8) | output[0]

    # ------------------------------------------------------------------ SPI
    def transceive(self, data: Iterable[int]) -> bytes:
        """Full-duplex SPI transfer. Returns received bytes (same length).

        AUTO_CS is OFF: caller must drive the CS GPIO around this call.
        """
        out = bytes(data)
        n = len(out)
        if n == 0:
            return b""

        # Each USB packet carries (32 - 1 = 31) SPI bytes prefixed by 0xA8.
        chunk = _PACKET_LEN - 1
        packets = (n + chunk - 1) // chunk

        wbuf = bytearray()
        for p in range(packets):
            start = p * chunk
            piece = out[start:start + chunk]
            wbuf.append(_CMD_SPI_STREAM)
            wbuf.extend(_REVERSE_TABLE[b] for b in piece)

        with self._lock:
            self._bulk_write(bytes(wbuf))
            rx = bytearray()
            while len(rx) < n:
                # CH341 echoes (n - already_read) bytes of MISO, but bounded
                # by the per-IN-transfer cap of 31 bytes per packet.
                want = min(chunk, n - len(rx))
                chunk_in = self._bulk_read(want)
                if not chunk_in:
                    raise Ch341Error("CH341 SPI read returned empty buffer")
                rx.extend(chunk_in)

        return bytes(_REVERSE_TABLE[b] for b in rx)
