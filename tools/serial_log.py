"""Read serial output from the ESP32 for a fixed time, then exit.

Non-interactive replacement for `idf.py monitor` (which blocks).

Usage:
    python tools/serial_log.py --port COM3 --seconds 15 --reset
    python tools/serial_log.py --port COM3 --seconds 30 --out log.txt --timestamps

The ESP32-S3 USB-Serial-JTAG port disappears briefly on reset, so the script
reconnects automatically until the time is up.
"""

from __future__ import annotations

import argparse
import sys
import time

import serial


def open_port(port: str, baud: int) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.1
    # Open with pyserial's default line states (DTR=RTS=asserted). On the
    # USB-Serial-JTAG this does not reset the chip; only RTS=1 with DTR=0 does.
    ser.open()
    return ser


def hard_reset(ser: serial.Serial) -> None:
    # USB-Serial-JTAG chip reset: RTS asserted while DTR released. A pulse of
    # 100 ms was not always detected on Windows; 250 ms works reliably.
    ser.dtr = False
    ser.rts = True
    time.sleep(0.25)
    ser.rts = False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", required=True, help="e.g. COM3 or /dev/ttyACM0")
    ap.add_argument("--seconds", type=float, default=15.0, help="how long to read")
    ap.add_argument("--baud", type=int, default=115200, help="ignored by USB-Serial-JTAG")
    ap.add_argument("--reset", action="store_true", help="hard-reset the chip before reading")
    ap.add_argument("--out", help="also write the log to this file")
    ap.add_argument("--timestamps", action="store_true", help="prefix lines with host time")
    args = ap.parse_args()

    out = open(args.out, "w", encoding="utf-8") if args.out else None
    deadline = time.monotonic() + args.seconds
    start = time.monotonic()
    ser: serial.Serial | None = None
    did_reset = False
    at_line_start = True

    def emit(text: str) -> None:
        nonlocal at_line_start
        if args.timestamps:
            parts = []
            for ch in text:
                if at_line_start:
                    parts.append(f"[{time.monotonic() - start:7.3f}] ")
                    at_line_start = False
                parts.append(ch)
                if ch == "\n":
                    at_line_start = True
            text = "".join(parts)
        sys.stdout.write(text)
        sys.stdout.flush()
        if out:
            out.write(text)

    try:
        while time.monotonic() < deadline:
            if ser is None:
                try:
                    ser = open_port(args.port, args.baud)
                except (serial.SerialException, OSError):
                    time.sleep(0.2)
                    continue
                if args.reset and not did_reset:
                    did_reset = True
                    hard_reset(ser)
            try:
                data = ser.read(4096)
            except (serial.SerialException, OSError):
                # Port vanished (e.g. USB re-enumeration after reset): reconnect.
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                time.sleep(0.2)
                continue
            if data:
                emit(data.decode("utf-8", errors="replace").replace("\r\n", "\n"))
    except KeyboardInterrupt:
        pass
    finally:
        if ser is not None:
            ser.close()
        if out:
            out.close()

    if ser is None and not did_reset and args.reset:
        print(f"\n[serial_log] could not open {args.port}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
