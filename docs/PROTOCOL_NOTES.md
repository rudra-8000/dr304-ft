# D.R304 protocol — reverse-engineering notes

These notes record how the protocol behind `dr304_ft` was recovered, so it can be checked or extended later.

## Sources

| Vendor file | Content |
|---|---|
| `D.R304高速采集系统使用说明new.doc` | GUI manual (Chinese), wiring of a 485 cable, no protocol |
| `Six dimensional force sensor debugging guide.docx` | English set-up guide, screenshots of the parameter table |
| `操作指导视频_cn.mp4` | 7 min video: 24 V, 485-USB cable, Ethernet direct or via switch |
| `系统安装包System installation package/bin/dp/data.cab` | the actual app: `D.R304高速采集系统.exe`, `D.R304调试工具.exe`, manual PDF, dongle DLL |
| `485驱动485drive/891.zip` | Silicon Labs CP210x Windows driver → UT-891 is a CP210x (Linux: in-kernel `cp210x`) |

## Method

1. `cabextract bin/dp/data.cab` → two LabVIEW 2020 executables.
2. Each EXE embeds an `RSRC … LVAR LBVW` resource (an LLB). Carve it out from the `RSRC` magic to EOF.
3. `pylabview` (github.com/mefistotelis/pylabview): `readRSRC -x` gives an `LVzp` block, which is a zip of the VIs.
   Zip names are GBK.
4. `readRSRC -x -t gbk` on each VI. The **DFDS** (default data space) section holds every block-diagram
   constant, labelled with its terminal name (起始地址 = start address, 寄存器数量 = register count, 偏移量 = offset,
   写入数据 = data to write).

## Key constants found

Debug tool `D.R304调试工具.vi` (RS485, Modbus RTU via sub-VIs `modbusRTU03.vi` / `modbusRTU10.vi` / `crc16.vi`):

```
read params    start "\x06\x16"(1558) count 4        -> force & torque decimals
               start "\x06\x38"(1592) count 2, y=10  -> data format, tens digit = word order
               start "\x06\x20"(1568) count 2
live force     raw "03 04 00 00 0C"  (fc3, 0x0400, 12 regs), wait 80 ms,
               substrings at 3,7,11,15,19,23 len 4 -> fx fy fz mx my mz
               (the 4-byte fields are IEEE-754 float32, see "Hardware findings" below)
live mV/V      raw "03 0A 50 00 0C"  (fc3, 0x0A50, 12 regs), same offsets
IP             read 1598 count 6, offsets 3/7/11; write 1598, 1600 (each x1000 split), 1602
save           write "\x0A\x20"(2592) <- 0x00000028 (40)  -> "保存成功"
factory reset  write 2592 <- 30, then 2592 <- 40     -> "请重启变送器"
param table    see registers.py (names, addresses, decimals, "long")
```

GUI `304数据处理.vi` (Ethernet, protocol 2 "DAS_TCP"):

```
TCP read 71 bytes, timeout 500 ms
substrings: (19,len2) (21) (22,len1) and six (25|32|39|46|53|60, len 7) parsed with "%d"
commands (CR-terminated): "ZERO 000" + " +0000000 "     zero
                          "WRIT 0008 +0000020" / 10 / 2 / 1   GUI pairs these with "packet size" 5/10/50/100; 0008 is probably the send interval in ms
                          "WRIT 0039 ..."
```

## Hardware findings (first contact, 2026-10-20, UT-891 / CP210x, 115200 8N1, unit 1)

* **Live data is float32, not int32.** Reply to `01 03 04 00 00 0C` is `01 03 18 | 42 9c 8f 5c  c1 e4 7a e1  42 03 d7 0a
  bf 7c ac 08  bf cf 5c 29  40 42 b0 21 | crc` = 78.28, -28.56, 32.96 N, -0.987, -1.62, 3.04 N.m, high word first. This was
  the one wrong inference from the VI constants (the substring offsets say "4 bytes", not their type); the type
  cast is a separate LabVIEW node that we did not read. `force_decimals`/`torque_decimals` (1558/1560 = 2/3) are only
  display rounding. The mV/V block at 0x0A50 *is* int32 (e.g. 12981, 33501, -13531).
* Register 1592 is serial framing (units digit) + word order (tens digit), not a data-type flag; ours reads 0.
* Network registers: 1598..1603 read `192168, 1100, 502`, 1604 reads 1 (Modbus TCP) → IP 192.168.1.100:502, so
  `ip0·1000+ip1` / `ip2·1000+ip3` is confirmed for reads. 1606.. holds a second address (`192168, 1110`) and `60`
  which look like the DAS_TCP push target; not used.
* Our unit: unit=N, decimals 2/3, picking_rate=4 (100 Hz), filter_depth=8, RS485 Modbus RTU 115200, packet_interval
  0.010 s, eth_interval 0.025 s, K1..K6 = 10000.
* Reads of 1538..1561 (24 regs) and 0x0400 (12 regs) work; a 100-register read from 1538 is not served.
* **Timing (the cause of the first "no D.R304 answered").**
  * Reply starts ≈ 10.6 ms after the request, regardless of register count; a 12-register reply is 29 bytes (2.5 ms
    at 115200).
  * A request starting < ~1 ms after the end of the previous reply is never answered, 1-2 ms about half the time,
    ≥ 2 ms always (500 random trials). 3 ms showed ~2 % losses in a tight loop, 4 ms none (8 s, 0 retries).
  * After an ignored request the transmitter stays deaf for as long as bytes keep arriving: 50-65 ms polling
    never recovers, ≥ 100 ms of silence does. The old client (no gap, no recovery) therefore failed on every second
    request, and `--baud auto` only tried unit 1 / 8N1 and swallowed the error.
  * Resulting RS485 ceiling: ≈ 55 reads/s (≈ 18 ms per read incl. 4 ms gap). 30 Hz is exact (std of period 0.5 ms).
* Writes work: `set eth_protocol`, `set picking_rate 7`, `set-ip` (which also saves with command 40) persisted
  across a power cycle.
* **Modbus TCP works** (same register map and float32 data as RTU): 120 Hz record, 1201 samples in 10 s, period
  8.33 ms ± 0.30 ms (max 8.82), no sequence gaps, 0 errors. Reads need no inter-request gap workaround over TCP so far.
* **Noise:** the unloaded sensor shows a 50 Hz component (≈ 3 N on Fy/Fz, 0.4 N on Fx; folds to 10 Hz at 30 Hz sampling,
  Fy std 2.8 N). Gripper on/off: identical. `filter_depth` 8 → 16 (picking_rate 7 = 800 Hz): Fy std 0.048 N,
  Fz 0.028 N, torques ≈ 0.001 N·m. Consistent with `filter_depth` = moving-average length in ADC samples (16 / 800 Hz = 20 ms).
* Not yet tested on hardware: `low_word_first`, AutoSend (RS485 protocol 2) frame format, DAS_TCP.

## Still unknown

* DAS_TCP frame header (bytes 0–24) and terminator.
* Register 1568 (read by the tool; probably model or version).
* Whether the device zero ("清零") is reachable over Modbus. The package does software tare instead.
