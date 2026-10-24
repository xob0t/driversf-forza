"""Type into a VM over VNC (RFB 3.8, no auth), for debugging a headless install before
VMware Tools runs. Usage:
  uv run dsff vnckeys host port STEP...
Steps: 'text:<chars>' types ASCII text; '{enter}', '{esc}', '{tab}', '{win+r}', '{win}',
'{alt+f4}', '{ctrl+shift+enter}' press keys; 'sleep:<seconds>' waits."""
import socket
import struct
import sys
import time

KEYS = {"enter": 0xFF0D, "esc": 0xFF1B, "tab": 0xFF09, "win": 0xFFEB, "alt": 0xFFE9, "ctrl": 0xFFE3,
        "shift": 0xFFE1, "f4": 0xFFC1, "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
        "backspace": 0xFF08, "space": 0x20}
SHIFTED = '~!@#$%^&*()_+{}|:"<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def connect(host, port):
    s = socket.create_connection((host, port), timeout=20)
    recv_exact(s, 12)
    s.sendall(b"RFB 003.008\n")
    n = recv_exact(s, 1)[0]
    if 1 not in recv_exact(s, n):
        raise SystemExit("no 'None' security type")
    s.sendall(b"\x01")
    if struct.unpack(">I", recv_exact(s, 4))[0] != 0:
        raise SystemExit("security handshake failed")
    s.sendall(b"\x01")
    recv_exact(s, 20)
    recv_exact(s, struct.unpack(">I", recv_exact(s, 4))[0])
    return s


def key(s, sym, down):
    s.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, sym))


def press(s, syms):
    for sym in syms:
        key(s, sym, True)
        time.sleep(0.03)
    for sym in reversed(syms):
        key(s, sym, False)
        time.sleep(0.03)
    time.sleep(0.1)


def main():
    s = connect(sys.argv[1], int(sys.argv[2]))
    for step in sys.argv[3:]:
        if step.startswith("text:"):
            for ch in step[5:]:
                sym = ord(ch)
                press(s, [KEYS["shift"], sym] if ch in SHIFTED else [sym])
        elif step.startswith("sleep:"):
            time.sleep(float(step[6:]))
        elif step.startswith("{") and step.endswith("}"):
            press(s, [KEYS[k] if k in KEYS else ord(k) for k in step[1:-1].split("+")])
        else:
            raise SystemExit(f"bad step {step}")
    s.close()


if __name__ == "__main__":
    main()
