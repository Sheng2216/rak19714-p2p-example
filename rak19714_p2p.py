#!/usr/bin/env python3
# RAK19714 Linux P2P example: SX1262 LoRa TX/RX over the CH341 USB bridge.
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

"""RAK19714 (CH341 USB to SX1262) P2P LoRa transmit and receive.

The CLI follows the RAK RUI3 P2P AT-command convention:

    rak19714_p2p.py tx <freq>:<sf>:<bw>:<cr>:<preamble>:<power> <hex_payload>
    rak19714_p2p.py rx <freq>:<sf>:<bw>:<cr>:<preamble>:<power> <timeout_ms>

Examples
--------

    # Send a 7-byte hex payload once and exit
    rak19714_p2p.py tx 868000000:7:125:0:8:14 12121212121212

    # Listen for 60 s and print every received packet
    rak19714_p2p.py rx 868000000:7:125:0:8:14 60000

    # Listen until a single packet arrives, then exit (RAK timeout 65535)
    rak19714_p2p.py rx 868000000:7:125:0:8:14 65535

    # Listen forever (RAK timeout 65534), Ctrl+C to stop
    rak19714_p2p.py rx 868000000:7:125:0:8:14 65534

P2P spec fields
---------------

* ``freq``     -- MHz (e.g. 868.0, 915.5) or Hz (>= 1_000_000)
* ``sf``       -- 7..12 typical; SX1262 also accepts 5 and 6
* ``bw``       -- 0/1/2 (RAK enum: 125/250/500 kHz), 125/250/500 (kHz)
                  or 125000/250000/500000 (Hz)
* ``cr``       -- 0..3 (RAK enum: 4/5..4/8) or 5..8 (LoRaRF raw)
* ``preamble`` -- 1..65535 LoRa preamble symbols
* ``power``    -- -9..22 dBm (SX1262 high-power PA)

RX output format
----------------

For every successfully received packet the script prints one line::

    RSSI:SNR:HEX

for example ``-30:13:12121212121212``. RSSI is dBm from
``packetRssi()`` (already negative), SNR is dB rounded to int, and the
payload is uppercase hex with no separators. The default sync word is
``0x1424`` (Semtech private network). Do not pass LoRaRF's
``LORA_SYNC_WORD_PRIVATE`` (``0x0741``); that constant is not the
datasheet value. LoRaWAN public networks use ``0x3444``.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.abspath(__file__))
_THIRD = os.path.join(_ROOT, "third_party")
if _THIRD not in sys.path:
    sys.path.insert(0, _THIRD)

from ch341_bridge import Ch341Bridge  # noqa: E402
from lorarf_ch341 import Ch341Gpio, Ch341Spi  # noqa: E402

from LoRaRF import SX126x  # noqa: E402


# RAK19714 1262 CH341 D-pin map.
PIN_CS = 0
PIN_RXEN = 1
PIN_RESET = 2
PIN_BUSY = 4
PIN_IRQ = 6

DEFAULT_VID = 0x1A86
DEFAULT_PID = 0x5512

# RAK P2P timeout sentinels
RAK_RX_SINGLE = 65535       # listen until first packet then exit
RAK_RX_CONTINUOUS = 65534   # listen forever
RAK_RX_TIMEOUT_MAX = 65532  # max regular timeout in ms

# Polling cadence for RX IRQ + Ctrl+C check (Hz)
RX_POLL_INTERVAL_S = 0.02

quit_requested = False


def signal_handler(signum, frame):  # noqa: ARG001
    global quit_requested
    quit_requested = True


# --------------------------------------------------------------------- parsing


@dataclass
class P2PConfig:
    frequency_hz: int
    sf: int
    bw_hz: int
    cr: int           # LoRaRF raw value (5..8 for 4/5..4/8)
    preamble: int
    power_dbm: int


def _parse_freq(s: str) -> int:
    v = float(s)
    if v <= 0:
        raise ValueError(f"frequency must be positive, got {s!r}")
    if v < 1_000_000:           # treat as MHz
        return int(round(v * 1_000_000))
    return int(round(v))         # treat as Hz


def _parse_bw(s: str) -> int:
    v = int(s, 0)
    if v in (0, 1, 2):
        return (125_000, 250_000, 500_000)[v]
    if v in (125, 250, 500):
        return v * 1_000
    if v in (125_000, 250_000, 500_000):
        return v
    raise ValueError(
        f"unsupported BW {s!r}; allowed: 0/1/2, 125/250/500, 125000/250000/500000"
    )


def _parse_cr(s: str) -> int:
    v = int(s, 0)
    if 0 <= v <= 3:
        return v + 5            # RAK enum 0..3 -> LoRaRF 5..8 (4/5..4/8)
    if 5 <= v <= 8:
        return v
    raise ValueError(f"unsupported CR {s!r}; allowed: 0..3 (RAK) or 5..8 (LoRaRF)")


def _parse_preamble(s: str) -> int:
    v = int(s, 0)
    if not 1 <= v <= 65535:
        raise ValueError(f"preamble must be 1..65535, got {s!r}")
    return v


def _parse_power(s: str) -> int:
    v = int(s, 0)
    if not -9 <= v <= 22:
        raise ValueError(f"power must be -9..22 dBm, got {s!r}")
    return v


def parse_p2p_spec(spec: str) -> P2PConfig:
    parts = spec.split(":")
    if len(parts) != 6:
        raise ValueError(
            f"P2P spec must have 6 colon-separated fields "
            f"(freq:sf:bw:cr:preamble:power), got {spec!r}"
        )
    freq_s, sf_s, bw_s, cr_s, pre_s, pwr_s = parts
    sf = int(sf_s, 0)
    if not 5 <= sf <= 12:
        raise ValueError(f"SF must be 5..12, got {sf_s!r}")
    return P2PConfig(
        frequency_hz=_parse_freq(freq_s),
        sf=sf,
        bw_hz=_parse_bw(bw_s),
        cr=_parse_cr(cr_s),
        preamble=_parse_preamble(pre_s),
        power_dbm=_parse_power(pwr_s),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RAK19714 (CH341) SX1262 P2P LoRa TX / RX",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("mode", choices=("tx", "rx"),
                   help="tx = send one packet then exit; rx = listen and print packets")
    p.add_argument(
        "p2p",
        help="P2P parameters as freq:sf:bw:cr:preamble:power "
        "(e.g. 868000000:7:125:0:8:14)",
    )
    p.add_argument(
        "payload_or_timeout",
        help="tx mode: hex payload (e.g. 12121212121212); "
        "rx mode: timeout ms (1..65532), 65535 = single, 65534 = continuous",
    )
    p.add_argument("--sync-word", default="0x1424",
                   help="LoRa sync word written to 0x0740 (default 0x1424, "
                   "Semtech private network; not LoRaRF's 0x0741)")
    p.add_argument("--no-crc", action="store_true",
                   help="Disable CRC in LoRa packet (default: CRC on)")
    p.add_argument("--invert-iq", action="store_true",
                   help="Invert IQ polarity (default: standard)")
    p.add_argument("--implicit-header", action="store_true",
                   help="Use implicit LoRa header (default: explicit)")
    p.add_argument("--vid", type=lambda s: int(s, 0), default=DEFAULT_VID)
    p.add_argument("--pid", type=lambda s: int(s, 0), default=DEFAULT_PID)
    p.add_argument("--serial", default=None, help="Optional CH341 USB serial filter")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress setup logs, including TX_DONE. "
                   "Received RSSI:SNR:HEX lines are still printed")
    return p.parse_args()


# --------------------------------------------------------------------- radio


def build_radio(bridge: Ch341Bridge) -> SX126x:
    spi = Ch341Spi(bridge)
    cs = Ch341Gpio(bridge, PIN_CS, mode="output")
    reset = Ch341Gpio(bridge, PIN_RESET, mode="output")
    busy = Ch341Gpio(bridge, PIN_BUSY, mode="input")
    Ch341Gpio(bridge, PIN_RXEN, mode="output").output(0)
    return SX126x(spi, cs, reset, busy)


def _needs_ldro(cfg: P2PConfig) -> bool:
    """Return True when the LoRa symbol time requires low data-rate optimization."""
    symbol_time_s = (1 << cfg.sf) / cfg.bw_hz
    return symbol_time_s >= 0.016384


def setup_radio(lora: SX126x, cfg: P2PConfig, sync_word: int,
                payload_len: int, header_explicit: bool, crc_on: bool,
                invert_iq: bool, log) -> None:
    ldro_on = _needs_ldro(cfg)

    log("LoRaRF begin: CS=D0, RESET=D2, BUSY=D4 (CH341 USB)")
    if not lora.begin():
        raise RuntimeError("LoRaRF begin() failed")

    log("SetStandby(RC): 0x80 0x00")
    lora.setStandby(lora.STANDBY_RC)

    log("SetRegulatorMode(DCDC): 0x96 0x01")
    lora.setRegulatorMode(lora.REGULATOR_DC_DC)

    log("SetDio2AsRfSwitchCtrl(enable): 0x9D 0x01")
    lora.setDio2AsRfSwitchCtrl(lora.DIO2_AS_RF_SWITCH)

    log("SetDio3AsTcxoCtrl(1.8V, 0x0560): 0x97 0x02 0x00 0x05 0x60")
    lora.setDio3TcxoCtrl(lora.DIO3_OUTPUT_1_8, lora.TCXO_DELAY_10)
    time.sleep(0.015)

    log("SetPacketType(LoRa): 0x8A 0x01")
    lora.setPacketType(lora.LORA_MODEM)

    log(f"SetFrequency({cfg.frequency_hz} Hz): 0x86 ...")
    lora.setFrequency(cfg.frequency_hz)

    log("SetPaConfig(SX1262 HP): 0x95 0x04 0x07 0x00 0x01")
    lora.setPaConfig(0x04, 0x07, 0x00, 0x01)

    log(f"SetTxParams({cfg.power_dbm} dBm, ramp=40us): "
        f"0x8E {cfg.power_dbm & 0xFF:#04x} 0x02")
    lora.setTxParams(cfg.power_dbm, lora.PA_RAMP_40U)

    log(f"SetLoRaModulation(SF{cfg.sf}, BW={cfg.bw_hz}, CR=4/{cfg.cr}, "
        f"LDRO {'on' if ldro_on else 'off'})")
    lora.setLoRaModulation(cfg.sf, cfg.bw_hz, cfg.cr, ldro=ldro_on)

    header = lora.HEADER_EXPLICIT if header_explicit else lora.HEADER_IMPLICIT
    log(f"SetLoRaPacket("
        f"{'explicit' if header_explicit else 'implicit'}, "
        f"preamble={cfg.preamble}, payload={payload_len}, "
        f"CRC {'on' if crc_on else 'off'}, "
        f"IQ {'inverted' if invert_iq else 'normal'})")
    lora.setLoRaPacket(header, cfg.preamble, payload_len, crc_on, invert_iq)

    # Datasheet private-network sync word is 0x1424 (bytes 0x14 0x24 at
    # register 0x0740). LoRaRF's LORA_SYNC_WORD_PRIVATE = 0x0741 writes
    # 0x07 0x41 instead, so this example does not use that constant.
    log(f"SetSyncWord(0x{sync_word:04X})")
    lora.setSyncWord(sync_word)


# --------------------------------------------------------------------- TX


def do_tx(lora: SX126x, payload: bytes, log) -> bool:
    """Transmit one packet and return True on TX_DONE."""
    if not payload:
        raise ValueError("TX payload must be non-empty")
    log(f"BeginPacket / write({len(payload)} bytes) / EndPacket(TX_SINGLE)")
    lora.beginPacket()
    lora.write(list(payload), len(payload))
    # endPacket() inside LoRaRF sets lora._transmitTime = time.time() right
    # before kicking SetTx; we keep our own copy so the elapsed math is
    # independent of LoRaRF internals.
    t_start = time.time()
    lora.endPacket(lora.TX_SINGLE)

    # Replace lora.wait() (busy-loops without sleep) with a friendlier
    # poll so Ctrl+C still works mid-transmit.
    while not quit_requested:
        irq = lora.getIrqStatus()
        if irq:
            break
        time.sleep(RX_POLL_INTERVAL_S)
    if quit_requested:
        log("Ctrl+C during TX, aborting")
        return False

    elapsed_ms = (time.time() - t_start) * 1000
    lora._statusIrq = irq                # let lora.status() see it
    lora._transmitTime = elapsed_ms / 1000  # repair LoRaRF's absolute->delta convention
    if irq & lora.IRQ_TX_DONE:
        log(f"TX_DONE (transmit time ~{elapsed_ms:.2f} ms)")
        return True
    if irq & lora.IRQ_TIMEOUT:
        log("TX_TIMEOUT")
    else:
        log(f"TX failed, IRQ=0x{irq:04X}")
    return False


# --------------------------------------------------------------------- RX


def _format_packet(rssi_db: float, snr_db: float, payload: bytes) -> str:
    rssi_int = int(round(rssi_db))
    snr_int = int(round(snr_db))
    hex_payload = "".join(f"{b:02X}" for b in payload)
    return f"{rssi_int}:{snr_int}:{hex_payload}"


def _arm_rx(lora: SX126x, timeout_ms: int) -> bool:
    """Wrap LoRaRF .request() to do exactly what RAK P2P expects.

    timeout_ms semantics
    --------------------
    1..65532  -> single mode with that ms timeout
    65535     -> single mode, no timeout (RX_SINGLE)
    65534     -> continuous mode (RX_CONTINUOUS)
    """
    if timeout_ms == RAK_RX_SINGLE:
        return lora.request(lora.RX_SINGLE)
    if timeout_ms == RAK_RX_CONTINUOUS:
        return lora.request(lora.RX_CONTINUOUS)
    if 1 <= timeout_ms <= RAK_RX_TIMEOUT_MAX:
        return lora.request(timeout_ms)
    raise ValueError(
        f"timeout {timeout_ms} not allowed: use 1..{RAK_RX_TIMEOUT_MAX}, "
        f"{RAK_RX_SINGLE} (single), or {RAK_RX_CONTINUOUS} (continuous)"
    )


def do_rx(lora: SX126x, timeout_ms: int, log) -> int:
    """Receive packets per RAK P2P semantics.

    Returns the number of packets successfully printed.

    Three distinct modes:

    * continuous (65534)     — chip stays in RX permanently; never re-armed,
                               never times out; Ctrl+C to stop.
    * single-no-timeout (65535) — chip waits in single RX with no chip-level
                               timeout; exit after the FIRST packet.
    * timed (1..65532)       — total listening window of ``timeout_ms`` ms;
                               every received packet is printed; the chip is
                               re-armed with the remaining wall-clock time
                               after each packet (or error); exit when the
                               chip signals IRQ_TIMEOUT (window exhausted) or
                               the deadline passes.
    """
    continuous = (timeout_ms == RAK_RX_CONTINUOUS)
    single_no_timeout = (timeout_ms == RAK_RX_SINGLE)
    timed = not continuous and not single_no_timeout   # 1..65532

    if continuous:
        log("Arming RX in CONTINUOUS mode (Ctrl+C to stop)")
    elif single_no_timeout:
        log("Arming RX in SINGLE mode, no timeout (waits until 1 packet)")
    else:
        log(f"Arming RX with timeout = {timeout_ms} ms")

    # Absolute deadline used only in timed mode to compute remaining budget.
    deadline: float = time.monotonic() + timeout_ms / 1000 if timed else 0.0

    def _remaining_ms() -> int:
        """Milliseconds left until the timed window expires (min 1)."""
        return max(1, int((deadline - time.monotonic()) * 1000))

    def _rearm() -> None:
        """Re-arm RX with appropriate timeout.

        In continuous mode the chip never left RX, so nothing to do.
        In timed mode pass the remaining wall-clock budget so the chip fires
        IRQ_TIMEOUT when the window genuinely expires.
        In single-no-timeout mode re-arm with RX_SINGLE.
        """
        if continuous:
            return
        ms = _remaining_ms() if timed else timeout_ms
        _arm_rx(lora, ms)

    if not _arm_rx(lora, timeout_ms):
        log("WARNING: RX arm failed (already in RX?). Forcing standby first.")
        lora.setStandby(lora.STANDBY_RC)
        _arm_rx(lora, timeout_ms)

    received = 0
    while not quit_requested:
        # In timed mode, also respect the wall-clock deadline so we don't
        # spin forever if the chip never fires IRQ_TIMEOUT (shouldn't happen,
        # but defensive against poll jitter near the boundary).
        if timed and time.monotonic() >= deadline:
            log("RX window expired (wall-clock deadline reached)")
            break

        irq = lora.getIrqStatus()
        if irq == 0:
            time.sleep(RX_POLL_INTERVAL_S)
            continue

        if irq & lora.IRQ_RX_DONE:
            payload_len, buffer_index = lora.getRxBufferStatus()
            if payload_len > 0:
                payload = bytes(lora.readBuffer(buffer_index, payload_len))
                rssi = lora.packetRssi()
                snr = lora.snr()
                print(_format_packet(rssi, snr, payload), flush=True)
                received += 1
            else:
                log(f"WARNING: RX_DONE but payload_len=0 (irq=0x{irq:04X})")
            lora.clearIrqStatus(0x03FF)
            if continuous:
                # Chip remains in RX — just loop.
                pass
            elif timed:
                # Keep listening for the rest of the window.
                _rearm()
            else:
                # single_no_timeout (65535): exit after the first packet.
                break

        elif irq & lora.IRQ_TIMEOUT:
            log("RX_TIMEOUT (no packet within window)")
            lora.clearIrqStatus(0x03FF)
            break

        elif irq & lora.IRQ_CRC_ERR:
            log(f"CRC error (irq=0x{irq:04X})")
            lora.clearIrqStatus(0x03FF)
            if not continuous:
                # Chip fell back to STDBY after the error; re-arm to keep
                # listening (both single-no-timeout and timed modes).
                _rearm()

        elif irq & lora.IRQ_HEADER_ERR:
            log(f"Header error (irq=0x{irq:04X})")
            lora.clearIrqStatus(0x03FF)
            if not continuous:
                _rearm()

        else:
            log(f"Unhandled IRQ 0x{irq:04X}, clearing")
            lora.clearIrqStatus(0x03FF)

    if quit_requested:
        log("Ctrl+C, exiting RX")
    return received


# --------------------------------------------------------------------- main


def main() -> int:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    args = parse_args()

    cfg = parse_p2p_spec(args.p2p)
    sync_word = int(args.sync_word, 0) & 0xFFFF

    log = (lambda *a, **kw: None) if args.quiet else print

    if args.mode == "tx":
        try:
            payload = bytes.fromhex(args.payload_or_timeout.strip())
        except ValueError as exc:
            print(f"ERROR: payload must be hex (e.g. 12121212121212): {exc}",
                  file=sys.stderr)
            return 2
        if not payload:
            print("ERROR: payload cannot be empty", file=sys.stderr)
            return 2
        if len(payload) > 255:
            print("ERROR: LoRa payload max is 255 bytes", file=sys.stderr)
            return 2
        payload_len = len(payload)
    else:
        try:
            timeout_ms = int(args.payload_or_timeout, 0)
        except ValueError:
            print(
                f"ERROR: timeout must be an integer (1..{RAK_RX_TIMEOUT_MAX}, "
                f"{RAK_RX_SINGLE}, or {RAK_RX_CONTINUOUS}): "
                f"{args.payload_or_timeout!r}",
                file=sys.stderr,
            )
            return 2
        # We still need to tell SX1262 a payload-length up front for implicit
        # header. For explicit header it's overridden by the received header
        # but LoRaRF's setLoRaPacket asks for it anyway; 255 = max possible.
        payload_len = 255

    log(f"P2P config: f={cfg.frequency_hz} Hz, SF={cfg.sf}, "
        f"BW={cfg.bw_hz} Hz, CR=4/{cfg.cr}, preamble={cfg.preamble}, "
        f"power={cfg.power_dbm} dBm, sync=0x{sync_word:04X}")

    bridge = Ch341Bridge(vid=args.vid, pid=args.pid, serial=args.serial)
    bridge.open()
    log(f"CH341 opened: VID=0x{args.vid:04X} PID=0x{args.pid:04X}"
        + (f" serial={args.serial!r}" if args.serial else ""))

    rc = 0
    try:
        lora = build_radio(bridge)
        setup_radio(
            lora, cfg, sync_word, payload_len,
            header_explicit=not args.implicit_header,
            crc_on=not args.no_crc,
            invert_iq=args.invert_iq,
            log=log,
        )

        if args.mode == "tx":
            ok = do_tx(lora, payload, log)
            rc = 0 if ok else 1
        else:
            n = do_rx(lora, timeout_ms, log)
            log(f"Received {n} packet(s)")
            rc = 0
    finally:
        log("SetStandby(RC): 0x80 0x00")
        try:
            lora.setStandby(lora.STANDBY_RC)
        except Exception as exc:  # noqa: BLE001
            log(f"WARNING: standby failed: {exc}")
        bridge.close()
        log("CH341 closed")
    return rc


if __name__ == "__main__":
    sys.exit(main())
