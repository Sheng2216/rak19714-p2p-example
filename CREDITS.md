# Credits

Upstream projects, exact revisions, and local changes used in this repository.

## 1. Semtech SX1261/2 Command Set

`rak19714_p2p.py` configures the radio using the standard Semtech SX1261/2 SPI opcodes (`SetStandby`, `SetRegulatorMode`, `SetDio2AsRfSwitchCtrl`, `SetDio3AsTcxoCtrl`, `SetPacketType`, `SetRfFrequency`, `SetPaConfig`, `SetTxParams`, `SetModulationParams`, `SetPacketParams`) and the sync-word registers at `0x0740` (`0x1424` for private networks, `0x3444` for public networks).

## 2. LoRaRF-Python (`third_party/LoRaRF/`)

| Item | Detail |
| --- | --- |
| Upstream | [chandrawi/LoRaRF-Python](https://github.com/chandrawi/LoRaRF-Python) |
| Revision | `39c664476dc447f50b8f5c1f1c9d77f3fea21140` (`main`, 2024-12-13) |
| License | MIT ([`third_party/LoRaRF/LICENSE`](third_party/LoRaRF/LICENSE)) |
| Copyright | Copyright (c) 2022 Chandra Wijaya Sentosa |

`third_party/LoRaRF/base.py` and `third_party/LoRaRF/SX126x.py` are copied unmodified from commit `39c6644`.

Local adjustments:

- `SX127x.py` is omitted and removed from `third_party/LoRaRF/__init__.py`.
- `lorarf_ch341.py` subclasses `LoRaSpi` and `LoRaGpio` to route SPI and GPIO calls through `Ch341Bridge` (and installs lightweight import stubs on hosts without `spidev` or `gpiod`).
- `rak19714_p2p.py` passes `0x1424` to `setSyncWord()` rather than `SX126x.LORA_SYNC_WORD_PRIVATE` (`0x0741`).

## 3. Userspace CH341 SPI/GPIO Driver (`ch341_bridge.py`)

| Item | Detail |
| --- | --- |
| Upstream | [pine64/libch341-spi-userspace](https://github.com/pine64/libch341-spi-userspace) (`libpinedio-usb.c`, `libpinedio-usb.h`) |
| Revision | `b0694ec84091b293cb1e97e00a821ff88353274f` (`main`) |
| License | `SPDX-License-Identifier: GPL-2.0-or-later` |
| Copyright | Copyright (C) 2024 Marek Kraus; based on flashrom `ch341a_spi.c` (Copyright (C) 2011 asbokid, 2014 Pluto Yang, 2015-2016 Stefan Tauner, 2015 Urja Rannikko) |

`ch341_bridge.py` translates the CH341 USB vendor protocol from `libpinedio-usb.c` into Python on top of `pyusb`:

- `0xA8` SPI stream (LSB-first on the wire; bytes are bit-reversed via a lookup table)
- `0xAB` UIO stream (`0x80` output state, `0x40` direction mask, `0x20` end)
- `0xA0` input status read
- Pre-configures `D0|D1|D2|D3|D5` as outputs before the first UIO packet so `D3` (SCK) and `D5` (MOSI) are actively driven

## 4. Runtime Dependencies

Installed externally (not vendored):

- [pyusb](https://github.com/pyusb/pyusb) — `BSD-3-Clause`
- [libusb](https://github.com/libusb/libusb) (`libusb-1.0`) — `LGPL-2.1`

## 5. Repository License

Because `ch341_bridge.py` is derived from `GPL-2.0-or-later` code and imported by `lorarf_ch341.py` and `rak19714_p2p.py`, this repository is distributed under **GPL-3.0-only** ([`LICENSE`](LICENSE)), while the files under `third_party/LoRaRF/` retain their upstream MIT license notice.
