# RAK19714 LoRaRF SPI/GPIO shims for the CH341 bridge.
# Copyright (C) 2026 RAK19714 P2P example authors
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

"""LoRaRF-Python compatibility shims that route SPI/GPIO through CH341.

LoRaRF's `SX126x` only consumes a small surface of the `LoRaSpi` /
`LoRaGpio` classes:

    LoRaSpi:
        .speed (read/write attribute)
        .transfer(buf) -> Iterable[int]   # full-duplex
    LoRaGpio:
        LOW / HIGH  class constants
        .output(value)
        .input() -> value
        .monitor(callback, timeout)       # only used when `irq` is not None

We expose drop-in classes that hit a `Ch341Bridge` instead of /dev/spidev*
and /dev/gpiochip*. They subclass the upstream `LoRaSpi` / `LoRaGpio`
purely to inherit the `LOW = Value.INACTIVE` / `HIGH = Value.ACTIVE`
constants that `SX126x` compares against.
"""

from __future__ import annotations

import os
import sys
import time
import types
from typing import Iterable, Optional

# Vendored chandrawi/LoRaRF-Python lives in third_party/ and is imported
# as the LoRaRF package. Do not pip-install the older PyPI LoRaRF.
_HERE = os.path.dirname(os.path.abspath(__file__))
_THIRD = os.path.join(_HERE, "third_party")
if _THIRD not in sys.path:
    sys.path.insert(0, _THIRD)


def _install_lorarf_import_stubs() -> None:
    """Let LoRaRF.base import on non-Linux hosts.

    The CH341 scripts never use LoRaRF's default /dev/spidev or /dev/gpiochip
    backends, but upstream LoRaRF imports ``spidev`` and ``gpiod`` at module
    import time in order to define its base classes. macOS does not provide
    those Linux-only modules, so install tiny import-time stubs when they are
    missing. Real Linux installations keep using the actual modules.
    """
    try:
        import spidev  # noqa: F401
    except ModuleNotFoundError:
        spidev_stub = types.ModuleType("spidev")

        class _MissingSpiDev:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("spidev is unavailable; use Ch341Spi instead")

        spidev_stub.SpiDev = _MissingSpiDev
        sys.modules.setdefault("spidev", spidev_stub)

    try:
        import gpiod  # noqa: F401
        from gpiod.line import Direction, Edge, Value  # noqa: F401
    except ModuleNotFoundError:
        gpiod_stub = types.ModuleType("gpiod")
        line_stub = types.ModuleType("gpiod.line")

        class _Value:
            INACTIVE = 0
            ACTIVE = 1

        class _Direction:
            OUTPUT = "output"
            INPUT = "input"

        class _Edge:
            RISING = "rising"

        class _LineSettings:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        def _request_lines(*args, **kwargs):
            raise RuntimeError("gpiod is unavailable; use Ch341Gpio instead")

        line_stub.Value = _Value
        line_stub.Direction = _Direction
        line_stub.Edge = _Edge
        gpiod_stub.line = line_stub
        gpiod_stub.LineSettings = _LineSettings
        gpiod_stub.request_lines = _request_lines
        sys.modules.setdefault("gpiod", gpiod_stub)
        sys.modules.setdefault("gpiod.line", line_stub)


_install_lorarf_import_stubs()

from LoRaRF.base import LoRaGpio, LoRaSpi  # type: ignore  # noqa: E402

from ch341_bridge import Ch341Bridge  # noqa: E402


class Ch341Spi(LoRaSpi):
    """LoRaSpi that performs full-duplex transfers over a CH341 bridge.

    `bus`/`cs` are kept for API compatibility but ignored: the CH341 has
    no kernel SPI device. CS is driven by Ch341Gpio at the pin level.
    """

    def __init__(self, bridge: Ch341Bridge, speed: int = 8_000_000) -> None:
        self.bus = 0
        self.cs = 0
        self.speed = speed
        self.bridge = bridge

    def transfer(self, buf: Iterable[int]) -> list:
        rx = self.bridge.transceive(buf)
        return list(rx)


class Ch341Gpio(LoRaGpio):
    """LoRaGpio that routes a single CH341 D-pin through a Ch341Bridge.

    `pin` is the CH341 D-line index (0..7). On the RAK19714:

        D0 = NSS / CS
        D1 = RXen
        D2 = NRESET
        D3 = SCK   (SPI engine; direction is set in Ch341Bridge.open)
        D4 = BUSY
        D5 = MOSI  (SPI engine; direction is set in Ch341Bridge.open)
        D6 = DIO1 / IRQ
        D7 = MISO  (SPI engine input)
    """

    def __init__(self, bridge: Ch341Bridge, pin: int, *, mode: str = "output") -> None:
        if mode not in ("input", "output"):
            raise ValueError(f"mode must be 'input' or 'output', got {mode!r}")
        self.chip = "ch341"
        self.offset = pin
        self.seqno = 0
        self.bridge = bridge
        self.pin = pin
        self.mode = mode
        bridge.set_pin_mode(pin, output=(mode == "output"))

    def output(self, value) -> None:
        active = value == LoRaGpio.HIGH
        self.bridge.digital_write(self.pin, active)

    def input(self):
        v = self.bridge.digital_read(self.pin)
        return LoRaGpio.HIGH if v else LoRaGpio.LOW

    def monitor(self, callback, timeout: float) -> None:
        """Polling rising-edge detection, same ~30 Hz cadence as libpinedio.

        Used only when an IRQ pin is passed into SX126x. This example polls
        GetIrqStatus over SPI instead, so the P2P path does not call this.
        """
        period = 1.0 / 30.0
        start = time.time()
        prev: Optional[int] = None
        while timeout == 0 or (time.time() - start) < timeout:
            curr = self.bridge.digital_read(self.pin)
            if prev is not None and prev == 0 and curr != 0:
                self.seqno += 1
                callback()
                return
            prev = curr
            time.sleep(period)


__all__ = ["Ch341Bridge", "Ch341Gpio", "Ch341Spi"]
