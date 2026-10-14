# RAK19714 P2P 示例

该示例展示了如何在主机设备（`x86_64` / `aarch64`）上通过 [RAK19714](https://store.rakwireless.com/products/rak19714-lorawan-mini-pcie-module) 收发 LoRa P2P 数据包。

RAK19714 板载一颗 WCH CH341 USB bridge芯片，后端接 Semtech SX1262。脚本通过 `pyusb`（底层调用 `libusb-1.0`）直接向 USB 设备 `1a86:5512` 发送 CH341 的 SPI/GPIO 控制流来读写 SX1262。

注意：此示例代码只展示了LoRa 物理层数据包（P2P）的收发，不提供LoRaWAN示例。上电发射前请先接好天线，并确认频点与发射功率符合当地无线电法规。

## 硬件说明

插上 RAK19714 前，确认板上的 **EEPROM 编程跳线处于断开状态**。短接该跳线会让 CH341 进入 I2C EEPROM 烧录模式，此时 SPI 通道无法访问 SX1262。

插入主机后执行 `lsusb`，可以看到 VID `1a86`、PID `5512`：

```text
Bus 001 Device 004: ID 1a86:5512 QinHeng Electronics CH341 in EPP/MEM/I2C mode
```

设备节点位于 `/dev/bus/usb/<bus>/<dev>`，由 `libusb` 按 `1a86:5512` 打开。

RAK19714芯片内部CH341 与 SX1262 的引脚连接：


| CH341 引脚 | SX1262 信号 |
| -------- | --------- |
| D0       | NSS / CS  |
| D1       | RXen      |
| D2       | NRESET    |
| D3       | SCK       |
| D4       | BUSY      |
| D5       | MOSI      |
| D6       | DIO1      |
| D7       | MISO      |
| —        | DIO2      |
| —        | DIO3      |




## 获取代码

Git 克隆：

```bash
git clone https://github.com/RAKWireless/rak19714-p2p-example.git
cd rak19714-p2p-example
```

`third_party/LoRaRF` 已包含在仓库目录内，无需手动下载。

## 安装依赖



### 1. 系统依赖

安装 Python 3（带 `venv`）和 `libusb-1.0`：

```bash
# 以Debian / Ubuntu / Raspberry Pi OS / RAKPiOS为示例
sudo apt update
sudo apt install -y python3 python3-venv python3-pip libusb-1.0-0
```



### 2. Python 虚拟环境

在仓库根目录创建虚拟环境并安装 `pyusb`：

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> SX1262 驱动直接使用仓库内的 `third_party/LoRaRF`（支持在构造函数中传入自定义 SPI 和 GPIO 对象）。不要执行 `pip install LoRaRF`，PyPI 上的 `1.4.0` 版本接口不同，装上后会覆盖本地包。

查看命令行参数：

```bash
.venv/bin/python rak19714_p2p.py --help
```



### 3. USB 访问权限

Raspberry Pi OS 默认带有 `/usr/lib/udev/rules.d/60-flashrom.rules`，已对 `1a86:5512` 开放了 `plugdev` 组读写权限，直接运行即可。

在其他linux发行版上如果遇到 `Permission denied`，请确认系统是否包含上述规则，或手动添加一条 `udev` 规则：

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="5512", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-rak19714-ch341.rules >/dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger
```



## 运行示例



### 命令格式

如果有别的进程正在访问RAK19714的资源, 请手动释放后再运行示例代码。

```bash
# 发送单包后退出
.venv/bin/python rak19714_p2p.py tx <freq>:<sf>:<bw>:<cr>:<preamble>:<power> <hex_payload> [可选参数]

# 接收监听
.venv/bin/python rak19714_p2p.py rx <freq>:<sf>:<bw>:<cr>:<preamble>:<power> <timeout_ms> [可选参数]
```



### 两机对测

准备两块 RAK19714，使用相同的射频参数：868 MHz、SF7、125 kHz、CR 4/5（`0`）、前导码 8 符号、14 dBm。

先在接收端启动 60 秒监听：

```bash
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 60000
```

再在发射端发送 7 字节十六进制数据 `12121212121212`：

```bash
.venv/bin/python rak19714_p2p.py tx 868000000:7:125:0:8:14 12121212121212
```

发射端输出：

```text
TX_DONE (transmit time ~36.02 ms)
```

接收端每收到一包输出一行 `RSSI:SNR:HEX`（RSSI 单位 dBm，SNR 单位 dB 取整，后面是无分隔符的大写十六进制载荷）：

```text
-30:13:12121212121212
```



### 接收超时选项

```bash
# 收到第 1 个有效包后退出（65535）
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 65535

# 持续接收，按 Ctrl+C 退出（65534）
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 65534
```

按 `Ctrl+C` 时，脚本会将 SX1262 切回 `STDBY_RC` 并释放 USB 设备。

### 安静模式（`--quiet`）

加上 `--quiet` 后不打印初始化日志，接收端标准输出只保留 `RSSI:SNR:HEX` 数据行，便于管道或脚本处理：

```bash
.venv/bin/python rak19714_p2p.py rx 868000000:7:125:0:8:14 65535 --quiet
```

`tx` 模式下 `TX_DONE` 也会被略去，可用进程退出码（成功为 `0`）判断发送结果。

## 参数说明

示例脚本中传入参数的格式与 [RAK RUI3](https://docs.rakwireless.com/product-categories/software-apis-and-libraries/rui3/at-command-manual/#atp2p) 的 `AT+P2P` 保持一致：

```text
freq:sf:bw:cr:preamble:power
```


| 字段         | 取值                                                                                                         | 说明                     |
| ---------- | ---------------------------------------------------------------------------------------------------------- | ---------------------- |
| `freq`     | `>= 1000000` 按 Hz 解析；`< 1000000` 按 MHz 解析（如 `868`、`915.5`）                                                 | 中心频率                   |
| `sf`       | `5`..`12`（常用 `7`..`12`）                                                                                    | 扩频因子                   |
| `bw`       | `0` / `1` / `2`（对应 125 / 250 / 500 kHz），或直接填 `125` / `250` / `500`（kHz）、`125000` / `250000` / `500000`（Hz） | 带宽                     |
| `cr`       | `0`..`3`（对应 4/5..4/8），或填分母 `5`..`8`                                                                        | 编码率                    |
| `preamble` | `1`..`65535`                                                                                               | 前导码长度（符号数）             |
| `power`    | `-9`..`22`                                                                                                 | 发射功率（dBm，SX1262 HP PA） |


`tx` 模式的第 3 个位置参数为偶数长度的十六进制字符串（最长 255 字节）。

`rx` 模式的第 3 个位置参数为超时时间（毫秒）：


| 值            | 行为                                |
| ------------ | --------------------------------- |
| `1`..`65532` | 在指定毫秒内持续监听，每收到一包打印一行，并用剩余时间重新开启接收 |
| `65535`      | 监听直到收到第 1 个有效包后退出                 |
| `65534`      | 连续接收，直到收到 `Ctrl+C`                |


默认配置为显式包头（Explicit Header）、开启 CRC、标准 IQ 极性、同步字 `0x1424`。可通过 `--implicit-header`、`--no-crc`、`--invert-iq`、`--sync-word` 修改；USB VID/PID 默认 `0x1A86` / `0x5512`，可通过 `--vid` / `--pid` 覆盖。 当单个 LoRa 符号时长 `2^SF / BW >= 16.384 ms`（例如 125 kHz 带宽下的 SF11、SF12）时，脚本会在 `SetModulationParams` 中启用低速率优化（LDRO）。

## 同步字（Sync Word）说明

脚本默认写入 `0x1424`（寄存器 `0x0740` 写入 `0x14 0x24`），对应 Semtech SX1261/2 数据手册定义的私有网同步字。收发双方必须一致。

如需与使用公共网同步字的对端互通，可在收发两端同时加上 `--sync-word 0x3444`。

## 文件结构

```text
rak19714_p2p.py          P2P 收发命令行入口
ch341_bridge.py          基于 pyusb 的 CH341 SPI/GPIO 驱动（命令 0xA8、0xAB、0xA0）
lorarf_ch341.py          将 Ch341Bridge 封装为 LoRaRF 的 LoRaSpi / LoRaGpio 接口
third_party/LoRaRF/      chandrawi/LoRaRF-Python（MIT，仅保留 SX1262 相关文件）
requirements.txt         Python 依赖（pyusb）
LICENSE                  GNU GPL v3 许可证正文
CREDITS.md               上游代码来源、提交号与修改记录
```



## 致谢与许可证

代码来源与修改细节见 [CREDITS.md](CREDITS.md)：

- `third_party/LoRaRF/` 来自 [chandrawi/LoRaRF-Python](https://github.com/chandrawi/LoRaRF-Python)（MIT，见 `[third_party/LoRaRF/LICENSE](third_party/LoRaRF/LICENSE)`）。
- `ch341_bridge.py` 移植自 [pine64/libch341-spi-userspace](https://github.com/pine64/libch341-spi-userspace) 的 `libpinedio-usb`（原 C 文件头为 `GPL-2.0-or-later`）。

本仓库整体按 **GPL-3.0-only** 发布，许可证全文见 [LICENSE](LICENSE)。