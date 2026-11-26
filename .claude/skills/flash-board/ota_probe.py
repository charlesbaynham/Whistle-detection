"""Check the board answers the OTA invitation (UDP 3232). Prints AUTH <nonce> if it does; needs no password."""
import socket, sys
ip = sys.argv[1] if len(sys.argv) > 1 else "10.0.1.52"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)
s.sendto(b"0 41234 1000 d41d8cd98f00b204e9800998ecf8427e\n", (ip, 3232))
try:
    print("reply:", s.recvfrom(100)[0].decode().strip())
except socket.timeout:
    sys.exit("no reply: board off/off Wi-Fi, not on tailnet route, or running non-OTA firmware")
