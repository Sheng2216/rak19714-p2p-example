# RAK19714 P2P Example

This example shows how to send and receive LoRa P2P packets on a host device (`x86_64` / `aarch64`) using the [RAK19714](https://store.rakwireless.com/products/rak19714-lorawan-mini-pcie-module).

The RAK19714 integrates an onboard WCH CH341 USB bridge chip connected to a Semtech SX1262 radio. The script uses `pyusb` (backed by `libusb-1.0`) to send CH341 SPI/GPIO control streams directly to USB device `1a86:5512` to read and write the SX1262.

Note: This example only covers raw LoRa PHY packet (P2P) transmission and reception, and does not include a LoRaWAN example. Always connect an antenna before powering on and transmitting, and make sure your frequency and output power comply with local radio regulations.

## Hardware

Before plugging in the RAK19714, make sure the onboard **EEPROM programming jumper is open**. Shorting that jumper puts the CH341 into I2C EEPROM programming mode, where the SPI channel cannot access the SX1262.

After plugging the board into the host, `lsusb` shows VID `1a86` and PID `5512`:

```text
Bus 001 Device 004: ID 1a86:5512 QinHeng Electronics CH341 in EPP/MEM/I2C mode
```

The device node is located at `/dev/bus/usb/<bus>/<dev>` and opened by `libusb` using `1a86:5512`.

Internal pin connections between the CH341 and SX1262 on the RAK19714:

| CH341 Pin | SX1262 Signal |
| --------- | ------------- |
| D0        | NSS / CS      |
| D1        | RXen          |
| D2        | NRESET        |
| D3        | SCK           |
| D4        | BUSY          |
| D5        | MOSI          |
| D6        | DIO1          |
| D7        | MISO          |
| —         | DIO2          |
| —         | DIO3          |

## Get the Code

Clone with Git:

```bash
git clone https://github.com/RAKWireless/rak19714-p2p-example.git
cd rak19714-p2p-example
```

`third_party/LoRaRF` is already included in the repository directory; no manual download is needed.

## Install Dependencies

### 1. System dependencies

Install Python 3 (with `venv`) and `libusb-1.0`:

```bash
# Example for Debian / Ubuntu / Raspberry Pi OS / RAKPiOS
sudo apt update
sudo apt install -y python3 python3-venv python3-pip libusb-1.0-0
```

### 2. Python virtual environment

Create a virtual environment in the repository root and install `pyusb`:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> The SX1262 driver uses `third_party/LoRaRF` included in this repository (which accepts custom SPI and GPIO objects in its constructor). Do not run `pip install LoRaRF`—version `1.4.0` on PyPI has a different interface and will overwrite the bundled package.

Check the CLI options:

```bash
.venv/bin/python rak19714_p2p.py --help
```

### 3. USB access permissions

Raspberry Pi OS includes `/usr/lib/udev/rules.d/60-flashrom.rules` by default, which already grants `plugdev` read/write access to `1a86:5512`, so you can run the script directly.

If you encounter `Permission denied` on other Linux distributions, verify whether the rule above is present or manually add a `udev` rule:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="5512", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-rak19714-ch341.rules >/dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Run the Example

### Command syntax

If another process is accessing the RAK19714 hardware resources, release it before running the example code.

```bash
# Send a single packet and exit
.venv/bin/python rak19714_p2p.py tx <freq>:<sf>:<bw>:<cr>:<preamble>:<power> <hex_payload> [options]

# Listen for incoming packets
.venv/bin/python rak19714_p2p.py rx <freq>:<sf>:<bw>:<cr>:<preamble>:<power> <timeout_ms> [options]
```

### Two-board test

Prepare two RAK19714 boards with the same RF parameters: 868 MHz, SF7, 125 kHz, CR 4/5 (`0`), 8-symbol preamble, and 14 dBm.

First, start a 60-second receive window on the receiver:

```bash
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 60000
```

Then send 7 bytes of hex data (`12121212121212`) from the transmitter:

```bash
.venv/bin/python rak19714_p2p.py tx 868000000:7:125:0:8:14 12121212121212
```

Transmitter output:

```text
TX_DONE (transmit time ~36.02 ms)
```

For each packet received, the receiver prints one `RSSI:SNR:HEX` line (RSSI in dBm, SNR in dB rounded to an integer, followed by the uppercase hex payload without separators):

```text
-30:13:12121212121212
```

### Receive timeout options

```bash
# Exit after receiving the first valid packet (65535)
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 65535

# Continuous receive, press Ctrl+C to exit (65534)
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 65534
```

When `Ctrl+C` is pressed, the script returns the SX1262 to `STDBY_RC` and releases the USB device.

### Quiet mode (`--quiet`)

Adding `--quiet` suppresses initialization logs so receiver stdout only keeps the `RSSI:SNR:HEX` data lines for piping or scripting:

```bash
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 65535 --quiet
```

In `tx` mode, `TX_DONE` is also omitted; check the process exit code (`0` on success) to determine the result.

## Parameters

The parameter string passed to the script matches the `AT+P2P` format of [RAK RUI3](https://docs.rakwireless.com/product-categories/software-apis-and-libraries/rui3/at-command-manual/#atp2p):

```text
freq:sf:bw:cr:preamble:power
```

| Field      | Accepted values                                                                                                       | Description                        |
| ---------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| `freq`     | `>= 1000000` parsed as Hz; `< 1000000` parsed as MHz (e.g. `868`, `915.5`)                                            | Center frequency                   |
| `sf`       | `5`..`12` (`7`..`12` typical)                                                                                         | Spreading factor                   |
| `bw`       | `0` / `1` / `2` (for 125 / 250 / 500 kHz), or `125` / `250` / `500` (kHz), `125000` / `250000` / `500000` (Hz)        | Bandwidth                          |
| `cr`       | `0`..`3` (for 4/5..4/8), or denominator `5`..`8`                                                                      | Coding rate                        |
| `preamble` | `1`..`65535`                                                                                                          | Preamble length (symbols)          |
| `power`    | `-9`..`22`                                                                                                            | Transmit power (dBm, SX1262 HP PA) |

In `tx` mode, the 3rd positional argument is an even-length hexadecimal string (up to 255 bytes).

In `rx` mode, the 3rd positional argument is the timeout in milliseconds:

| Value        | Behavior                                                                                                   |
| ------------ | ---------------------------------------------------------------------------------------------------------- |
| `1`..`65532` | Listen within the specified window (ms), print a line for each packet, and re-arm with the remaining time |
| `65535`      | Listen until the first valid packet is received, then exit                                                 |
| `65534`      | Continuous receive until `Ctrl+C`                                                                          |

Defaults are Explicit Header, CRC enabled, standard IQ polarity, and sync word `0x1424`. These can be changed via `--implicit-header`, `--no-crc`, `--invert-iq`, and `--sync-word`; the default USB VID/PID `0x1A86` / `0x5512` can be overridden with `--vid` / `--pid`. When the single LoRa symbol duration `2^SF / BW >= 16.384 ms` (for example SF11 and SF12 at 125 kHz bandwidth), the script enables Low Data Rate Optimization (LDRO) in `SetModulationParams`.

## Sync Word

By default, the script writes `0x1424` (`0x14 0x24` at register `0x0740`), which is the private-network sync word defined in the Semtech SX1261/2 datasheet. Both transmitter and receiver must use the same value.

To interoperate with a peer using the public-network sync word, pass `--sync-word 0x3444` on both ends.

## File Structure

```text
rak19714_p2p.py          P2P TX/RX CLI entry point
ch341_bridge.py          CH341 SPI/GPIO driver over pyusb (commands 0xA8, 0xAB, 0xA0)
lorarf_ch341.py          Wraps Ch341Bridge into LoRaRF LoRaSpi / LoRaGpio interfaces
third_party/LoRaRF/      chandrawi/LoRaRF-Python (MIT, SX1262 files only)
requirements.txt         Python dependency (pyusb)
LICENSE                  GNU GPL v3 license text
CREDITS.md               Upstream sources, commit hashes, and modification notes
```

## Credits and License

See [CREDITS.md](CREDITS.md) for upstream sources and modification details:

- `third_party/LoRaRF/` is from [chandrawi/LoRaRF-Python](https://github.com/chandrawi/LoRaRF-Python) (MIT, see [`third_party/LoRaRF/LICENSE`](third_party/LoRaRF/LICENSE)).
- `ch341_bridge.py` is ported from `libpinedio-usb` in [pine64/libch341-spi-userspace](https://github.com/pine64/libch341-spi-userspace) (original C header is `GPL-2.0-or-later`).

This repository is released as a whole under **GPL-3.0-only**; see [LICENSE](LICENSE) for the full text.
