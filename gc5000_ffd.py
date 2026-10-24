#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gc5000_ffd.py -- Force-feedback proxy daemon for the Nokia Game Controller 5000
=================================================================================

Background (result of the hidraw reverse-engineering, October 2026):
  Report ID 0x01, 2-byte payload, binary motor control:
    Byte 0 = strong motor  (0x00 = off, anything else = on)
    Byte 1 = weak motor     (0x00 = off, anything else = on)
  The stop report 0x01:0000 ends the vibration immediately.
  (The amplitude is binary: 0x01 and 0xFF feel exactly the same.)

The daemon:
  1. Finds the controller's hidraw device and gamepad event device.
  2. Creates a uinput device with identical buttons/axes plus
     FF capabilities (FF_RUMBLE, FF_PERIODIC, FF_CONSTANT, FF_GAIN).
  3. Grabs the real gamepad (EVIOCGRAB) so games only see the proxy.
     All input events are passed through 1:1.
  4. Translates the games' FF uploads/playback into hidraw reports.

Validation:
    Terminal 1:  sudo python3 gc5000_ffd.py
    Terminal 2:  fftest /dev/input/<proxy event node>
                 -> effects 0 (sine), 4 (strong), 5 (weak) play.
                 The pad must vibrate; pressing 'q' to stop sends 0x00.

Notes:
  - Close running games/Steam before starting, or restart Steam
    afterwards so the proxy gets picked up.
  - FF_GAIN is interpreted in a binary fashion (gain==0 -> motors off).
  - FF protocol: EV_UINPUT event (request_id in 'value') plus the
    UI_BEGIN_FF_UPLOAD/UI_END_FF_UPLOAD resp. _FF_ERASE ioctls, the
    structs are 104/12 bytes (see the kernel's uinput.h). ioctl
    numbers, FF codes and EV_UINPUT are read from the UAPI headers at
    runtime.

As a systemd service, started as soon as the controller shows up via
Bluetooth (udev rule on hidraw add of 1D5A:1802):

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
  # No [Install] section: the service is triggered exclusively by the
  # udev rule. A Bluetooth disconnect ends it with exit code 0
  # (no restart loop while the pad is away).
  ------------------------------------------------------------

  /etc/udev/rules.d/90-gc5000.rules
  ------------------------------------------------------------
  # Note: Bluetooth HID devices (HID bus 0005) have NO idVendor/idProduct
  # attributes (those are USB-specific) -> match on the parent name:
  ACTION=="add", SUBSYSTEM=="hidraw", KERNELS=="0005:1D5A:1802.*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="gc5000-ffd.service"
  ------------------------------------------------------------

Setup (one-time):
  sudo install -m 755 gc5000_ffd.py /usr/local/bin/gc5000_ffd.py
  sudo systemctl daemon-reload
  sudo udevadm control --reload
  # Switch the controller off and reconnect it -> the service starts automatically:
  systemctl status gc5000-ffd

Dependencies: Python stdlib only. Must run as root (uinput + hidraw + grab).
"""

import argparse
import errno
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

# ------------------------------------------------------------ kernel constants
EV_SYN, EV_KEY, EV_ABS, EV_MSC = 0x00, 0x01, 0x03, 0x04
EV_FF = 0x15
EV_UINPUT = 0x0410
# Fallback values (mainline state); overridden by _resolve_event_codes()
# from the UAPI headers -- the kernel this was developed on has
# renumbered the FF codes (FF_RUMBLE is NOT at 0x50 there).
FF_RUMBLE, FF_PERIODIC, FF_CONSTANT = 0x50, 0x51, 0x52
FF_SQUARE, FF_TRIANGLE, FF_SINE = 0x58, 0x59, 0x5a
FF_GAIN, FF_AUTOCENTER = 0x60, 0x61
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2


def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | (t << 8) | nr


# Fallback values in case /usr/include/linux/uinput.h is not readable.
# The actual values are fetched from the header at runtime:
# uinput answers unknown ioctl numbers with EINVAL, so we do NOT
# guess here anymore.
UI = {
    "UI_SET_EVBIT":  _IOC(1, 0x55, 100, 4),
    "UI_SET_KEYBIT": _IOC(1, 0x55, 101, 4),
    "UI_SET_ABSBIT": _IOC(1, 0x55, 103, 4),
    "UI_SET_MSCBIT": _IOC(1, 0x55, 104, 4),
    "UI_SET_FFBIT":  _IOC(1, 0x55, 107, 4),
    "UI_DEV_CREATE": (0x55 << 8) | 1,
    "UI_DEV_DESTROY": (0x55 << 8) | 2,
    # FF handshake protocol (see the kernel's uinput.h): EV_UINPUT event
    # with the request_id in 'value', then BEGIN/END ioctls with this struct.
    "UI_BEGIN_FF_UPLOAD": _IOC(3, 0x55, 200, 104),
    "UI_END_FF_UPLOAD":   _IOC(1, 0x55, 201, 104),
    "UI_BEGIN_FF_ERASE":  _IOC(3, 0x55, 202, 12),
    "UI_END_FF_ERASE":    _IOC(1, 0x55, 203, 12),
}
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
EV_UINPUT = 0x0410
EVIOCGRAB = _IOC(1, 0x45, 0x90, 4)


def _resolve_from_header(verbose=False):
    """Parse UI_* constants from /usr/include/linux/uinput.h."""
    global UI_FF_UPLOAD, UI_FF_ERASE, EV_UINPUT
    import re
    try:
        with open("/usr/include/linux/uinput.h") as f:
            text = f.read()
    except OSError as e:
        print(f"Warning: cannot read uinput.h ({e}), using fallback constants.")
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
            size = 0                      # _IO has no data element: size 0!
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
    """Fetch EV_FF and FF code values from the UAPI headers.

    Newer kernels have renumbered the FF codes; the values must not be
    hardcoded anymore. Searches input-event-codes.h and input.h, in
    both #define and enum style ('FF_RUMBLE = 42').
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
        print("Warning: cannot read input-event-codes.h -- keeping FF code fallbacks.")
        return
    for name in names:
        m = (re.search(r"#define\s+%s\s+[\(]?\s*(0[xX][0-9a-fA-F]+|\d+)" % name, text)
             or re.search(r"\b%s\s*=\s*(0[xX][0-9a-fA-F]+|\d+)\b" % name, text))
        if m:
            globals()[name] = int(m.group(1), 0)
        else:
            print(f"Warning: {name} not found in header, keeping fallback "
                  f"{globals()[name]:#x}.")
    if verbose:
        print("  FF codes: " + ", ".join(f"{n}={globals()[n]:#x}" for n in names))


def EVIOCGBIT(ev, size):
    return _IOC(2, 0x45, 0x20 + ev, size)


def EVIOCGABS(code):
    return _IOC(2, 0x45, 0x40 + code, 24)


# Struct sizes for x86_64 / current glibc
FMT_EVENT     = "<qqHHi"      # struct input_event      = 24
FMT_UPLOAD    = "<Ii48s48s"   # struct uinput_ff_upload = 104 (request_id, retval, effect, old)
FMT_ERASE     = "<IiI"        # struct uinput_ff_erase   = 12 (request_id, retval, effect_id)
FMT_SETUP     = "<HHHHI80s"  # struct uinput_setup      = 92
FMT_ABS_SETUP = "<H2x6i"     # struct uinput_abs_setup = 28
FMT_ABSINFO   = "<6i"         # struct input_absinfo    = 24


# --------------------------------------------------------------------- hidraw
def hid_device_dir(node):
    """Find the HID device directory behind a hidraw node (see the probe script)."""
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


REFRESH_INTERVAL = 0.07  # s: heartbeat (short == dense burst overlaps == smooth continuous tone)
PWM_TICK = 0.01        # s: evaluation tick of the periodic amplitude (100 Hz)
FF_LEVEL_LOW = 0.35    # sine magnitude below this: silence
FF_LEVEL_HIGH = 0.7    # below this: weak motor only (soft level); above: both


class Rumble:
    """Binary motor control via hidraw (report ID 0x01, 2 bytes).

    The GC5000 firmware stops vibrating after a short while unless the
    host keeps sending: every report with a value != 0x00 only triggers
    a short burst. set() writes on state changes, refresh() repeats
    the current report (heartbeat) while a motor is active.
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
        """Heartbeat: re-send the current report (burst firmware)."""
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
            print(f"hidraw error ({e}), will reopen on next attempt", file=sys.stderr)
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None


# --------------------------------------------------------- find the gamepad device
def find_pad_event():
    """Read the gamepad event device (not the consumer control) from /proc."""
    try:
        with open("/proc/bus/input/devices") as f:
            content = f.read()
    except OSError as e:
        sys.exit(f"Cannot read /proc/bus/input/devices: {e}")
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
    """Create a virtual gamepad with identical axes/buttons plus FF.

    Deliberately legacy API: name/ID/ff_effects_max/ABS values are
    written as struct uinput_user_dev (stable since kernel 2.6)
    instead of using UI_DEV_SETUP/UI_ABS_SETUP.
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

    # Offer force feedback (report numbers from the kernel header!)
    fcntl.ioctl(fd, UI["UI_SET_EVBIT"], EV_FF)
    # Periodic waveforms are separate FF bits in this kernel and are
    # validated separately during a periodic upload -> set them as well.
    for ffb in (FF_RUMBLE, FF_PERIODIC, FF_CONSTANT, FF_GAIN,
                FF_SQUARE, FF_TRIANGLE, FF_SINE):
        fcntl.ioctl(fd, UI["UI_SET_FFBIT"], ffb)

    # Set ABS bits and adopt absinfo from the real pad
    absbits = bitfield(ioctl_read(pad_fd, EVIOCGBIT(EV_ABS, 8), 8)) if EV_ABS in evbits else []
    absmax, absmin, absfuzz, absflat = ([0] * ABS_CNT for _ in range(4))
    for c in absbits:
        fcntl.ioctl(fd, UI["UI_SET_ABSBIT"], c)
        info = struct.unpack(FMT_ABSINFO, ioctl_read(pad_fd, EVIOCGABS(c), 24))
        _value, absmin[c], absmax[c], absfuzz[c], absflat[c], _res = info

    # Legacy: write struct uinput_user_dev (1116 bytes), then DEV_CREATE
    user_dev = (struct.pack("<80sHHHHI", PROXY_NAME, 0x05, VID, PID, 0x0100, 32)
                + struct.pack("<64i", *absmax)
                + struct.pack("<64i", *absmin)
                + struct.pack("<64i", *absfuzz)
                + struct.pack("<64i", *absflat))
    write_all(fd, user_dev)
    fcntl.ioctl(fd, UI["UI_DEV_CREATE"])

    # Find the event node via a sysfs name scan (no UI_GET_SYSNAME needed)
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
    """Buffer incoming bytes and split them into input_events / FF structs."""

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
    """Extract (strong, weak, length_ms, period_ms) from a 48-byte ff_effect."""
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
    return None  # not supported


def run_proxy(args):
    # udev start: the hidraw node may already exist while the pad's
    # event node does not yet (or vice versa) -> poll for --wait seconds.
    deadline = time.monotonic() + getattr(args, "wait", 0.0)
    hidraw_node = args.device or find_hidraw()
    pad_path = find_pad_event()
    while (not hidraw_node or not pad_path) and time.monotonic() < deadline:
        time.sleep(0.25)
        hidraw_node = hidraw_node or (args.device or find_hidraw())
        pad_path = pad_path or find_pad_event()
    if not hidraw_node:
        sys.exit("No hidraw device for 1D5A:1802 -- is the controller connected?")
    if not pad_path:
        sys.exit("No gamepad event device 'Nokia GC 5000' found (ABS != 0).")

    print(f"hidraw:  {hidraw_node}")
    print(f"pad:     {pad_path}")
    rumble = Rumble(hidraw_node, verbose=args.verbose)
    rumble.stop()

    pad_fd = os.open(pad_path, os.O_RDWR | os.O_NONBLOCK)
    uinput_fd, event_path = create_uinput(pad_fd, verbose=args.verbose)
    if not args.no_grab:
        try:
            fcntl.ioctl(pad_fd, EVIOCGRAB, 1)
        except OSError as e:
            if e.errno == errno.EBUSY:
                sys.exit("The pad is already grabbed -- is another daemon\n"
                         " instance running?\n"
                         "  systemctl status gc5000-ffd\n"
                         "  sudo systemctl stop gc5000-ffd  # then start again\n"
                         "(Hint: the systemd service restarts automatically via\n"
                         " the udev rule on every controller reconnect.)")
            raise
    print(f"proxy:   {event_path}  (fftest {event_path})")
    print("FF proxy active -- Ctrl+C to quit.")

    effects = {}   # effect_id -> (strong, weak, length_ms, period_ms)
    active = {}    # effect_id -> (deadline | None, start time)
    gain = 0xFFFF
    last_refresh = 0.0
    stream = Stream()
    grabbed = not args.no_grab

    def motor_target(now=None):
        """Target motor state.

        Two binary motors = 3 amplitude levels. Periodic effects
        quantize the sine magnitude onto these: off / weak motor only /
        both motors. Within a level the motor runs continuously
        (heartbeat); the level changes follow the sine curve -- that is
        the waveform this hardware layout can express.
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
                # otherwise: silence
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
                # Heartbeat as long as any motor should run
                timeout = REFRESH_INTERVAL if timeout is None else min(timeout, REFRESH_INTERVAL)
            if any(effects.get(eid) and effects[eid][3] > 0 for eid in active):
                # Periodic active: re-evaluate the sine levels regularly
                timeout = PWM_TICK if timeout is None else min(timeout, PWM_TICK)
            if timeout is not None and timeout < 0:
                timeout = 0

            r, _, _ = select.select([pad_fd, uinput_fd], [], [], timeout)
            now = time.monotonic()
            expired = [i for i, e in active.items() if e[0] is not None and e[0] <= now]
            for i in expired:
                del active[i]
            # Follow level changes and keep the burst heartbeat alive
            update_motors(now)
            if rumble.current != (0, 0) and now - last_refresh >= REFRESH_INTERVAL:
                rumble.refresh()
                last_refresh = now

            if pad_fd in r:
                data = os.read(pad_fd, 65536)
                if not data:
                    raise OSError("Pad event device closed (Bluetooth disconnected?)")
                write_all(uinput_fd, data)   # pass through to the proxy 1:1

            if uinput_fd in r:
                stream.feed(os.read(uinput_fd, 65536))
                while True:
                    ev = stream.next_event()
                    if ev is None:
                        break
                    etype, code, value = ev
                    if etype == EV_UINPUT:
                        # This kernel's protocol (see uinput.h): the
                        # EV_UINPUT event carries the request_id in
                        # 'value'. Effect data arrives via the
                        # UI_BEGIN_FF_UPLOAD ioctl; the reply goes back
                        # via the UI_END_FF_UPLOAD ioctl.
                        if code == UI_FF_UPLOAD:
                            buf = bytearray(struct.calcsize(FMT_UPLOAD))
                            struct.pack_into("<Ii", buf, 0, value & 0xFFFFFFFF, 0)
                            fcntl.ioctl(uinput_fd, UI["UI_BEGIN_FF_UPLOAD"], buf, True)
                            effect = bytes(buf[8:56])   # effect (48), old follows at offset 56
                            parsed = parse_effect(effect)
                            _etype, eid = struct.unpack_from("<Hh", effect, 0)
                            if parsed:
                                effects[eid] = parsed
                                retval = 0
                                if args.verbose:
                                    print(f"  FF upload id={eid} -> {parsed}")
                            else:
                                retval = 22  # EINVAL
                                print(f"  FF type {struct.unpack_from('<H', effect)[0]:#x} "
                                      "not supported")
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
                            print(f"  EV_UINPUT code={code} ignored")
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
                    # all other types are echoes of the pass-through -> ignore
    except (KeyboardInterrupt, OSError) as e:
        print(f"\nShutting down ({e.__class__.__name__}: {e})")
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
    print("Proxy stopped.")


def cmd_vibrate(args):
    node = args.device or find_hidraw()
    if not node:
        sys.exit("No hidraw device for 1D5A:1802 -- is the controller connected?")
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
    print(f"{args.pulse}x motor={args.motors} for {args.duration}s each "
          f"(heartbeat every {REFRESH_INTERVAL}s).")


def main():
    ap = argparse.ArgumentParser(description="FF proxy for the Nokia GC5000")
    ap.add_argument("--device", help="override the hidraw node (e.g. /dev/hidraw3)")
    ap.add_argument("--no-grab", action="store_true",
                    help="do not grab the real pad (debug: games see both devices)")
    ap.add_argument("--verbose", "-v", action="store_true", help="log FF traffic")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SEC",
                    help="wait up to N seconds for the controller at startup "
                         "(for udev/systemd start)")
    sub = ap.add_subparsers(dest="cmd")
    v = ap.add_parser("vibrate", help="just test vibration (without proxy)")
    v.add_argument("--motors", choices=("both", "strong", "weak"), default="both")
    v.add_argument("--duration", type=float, default=0.5, help="seconds per pulse")
    v.add_argument("--pulse", type=int, default=1)
    args = ap.parse_args()

    # Fetch kernel constants from the UAPI headers (ioctl numbers and FF
    # codes; see the project log for the EINVAL history).
    _resolve_from_header(verbose=args.verbose)
    _resolve_event_codes(verbose=args.verbose)

    if os.geteuid() != 0:
        print("Note: not running as root -- uinput/hidraw/GRAB will fail.\n")

    if args.cmd == "vibrate":
        cmd_vibrate(args)
    else:
        run_proxy(args)


if __name__ == "__main__":
    main()
