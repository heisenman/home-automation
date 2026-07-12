#!/usr/bin/env python3
"""Non-intrusive serial log reader for bench work.

Opens a serial port WITHOUT asserting DTR/RTS, so an ESP32/ESP8266 is NOT reset
when we attach (plain `cat`/`screen`/`miniterm` toggle the reset lines and reboot
the board). Use to read an already-running device's ESPHome/native logs on the bench.

Usage:  tools/bench_serial_read.py [PORT] [BAUD] [SECONDS]
Default: /dev/ttyACM0 115200 8

Requires pyserial (in the repo venv):  venv/bin/python tools/bench_serial_read.py ...
"""
import sys
import time
import serial  # pyserial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

s = serial.Serial()
s.port = port
s.baudrate = baud
s.dtr = False   # do NOT toggle the ESP reset/boot lines on open
s.rts = False
s.timeout = 0.2
s.open()

t0 = time.time()
got = False
while time.time() - t0 < secs:
    data = s.read(4096)
    if data:
        got = True
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
s.close()

if not got:
    sys.stderr.write(f"\n[no output on {port}@{baud} — device idle/silent, wrong baud, or another reader holds the port]\n")
