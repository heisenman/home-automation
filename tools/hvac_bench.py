#!/usr/bin/env python3
"""Drive the hvac bench self-test image (edge/esp32c6-hvac/bench) over USB-Serial-JTAG.

  hvac_bench.py [-p PORT] "relay 19 on" "tx 55 16" ...   run commands, print replies + any RX
  hvac_bench.py [-p PORT] --watch SECONDS                  just print what arrives (RX bytes, RX-pin lows)

Needs pyserial (present in the IDF python env: ~/.espressif/python_env/*/bin/python).
"""
import argparse, sys, time
import serial

ap = argparse.ArgumentParser()
ap.add_argument("-p", "--port", default="/dev/ttyACM0")
ap.add_argument("--watch", type=float, default=0.0)
ap.add_argument("--settle", type=float, default=0.6, help="seconds to collect output after each command")
ap.add_argument("cmds", nargs="*")
a = ap.parse_args()

s = serial.Serial(a.port, 115200, timeout=0.1)
s.dtr = False; s.rts = False   # never reset the chip by opening the port

def drain(secs):
    end = time.time() + secs
    buf = b""
    while time.time() < end:
        buf += s.read(4096)
    for line in buf.decode(errors="replace").splitlines():
        if line.strip():
            print(line, flush=True)

drain(0.3)
for c in a.cmds:
    print(f"> {c}", flush=True)
    s.write((c + "\n").encode())
    drain(a.settle)
if a.watch:
    drain(a.watch)
