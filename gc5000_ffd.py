#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gc5000_ffd.py -- Force-Feedback-Proxy-Daemon fuer den Nokia Game Controller 5000
=================================================================================

Grundlage (Ergebnis der hidraw-Recherche, Oktober 2026):
  Report-ID 0x01, 2 Byte Payload, binäre Motorsteuerung:
    Byte 0 = starker Motor   (0x00 = aus, sonst an)
    Byte 1 = schwacher Motor  (0x00 = aus, sonst an)
  Stop-Report 0x01:0000 beendet die Vibration sofort.
  (Amplitude ist binär: 0x01 und 0xFF fuehlen sich gleich an.)

Der Daemon:
  1. Findet hidraw-Device und Gamepad-Event-Device des Controllers.
  2. Legt ein uinput-Gerät mit identischen Buttons/Achsen plus
     FF-Fähigkeiten (FF_RUMBLE, FF_PERIODIC, FF_CONSTANT, FF_GAIN) an.
  3. Grabbed das echte Gamepad (EVIOCGRAB), sodass Spiele nur den
     Proxy sehen. Alle Eingabe-Events werden 1:1 durchgereicht.
  4. Übersetzt FF-Uploads/-Playback der Spiele in hidraw-Reports.

Validierung:
    Terminal 1:  sudo python3 gc5000_ffd.py
    Terminal 2:  fftest /dev/input/<event-Node-des-Proxys>
                 -> Effekte 0 (sinus), 4 (strong), 5 (weak) spielen.
                 Das Pad muss vibrieren; 'q' zum Stoppen sendet 0x00.

Hinweise:
  - Vor dem Start laufende Spiele/Steam beenden bzw. Steam danach neu
    starten, damit der Proxy erkannt wird.
  - FF_GAIN wird binär interpretiert (gain==0 -> Motoren aus).
  - FF-Protokoll: EV_UINPUT-Event (request_id im 'value') plus
    UI_BEGIN_FF_UPLOAD/UI_END_FF_UPLOAD- bzw. _FF_ERASE-Ioctl, Strukturen
    104/12 Byte (s. uinput.h des Kernels). ioctl-Nummern, FF-Codes und
    EV_UINPUT werden zur Laufzeit aus den UAPI-Headern gelesen.

Als systemd-Dienst, gestartet sobald der Controller per Bluetooth erscheint
(udev-Regel auf hidraw-Add von 1D5A:1802):

  /etc/systemd/system/gc5000-ffd.service
  ------------------------------------------------------------
  [Unit]
  Description=GC5000 Force-Feedback-Proxy
  StartLimitIntervalSec=0
  [Service]
  Type=simple
  ExecStart=/usr/bin/python3 /usr/local/bin/gc5000_ffd.py --wait 10
  Restart=on-failure
  RestartSec=2
  # Kein [Install]-Abschnitt: der Dienst wird ausschliesslich per
  # udev-Regel getriggert. BT-Disconnect beendet ihn mit Exit 0
  # (kein Restart-Loop, solange das Pad weg ist).
  ------------------------------------------------------------

  /etc/udev/rules.d/90-gc5000.rules
  ------------------------------------------------------------
  # Achtung: BT-HID-Geraete (HID-Bus 0005) haben KEINE idVendor/idProduct-
  # Attribute (die sind USB-spezifisch) -> ueber den Parent-Namen matchen:
  ACTION=="add", SUBSYSTEM=="hidraw", KERNELS=="0005:1D5A:1802.*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="gc5000-ffd.service"
  ------------------------------------------------------------

Einrichtung (einmalig):
  sudo install -m 755 gc5000_ffd.py /usr/local/bin/gc5000_ffd.py
  sudo systemctl daemon-reload
  sudo udevadm control --reload
  # Controller ausschalten, neu verbinden -> Dienst startet automatisch:
  systemctl status gc5000-ffd

Abhängigkeiten: nur Python-Stdlib. Muss als root laufen (uinput + hidraw + grab).
"""

import argparse
import fcntl
import math
import os
import select
import struct
import sys
import time

VID, PID = 0x1D5A, 0x1802
REPORT_ID = 0x01
PAD_NAME = "Nokia GC 5000"

# ------------------------------------------------------------ Kernel-Konstanten
EV_SYN, EV_KEY, EV_ABS, EV_MSC = 0x00, 0x01, 0x03, 0x04
EV_FF = 0x15
EV_UINPUT = 0x0410
# Fallback-Werte (Mainline-Stand); werden per _resolve_event_codes() aus den
# UAPI-Headern ueberschrieben -- der Kernel auf roamer hat die FF-Codes
# umnummeriert (FF_RUMBLE liegt dort NICHT bei 0x50).
FF_RUMBLE, FF_PERIODIC, FF_CONSTANT = 0x50, 0x51, 0x52
FF_SQUARE, FF_TRIANGLE, FF_SINE = 0x58, 0x59, 0x5a
FF_GAIN, FF_AUTOCENTER = 0x60, 0x61
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2


def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | (t << 8) | nr


# Fallback-Werte, falls /usr/include/linux/uinput.h nicht lesbar ist.
# Die tatsaechlichen Werte werden zur Laufzeit aus dem Header geholt:
# uinput antwortet auf unbekannte ioctl-Nummern mit EINVAL, deshalb
# raten wir hier NICHT mehr.
UI = {
    "UI_SET_EVBIT":  _IOC(1, 0x55, 100, 4),
    "UI_SET_KEYBIT": _IOC(1, 0x55, 101, 4),
    "UI_SET_ABSBIT": _IOC(1, 0x55, 103, 4),
    "UI_SET_MSCBIT": _IOC(1, 0x55, 104, 4),
    "UI_SET_FFBIT":  _IOC(1, 0x55, 107, 4),
    "UI_DEV_CREATE": (0x55 << 8) | 1,
    "UI_DEV_DESTROY": (0x55 << 8) | 2,
    # FF-Handshake-Protokoll (s. uinput.h des Kernels): EV_UINPUT-Event
    # mit request_id im 'value', dann BEGIN/END-Ioctl mit dieser Struktur.
    "UI_BEGIN_FF_UPLOAD": _IOC(3, 0x55, 200, 104),
    "UI_END_FF_UPLOAD":   _IOC(1, 0x55, 201, 104),
    "UI_BEGIN_FF_ERASE":  _IOC(3, 0x55, 202, 12),
    "UI_END_FF_ERASE":    _IOC(1, 0x55, 203, 12),
}
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
EV_UINPUT = 0x0410
EVIOCGRAB = _IOC(1, 0x45, 0x90, 4)


def _resolve_from_header(verbose=False):
    """UI_*-Konstanten aus /usr/include/linux/uinput.h parsen."""
    global UI_FF_UPLOAD, UI_FF_ERASE, EV_UINPUT
    import re
    try:
        with open("/usr/include/linux/uinput.h") as f:
            text = f.read()
    except OSError as e:
        print(f"Warnung: uinput.h nicht lesbar ({e}), nutze Fallback-Konstanten.")
        return
    for name in ("UI_FF_UPLOAD", "UI_FF_ERASE"):
        m = re.search(r"#define\s+%s\s+(\d+)" % name, text)
        if m:
            globals()[name] = int(m.group(1))
    m = re.search(r"#define\s+EV_UINPUT\s+0x([0-9a-fA-F]+)", text)
    if m:
        EV_UINPUT = int(m.group(1), 16)
    sizes = {"int": 4, "struct uinput_setup": 92, "struct uinput_abs_setup": 28,
             "struct uinput_ff_upload": 104, "struct uinput_ff_erase": 12}
    rx = re.compile(r"#define\s+(UI_[A-Z_]+)\s+(_IOWR|_IOW|_IOR|_IO)"
                    r"\(\s*(?:'U'|UINPUT_IOCTL_BASE)\s*,\s*(\d+)(?:\s*,\s*([^)]+))?\)")
    for m in rx.finditer(text):
        name, macro, nr, typ = m.groups()
        d = {"_IOWR": 3, "_IOW": 1, "_IOR": 2, "_IO": 0}[macro]
        typ = (typ or "").strip()
        if macro == "_IO":
            size = 0                      # _IO hat kein Datenelement: Groesse 0!
        elif typ in sizes:
            size = sizes[typ]
        else:
            arr = re.search(r"\[(\d+)\]", typ)
            size = int(arr.group(1)) if (typ.startswith("char") and arr) else 4
        UI[name] = _IOC(d, 0x55, int(nr), size)
    if verbose:
        for k in ("UI_SET_FFBIT", "UI_DEV_CREATE", "UI_DEV_DESTROY"):
            print(f"  {k} = 0x{UI[k]:08x}")


def _resolve_event_codes(verbose=False):
    """EV_FF- und FF-Code-Werte aus den UAPI-Headern holen.

    Neuere Kernel haben die FF-Codes umnummeriert; die Werte duerfen nicht
    mehr hardcoded werden. Gesucht wird in input-event-codes.h und input.h,
    sowohl #define- als auch enum-Stil ('FF_RUMBLE = 42').
    """
    global FF_RUMBLE, FF_PERIODIC, FF_CONSTANT, FF_GAIN, FF_AUTOCENTER, EV_FF
    import re
    names = ("EV_FF", "FF_RUMBLE", "FF_PERIODIC", "FF_CONSTANT",
             "FF_SQUARE", "FF_TRIANGLE", "FF_SINE",
             "FF_GAIN", "FF_AUTOCENTER")
    text = ""
    for path in ("/usr/include/linux/input-event-codes.h", "/usr/include/linux/input.h"):
        try:
            with open(path) as f:
                text += f.read() + "\n"
        except OSError:
            pass
    if not text:
        print("Warnung: input-event-codes.h nicht lesbar -- FF-Codes bleiben Fallback.")
        return
    for name in names:
        m = (re.search(r"#define\s+%s\s+[\(]?\s*(0[xX][0-9a-fA-F]+|\d+)" % name, text)
             or re.search(r"\b%s\s*=\s*(0[xX][0-9a-fA-F]+|\d+)\b" % name, text))
        if m:
            globals()[name] = int(m.group(1), 0)
        else:
            print(f"Warnung: {name} nicht im Header gefunden, behalte Fallback "
                  f"{globals()[name]:#x}.")
    if verbose:
        print("  FF-Codes: " + ", ".join(f"{n}={globals()[n]:#x}" for n in names))


def EVIOCGBIT(ev, size):
    return _IOC(2, 0x45, 0x20 + ev, size)


def EVIOCGABS(code):
    return _IOC(2, 0x45, 0x40 + code, 24)


# Strukturgrößen fuer x86_64 / aktuelles glibc
FMT_EVENT     = "<qqHHi"      # struct input_event      = 24
FMT_UPLOAD    = "<Ii48s48s"   # struct uinput_ff_upload = 104 (request_id, retval, effect, old)
FMT_ERASE     = "<IiI"        # struct uinput_ff_erase   = 12 (request_id, retval, effect_id)
FMT_SETUP     = "<HHHHI80s"  # struct uinput_setup      = 92
FMT_ABS_SETUP = "<H2x6i"     # struct uinput_abs_setup = 28
FMT_ABSINFO   = "<6i"         # struct input_absinfo    = 24


# --------------------------------------------------------------------- hidraw
def hid_device_dir(node):
    """HID-Device-Verzeichnis hinter einem hidraw-Node finden (s. Probe-Skript)."""
    real = os.path.realpath(os.path.join("/sys/class/hidraw", os.path.basename(node)))
    for cand in (os.path.join(real, "device"),
                 os.path.dirname(os.path.dirname(real)),
                 real):
        cand = os.path.realpath(cand) if os.path.exists(cand) else cand
        uevent = os.path.join(cand, "uevent")
        if os.path.isfile(uevent):
            try:
                with open(uevent) as f:
                    if "HID_ID=" in f.read():
                        return cand
            except OSError:
                continue
    return None


def find_hidraw():
    for entry in sorted(os.listdir("/sys/class/hidraw")):
        node = "/dev/" + entry
        sysfs = hid_device_dir(node)
        if not sysfs:
            continue
        try:
            with open(os.path.join(sysfs, "uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        hid_id = ""
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                hid_id = line.split("=", 1)[1]
        try:
            _bus, vid, pid = (int(x, 16) for x in hid_id.split(":"))
        except ValueError:
            continue
        if vid == VID and pid == PID:
            return node
    return None


REFRESH_INTERVAL = 0.07  # s: Heartbeat (kurz == dichte Burst-Overlaps == weicher Dauerton)
PWM_TICK = 0.01        # s: Auswertetakt der Periodic-Amplitude (100 Hz)
FF_LEVEL_LOW = 0.35    # Sinusbetrag darunter: Stille
FF_LEVEL_HIGH = 0.7    # darunter: nur schwacher Motor (leise Stufe), darueber: beide


class Rumble:
    """Binäre Motorsteuerung ueber hidraw (Report-ID 0x01, 2 Byte).

    Die Firmware des GC5000 hoert nach kurzer Zeit auf zu vibrieren, wenn der
    Host nicht nachlegt: Jeder Report mit Wert != 0x00 loest nur einen Burst
    aus. set() schreibt bei Zustandswechsel, refresh() wiederholt das aktuelle
    Report (Heartbeat), solange ein Motor aktiv ist.
    """

    def __init__(self, node, verbose=False):
        self.node = node
        self.verbose = verbose
        self.fd = None
        self.current = (0, 0)

    def _report(self, target):
        return bytes([REPORT_ID, 0xFF * target[0], 0xFF * target[1]])

    def stop(self):
        self.set(0, 0)

    def set(self, strong, weak):
        target = (1 if strong else 0, 1 if weak else 0)
        if target == self.current:
            return
        self.current = target
        self._send_current(log=True)

    def refresh(self):
        """Heartbeat: aktuelles Report erneut senden (Burst-Firmware)."""
        if self.current == (0, 0):
            return
        self._send_current(log=False)

    def _send_current(self, log=False):
        try:
            if self.fd is None:
                self.fd = os.open(self.node, os.O_WRONLY | os.O_NONBLOCK)
            os.write(self.fd, self._report(self.current))
            if log and self.verbose:
                print(f"  rumble -> strong={self.current[0]} weak={self.current[1]}")
        except OSError as e:
            print(f"hidraw-Fehler ({e}), Reopen beim nächsten Versuch", file=sys.stderr)
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None


# --------------------------------------------------------- Gamepad-Device finden
def find_pad_event():
    """Das Gamepad-Event-Device (nicht Consumer Control) aus /proc auslesen."""
    try:
        with open("/proc/bus/input/devices") as f:
            content = f.read()
    except OSError as e:
        sys.exit(f"Kann /proc/bus/input/devices nicht lesen: {e}")
    for block in content.strip().split("\n\n"):
        name, handlers, abs_bits = None, [], None
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line[len("H: Handlers="):].split()
            elif line.startswith("B: ABS="):
                abs_bits = line[len("B: ABS="):].strip()
        if name == PAD_NAME and abs_bits and set(abs_bits) != {"0"}:
            events = [h for h in handlers if h.startswith("event")]
            if events:
                return "/dev/input/" + events[0]
    return None


# ------------------------------------------------------------------ uinput
def ioctl_read(fd, req, size):
    buf = bytearray(size)
    fcntl.ioctl(fd, req, buf, True)
    return bytes(buf)


def bitfield(buf):
    v = int.from_bytes(buf, "little")
    return [i for i in range(len(buf) * 8) if (v >> i) & 1]


PROXY_NAME = b"Nokia GC 5000 (FF-Proxy)"
ABS_CNT = 64


def create_uinput(pad_fd, verbose=False):
    """Virtuelles Gamepad mit identischen Achsen/Tasten plus FF anlegen.

    Bewusst Legacy-API: Name/ID/ff_effects_max/ABS-Werte werden als
    struct uinput_user_dev geschrieben (stabil seit Kernel 2.6) statt
    ueber UI_DEV_SETUP/UI_ABS_SETUP.
    """
    fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)

    evbits = bitfield(ioctl_read(pad_fd, EVIOCGBIT(0, 4), 4))
    for t in evbits:
        if t in (EV_SYN, EV_KEY, EV_ABS, EV_MSC):
            fcntl.ioctl(fd, UI["UI_SET_EVBIT"], t)

    if EV_KEY in evbits:
        for c in bitfield(ioctl_read(pad_fd, EVIOCGBIT(EV_KEY, 96), 96)):
            fcntl.ioctl(fd, UI["UI_SET_KEYBIT"], c)
    if EV_MSC in evbits:
        for c in bitfield(ioctl_read(pad_fd, EVIOCGBIT(EV_MSC, 8), 8)):
            fcntl.ioctl(fd, UI["UI_SET_MSCBIT"], c)

    # Force Feedback anbieten (Report-Nummern aus dem Kernel-Header!)
    fcntl.ioctl(fd, UI["UI_SET_EVBIT"], EV_FF)
    # Periodische Wellenformen sind in diesem Kernel eigene FF-Bits und
    # werden beim Periodic-Upload separat validiert -> mitsetzen.
    for ffb in (FF_RUMBLE, FF_PERIODIC, FF_CONSTANT, FF_GAIN,
                FF_SQUARE, FF_TRIANGLE, FF_SINE):
        fcntl.ioctl(fd, UI["UI_SET_FFBIT"], ffb)

    # ABS-Bits setzen und Absinfo aus dem echten Pad uebernehmen
    absbits = bitfield(ioctl_read(pad_fd, EVIOCGBIT(EV_ABS, 8), 8)) if EV_ABS in evbits else []
    absmax, absmin, absfuzz, absflat = ([0] * ABS_CNT for _ in range(4))
    for c in absbits:
        fcntl.ioctl(fd, UI["UI_SET_ABSBIT"], c)
        info = struct.unpack(FMT_ABSINFO, ioctl_read(pad_fd, EVIOCGABS(c), 24))
        _value, absmin[c], absmax[c], absfuzz[c], absflat[c], _res = info

    # Legacy: struct uinput_user_dev (1116 Byte) schreiben, dann DEV_CREATE
    user_dev = (struct.pack("<80sHHHHI", PROXY_NAME, 0x05, VID, PID, 0x0100, 32)
                + struct.pack("<64i", *absmax)
                + struct.pack("<64i", *absmin)
                + struct.pack("<64i", *absfuzz)
                + struct.pack("<64i", *absflat))
    write_all(fd, user_dev)
    fcntl.ioctl(fd, UI["UI_DEV_CREATE"])

    # Event-Node ueber sysfs Namenssuche finden (kein UI_GET_SYSNAME noetig)
    event_path = None
    for entry in sorted(os.listdir("/sys/class/input")):
        try:
            with open(f"/sys/class/input/{entry}/name") as f:
                if f.read().strip() == PROXY_NAME.decode():
                    for sub in os.listdir(f"/sys/class/input/{entry}"):
                        if sub.startswith("event"):
                            event_path = "/dev/input/" + sub
        except OSError:
            continue
    return fd, event_path


def write_all(fd, data):
    while data:
        n = os.write(fd, data)
        data = data[n:]


class Stream:
    """Eingehende Bytes puffern und in input_events / FF-Structs zerlegen."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data

    def next_event(self):
        if len(self.buf) < 24:
            return None
        etype, code, value = struct.unpack_from("<HHi", self.buf, 16)
        self.buf = self.buf[24:]
        return etype, code, value

    def read_raw(self, n):
        if len(self.buf) < n:
            return None
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def parse_effect(effect):
    """(strong, weak, length_ms, period_ms) aus einem 48-Byte-ff_effect holen."""
    etype = struct.unpack_from("<H", effect, 0)[0]
    length = struct.unpack_from("<H", effect, 10)[0]
    if etype == FF_RUMBLE:
        strong, weak = struct.unpack_from("<2H", effect, 16)
        return strong > 0, weak > 0, length, 0
    if etype == FF_PERIODIC:
        mag = struct.unpack_from("<H", effect, 20)[0]
        period = struct.unpack_from("<H", effect, 18)[0]
        return mag > 0, mag > 0, length, period
    if etype == FF_CONSTANT:
        level = struct.unpack_from("<h", effect, 16)[0]
        return level != 0, level != 0, length, 0
    return None  # nicht unterstuetzt


def run_proxy(args):
    # udev-Start: hidraw-Node existiert evtl. schon, waehrend der Event-Node
    # des Pads noch nicht (oder umgekehrt) -> bis --wait Sekunden pollen.
    deadline = time.monotonic() + getattr(args, "wait", 0.0)
    hidraw_node = args.device or find_hidraw()
    pad_path = find_pad_event()
    while (not hidraw_node or not pad_path) and time.monotonic() < deadline:
        time.sleep(0.25)
        hidraw_node = hidraw_node or (args.device or find_hidraw())
        pad_path = pad_path or find_pad_event()
    if not hidraw_node:
        sys.exit("Kein hidraw-Device fuer 1D5A:1802 -- Controller verbunden?")
    if not pad_path:
        sys.exit("Kein Gamepad-Event-Device 'Nokia GC 5000' gefunden (ABS != 0).")

    print(f"hidraw:  {hidraw_node}")
    print(f"pad:     {pad_path}")
    rumble = Rumble(hidraw_node, verbose=args.verbose)
    rumble.stop()

    pad_fd = os.open(pad_path, os.O_RDWR | os.O_NONBLOCK)
    uinput_fd, event_path = create_uinput(pad_fd, verbose=args.verbose)
    if not args.no_grab:
        fcntl.ioctl(pad_fd, EVIOCGRAB, 1)
    print(f"proxy:   {event_path}  (fftest {event_path})")
    print("FF-Proxy aktiv -- Ctrl+C zum Beenden.")

    effects = {}   # effect_id -> (strong, weak, length_ms, period_ms)
    active = {}    # effect_id -> (Deadline | None, Startzeit)
    gain = 0xFFFF
    last_refresh = 0.0
    stream = Stream()
    grabbed = not args.no_grab

    def motor_target(now=None):
        """Soll-Zustand der Motoren.

        Zwei binäre Motoren = 3 Amplitudenstufen. Periodische Effekte
        quantisieren den Sinusbetrag darauf: aus / nur schwacher Motor /
        beide Motoren. Innerhalb einer Stufe laeuft der Motor durchgehend
        (Heartbeat), der Stufenwechsel folgt der Sinuskurve -- das ist die
        Wellenform, die dieses Hardware-Layout darstellen kann.
        """
        now = now if now is not None else time.monotonic()
        strong = weak = False
        for eid in active:
            eff = effects.get(eid)
            if not eff:
                continue
            if eff[3] > 0:
                duty = abs(math.sin(2.0 * math.pi * (now - active[eid][1])
                                    / (eff[3] / 1000.0)))
                if duty >= FF_LEVEL_HIGH:
                    strong, weak = True, True
                elif duty >= FF_LEVEL_LOW:
                    weak = True
                # sonst: Stille
            else:
                strong = strong or eff[0]
                weak = weak or eff[1]
        if gain == 0:
            return False, False
        return strong, weak

    def update_motors(now=None):
        strong, weak = motor_target(now)
        rumble.set(strong, weak)

    try:
        while True:
            timeout = None
            now = time.monotonic()
            for entry in active.values():
                if entry[0] is not None:
                    d = entry[0] - now
                    timeout = d if timeout is None else min(timeout, d)
            strong, weak = motor_target(now)
            if strong or weak or rumble.current != (0, 0):
                # Heartbeat, solange irgendein Motor laufen soll
                timeout = REFRESH_INTERVAL if timeout is None else min(timeout, REFRESH_INTERVAL)
            if any(effects.get(eid) and effects[eid][3] > 0 for eid in active):
                # Periodic aktiv: Sinus-Stufen regelmaessig neu bewerten
                timeout = PWM_TICK if timeout is None else min(timeout, PWM_TICK)
            if timeout is not None and timeout < 0:
                timeout = 0

            r, _, _ = select.select([pad_fd, uinput_fd], [], [], timeout)
            now = time.monotonic()
            expired = [i for i, e in active.items() if e[0] is not None and e[0] <= now]
            for i in expired:
                del active[i]
            # Stufenwechsel nachfuehren und Burst-Heartbeat aufrechterhalten
            update_motors(now)
            if rumble.current != (0, 0) and now - last_refresh >= REFRESH_INTERVAL:
                rumble.refresh()
                last_refresh = now

            if pad_fd in r:
                data = os.read(pad_fd, 65536)
                if not data:
                    raise OSError("Pad-Event-Device geschlossen (Bluetooth getrennt?)")
                write_all(uinput_fd, data)   # 1:1 an den Proxy durchreichen

            if uinput_fd in r:
                stream.feed(os.read(uinput_fd, 65536))
                while True:
                    ev = stream.next_event()
                    if ev is None:
                        break
                    etype, code, value = ev
                    if etype == EV_UINPUT:
                        # Protokoll dieses Kernels (s. uinput.h): das EV_UINPUT-
                        # Event traegt die request_id in 'value'. Effect-Daten
                        # kommen per UI_BEGIN_FF_UPLOAD-Ioctl, die Antwort geht
                        # per UI_END_FF_UPLOAD-Ioctl zurueck.
                        if code == UI_FF_UPLOAD:
                            buf = bytearray(struct.calcsize(FMT_UPLOAD))
                            struct.pack_into("<Ii", buf, 0, value & 0xFFFFFFFF, 0)
                            fcntl.ioctl(uinput_fd, UI["UI_BEGIN_FF_UPLOAD"], buf, True)
                            effect = bytes(buf[8:56])   # effect (48), old folgt bei 56
                            parsed = parse_effect(effect)
                            _etype, eid = struct.unpack_from("<Hh", effect, 0)
                            if parsed:
                                effects[eid] = parsed
                                retval = 0
                                if args.verbose:
                                    print(f"  FF upload id={eid} -> {parsed}")
                            else:
                                retval = 22  # EINVAL
                                print(f"  FF-Typ {struct.unpack_from('<H', effect)[0]:#x} "
                                      "nicht unterstuetzt")
                            struct.pack_into("<i", buf, 4, retval)
                            fcntl.ioctl(uinput_fd, UI["UI_END_FF_UPLOAD"], bytes(buf))
                        elif code == UI_FF_ERASE:
                            buf = bytearray(struct.calcsize(FMT_ERASE))
                            struct.pack_into("<I", buf, 0, value & 0xFFFFFFFF)
                            fcntl.ioctl(uinput_fd, UI["UI_BEGIN_FF_ERASE"], buf, True)
                            _req, _ret, effect_id = struct.unpack(FMT_ERASE, bytes(buf))
                            effects.pop(effect_id, None)
                            active.pop(effect_id, None)
                            struct.pack_into("<i", buf, 4, 0)
                            fcntl.ioctl(uinput_fd, UI["UI_END_FF_ERASE"], bytes(buf))
                            update_motors()
                        elif args.verbose:
                            print(f"  EV_UINPUT code={code} ignoriert")
                    elif etype == EV_FF:
                        if code == FF_GAIN:
                            gain = value & 0xFFFF
                        elif code == FF_AUTOCENTER:
                            pass
                        elif code in effects:
                            if args.verbose:
                                print(f"  FF playback id={code} value={value}")
                            if value > 0:
                                length = effects[code][2]
                                now = time.monotonic()
                                active[code] = ((now + length / 1000.0) if length > 0
                                                else None, now)
                            else:
                                active.pop(code, None)
                            update_motors(now)
                    # alle anderen Typen sind Echos der Durchleitung -> ignorieren
    except (KeyboardInterrupt, OSError) as e:
        print(f"\nBeende ({e.__class__.__name__}: {e})")
    finally:
        rumble.stop()
        try:
            if grabbed:
                fcntl.ioctl(pad_fd, EVIOCGRAB, 0)
        except OSError:
            pass
        try:
            fcntl.ioctl(uinput_fd, UI["UI_DEV_DESTROY"])
        except OSError:
            pass
        for fd in (pad_fd, uinput_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    print("Proxy beendet.")


def cmd_vibrate(args):
    node = args.device or find_hidraw()
    if not node:
        sys.exit("Kein hidraw-Device fuer 1D5A:1802 -- Controller verbunden?")
    rumble = Rumble(node)
    strong = args.motors in ("both", "strong")
    weak = args.motors in ("both", "weak")
    for _ in range(max(1, args.pulse)):
        rumble.set(strong, weak)
        end = time.monotonic() + args.duration
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(REFRESH_INTERVAL, remaining))
            rumble.refresh()
        rumble.stop()
        time.sleep(0.1)
    print(f"{args.pulse}x Motor={args.motors} fuer je {args.duration}s "
          f"(Heartbeat alle {REFRESH_INTERVAL}s).")


def main():
    ap = argparse.ArgumentParser(description="FF-Proxy fuer den Nokia GC5000")
    ap.add_argument("--device", help="hidraw-Node uebersteuern (z. B. /dev/hidraw3)")
    ap.add_argument("--no-grab", action="store_true",
                    help="echtes Pad nicht grabben (Debug: Spiele sehen beide Geraete)")
    ap.add_argument("--verbose", "-v", action="store_true", help="FF-Traffic loggen")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SEK",
                    help="beim Start bis zu N Sek. auf den Controller warten "
                         "(fuer udev-/systemd-Start)")
    sub = ap.add_subparsers(dest="cmd")
    v = sub.add_parser("vibrate", help="Nur Vibration testen (ohne Proxy)")
    v.add_argument("--motors", choices=("both", "strong", "weak"), default="both")
    v.add_argument("--duration", type=float, default=0.5, help="Sek. pro Puls")
    v.add_argument("--pulse", type=int, default=1)
    args = ap.parse_args()

    # Kernel-Konstanten aus den UAPI-Headern holen (ioctl-Nummern und FF-Codes;
    # EINVAL-Historie s. Projektlog).
    _resolve_from_header(verbose=args.verbose)
    _resolve_event_codes(verbose=args.verbose)

    if os.geteuid() != 0:
        print("Hinweis: nicht als root -- uinput/hidraw/GRAB werden scheitern.\n")

    if args.cmd == "vibrate":
        cmd_vibrate(args)
    else:
        run_proxy(args)


if __name__ == "__main__":
    main()
