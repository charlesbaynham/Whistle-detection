# /// script
# dependencies = ["pyserial"]
# ///
"""Reset the board and print its serial output for a fixed time, without needing a terminal.

uv run serial_log.py [port] [seconds]
"""

import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 15

s = serial.Serial()
s.port, s.baudrate, s.timeout = port, 115200, 0.2
s.dtr = s.rts = False  # keep G0 high so it boots the app
s.open()
s.rts = True  # pulse EN
time.sleep(0.2)
s.reset_input_buffer()
s.rts = False

end = time.monotonic() + seconds
while time.monotonic() < end:
    sys.stdout.write(s.read(s.in_waiting or 1).decode(errors="replace"))
    sys.stdout.flush()
s.close()
