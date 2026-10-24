#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gc5000_rumble_probe.py -- hidraw test script for the Nokia Game Controller 5000
=================================================================================

Goal: find the output report format for the GC5000's vibration motors
(VID 1D5A / PID 1802) by trial and error. Under Linux the controller is
handled by hid-generic and offers no force-feedback device features at
all (ff=0), but output reports can still be sent directly via hidraw
(HID_REQ_SET_REPORT over the Bluetooth L2CAP control channel).

Approach:
  1. Find all hidraw devices of the controller (also in the case of
     multiple HID interfaces, e.g. consumer control + gamepad).
  2. Parse the HID report descriptor and list all output reports with
     ID and length (--dry-run only shows that).
  3. Interactively send test patterns to every output report (zeros,
     0xFF, single bytes) and ask after each pattern whether the pad
     vibrates.
  4. Note hits as hex strings; afterwards the pattern can be repeated
     at will via --send.

Safety:
  - A zero report is sent before and after every pattern (motors off).
  - Each write has a timeout (default 3 s) so the call does not hang
     when the controller rejects a pattern.
  - The script only writes output reports that the descriptor itself
     declares. Random feature or input reports are not touched. Still,
     in theory device state may be changed (LED, pairing). When in
     doubt: controller reset = hold the Google Assistant button for
     10 seconds.

Usage (as root, otherwise PermissionError):
  sudo python3 gc5000_rumble_probe.py --dry-run      # only parse/show
  sudo python3 gc5000_rumble_probe.py                # interactive probe
  sudo python3 gc5000_rumble_probe.py --send 01:ff00ff00   # repeat a pattern
                                                      # (01 = report ID, 'u' = unnumbered)
  sudo python3 gc5000_rumble_probe.py --ids u,1,2,4,5,0x30 # try undeclared report IDs
Background: the GC5000 descriptor declares ONLY input reports
(ID 2, 4, 30) -- so rumble must go through an unnumbered or undeclared
report. Write errors (EINVAL) mean the kernel or controller rejects the
report ID/length; in that case try another ID or length.
Options: --device /dev/hidrawX, --delay, --write-timeout, --no-confirm, --lengths
"""

import argparse
import os
import signal
import sys
import time

VID = 0x1D5A
PID = 0x1802


# ----------------------------------------------------------------------------- devices
def hid_device_dir(node):
    """Determine the directory of the underlying HID device.

    /sys/class/hidraw/hidrawX is a symlink to
    .../<hid-device>/hidraw/hidrawX. The relevant attributes (uevent
    with HID_ID/HID_NAME, report_descriptor) do NOT live in the hidraw
    subdirectory but in the parent HID device directory. Depending on
    the kernel there is also a 'device' symlink pointing there.
    """
    entry = os.path.basename(node)
    real = os.path.realpath(os.path.join("/sys/class/hidraw", entry))
    candidates = []
    sub = os.path.join(real, "device")
    if os.path.exists(sub):
        candidates.append(os.path.realpath(sub))
    candidates.append(os.path.dirname(os.path.dirname(real)))  # .../hidraw/hidrawX -> up two levels
    candidates.append(real)
    for cand in candidates:
        uevent = os.path.join(cand, "uevent")
        if os.path.isfile(uevent):
            try:
                with open(uevent) as f:
                    if "HID_ID=" in f.read():
                        return cand
            except OSError:
                continue
    return None


def find_hidraw_devices():
    """Collect all hidraw nodes of the GC5000 including metadata from sysfs."""
    devs = []
    try:
        entries = sorted(os.listdir("/sys/class/hidraw"))
    except FileNotFoundError:
        sys.exit("No /sys/class/hidraw -- is there any HID on this system at all?")
    for entry in entries:
        node = "/dev/" + entry
        sysfs = hid_device_dir(node)
        if not sysfs:
            continue
        info = {"node": node, "sysfs": sysfs}
        try:
            with open(os.path.join(sysfs, "uevent")) as f:
                for line in f:
                    key, _, val = line.strip().partition("=")
                    info[key] = val
        except OSError:
            continue
        hid_id = info.get("HID_ID", "")
        parts = hid_id.split(":")
        if len(parts) != 3:
            continue
        try:
            bus, vid, pid = (int(p, 16) for p in parts)
        except ValueError:
            continue
        if vid == VID and pid == PID:
            info["bus"] = bus
            devs.append(info)
    return devs


def list_all_hidraw_devices():
    """Diagnostics: print all existing hidraw devices with name and ID."""
    lines = []
    try:
        entries = sorted(os.listdir("/sys/class/hidraw"))
    except FileNotFoundError:
        return lines
    for entry in entries:
        node = "/dev/" + entry
        sysfs = hid_device_dir(node)
        name, hid_id = "?", "?"
        if sysfs:
            try:
                with open(os.path.join(sysfs, "uevent")) as f:
                    for line in f:
                        key, _, val = line.strip().partition("=")
                        if key == "HID_NAME":
                            name = val
                        elif key == "HID_ID":
                            hid_id = val
            except OSError:
                pass
        lines.append(f"  {node:16} {name:40} HID_ID={hid_id}")
    return lines


def read_report_descriptor(info):
    with open(os.path.join(info["sysfs"], "report_descriptor"), "rb") as f:
        return f.read()


# ------------------------------------------------------------------ HID descriptor parser
def parse_descriptor(data):
    """Minimal parser: returns {report_id|None: {'input':bits,'output':bits,'feature':bits}}."""
    reports = {}

    def slot(rid):
        if rid not in reports:
            reports[rid] = {"input": 0, "output": 0, "feature": 0}
        return reports[rid]

    glob = {"report_size": 0, "report_count": 0, "usage_page": 0}
    stack = []
    cur_id = None
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        i += 1
        if b == 0xFE:  # long item: skip 1 length byte + data
            if i < n:
                i += 1 + data[i]
            continue
        size = b & 0x03
        if size == 3:
            size = 4
        tag = b >> 4
        typ = (b >> 2) & 0x03
        if i + size > n:
            break  # broken descriptor
        payload = data[i:i + size]
        i += size
        val = int.from_bytes(payload, "little") if payload else 0
        if typ == 1:  # global
            if tag == 0x8:                      # Report ID
                cur_id = val
                slot(cur_id)
            elif tag == 0x7:                    # Report Size
                glob["report_size"] = val
            elif tag == 0x9:                    # Report Count
                glob["report_count"] = val
            elif tag == 0xA:                    # Push
                stack.append((dict(glob), cur_id))
            elif tag == 0xB and stack:          # Pop
                glob, cur_id = stack.pop()
        elif typ == 0:  # main
            if tag in (0x8, 0x9, 0xB):          # Input / Output / Feature
                kind = {0x8: "input", 0x9: "output", 0xB: "feature"}[tag]
                bits = glob["report_count"] * glob["report_size"]
                slot(cur_id)[kind] += bits
    return reports


def output_report_len(bits):
    return (bits + 7) // 8


# ------------------------------------------------------------------------------- writing
class WriteTimeout(Exception):
    pass


def write_report(node, report_id, payload, timeout_s):
    """Send an output report (buf[0] = report ID, 0x00 when unnumbered)."""
    buf = bytes([(report_id if report_id is not None else 0)]) + bytes(payload)

    def _alarm(signum, frame):
        raise WriteTimeout()

    old = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        fd = os.open(node, os.O_WRONLY)
        try:
            os.write(fd, buf)
        finally:
            os.close(fd)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def send(node, report_id, payload, timeout_s, label=""):
    try:
        write_report(node, report_id, payload, timeout_s)
        return True
    except WriteTimeout:
        print(f"    ! Write timeout ({label}) -- the controller does not respond to this pattern.")
    except PermissionError:
        sys.exit("Permission denied -- run the script as root (sudo).")
    except OSError as e:
        print(f"    ! Write error ({label}): {e}")
    return False


# ---------------------------------------------------------------------------------- probe
def describe(report_id):
    return f"Report ID {report_id}" if report_id is not None else "unnumbered report"


def hex_str(seq):
    return " ".join(f"{b:02x}" for b in seq)


def probe_reports(dev, reports, args):
    node = dev["node"]

    if args.ids:
        # Try explicit report IDs (including ones the descriptor does not
        # declare -- on the GC5000 that is precisely the most interesting
        # case, since the descriptor contains no output reports).
        out_reports = []
        for token in args.ids.split(","):
            token = token.strip()
            if not token:
                continue
            rid = None if token.lower() == "u" else int(token, 0)  # decimal or 0x.. hex
            for length in args.lengths:
                out_reports.append((rid, {"input": 0, "output": length * 8, "feature": 0}))
    else:
        out_reports = [
            (rid, info) for rid, info in reports.items() if info["output"] > 0
        ]
    if not out_reports:
        # Descriptor not evaluable -> fallback: try unnumbered reports with
        # common lengths (HID only accepts exact lengths).
        print("  No output reports could be derived from the descriptor --")
        print("  fallback: unnumbered report with typical lengths.")
        out_reports = [(None, {"input": 0, "output": bits * 8, "feature": 0})
                       for bits in (2, 3, 4, 5, 6, 8, 10, 16, 20)]

    for rid, info in out_reports:
        n = output_report_len(info["output"])
        print(f"\n=== {describe(rid)}, {n} byte payload ===")
        zeros = [0x00] * n
        full = [0xFF] * n
        patterns = [("all 0x00 (off/baseline)", zeros), ("all 0xFF", full)]
        patterns += [(f"byte {i} = 0xFF, rest 0x00", zeros[:i] + [0xFF] + zeros[i + 1:])
                     for i in range(n)]
        hits = []
        for label, pat in patterns:
            print(f"\n  Sending: {label}  ->  hex {hex_str(pat)}")
            if not args.no_confirm:
                ans = input("  [Enter] send, [s] skip, [q] quit: ").strip().lower()
                if ans == "q":
                    return
                if ans == "s":
                    continue
            for _ in range(max(1, args.pulse)):
                if not send(node, rid, pat, args.write_timeout, label):
                    break
                time.sleep(args.delay)
            send(node, rid, zeros, args.write_timeout, "Stop (0x00)")  # always off afterwards
            fb = input("  Vibrating? [y]es / [n]o / [q] quit: ").strip().lower()
            if fb == "q":
                return
            if fb == "y":
                hits.append((label, hex_str(pat)))
                print(f"  >>> HIT! Note down: --send {'u' if rid is None else f'{rid:02x}'}:{bytes(pat).hex()}")
        if hits:
            print(f"\n  Hits for {describe(rid)}:")
            for label, hexs in hits:
                print(f"    {label}: {hexs}")


def cleanup(devs, reports_by_node, args):
    for dev in devs:
        reports = reports_by_node.get(dev["node"], {})
        for rid, info in reports.items():
            if info["output"] > 0:
                send(dev["node"], rid, [0x00] * output_report_len(info["output"]),
                     args.write_timeout, "Cleanup 0x00")


# ------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="hidraw rumble probe for the Nokia GC5000")
    ap.add_argument("--device", help="specify a hidraw node directly (e.g. /dev/hidraw3)")
    ap.add_argument("--dry-run", action="store_true", help="only parse and show, send nothing")
    ap.add_argument("--send", metavar="ID:HEXBYTES",
                    help="send one pattern directly, e.g. --send 01:ff:00ff (ID 'u' = unnumbered)")
    ap.add_argument("--ids", metavar="ID[,ID...]",
                    help="try explicit report IDs (decimal or 0x hex), "
                         "'u' = unnumbered; e.g. --ids u,1,2,4,0x30")
    ap.add_argument("--lengths", metavar="N[,N...]",
                    help="payload lengths (bytes) for --ids, default: 2,4,8,16,20")
    ap.add_argument("--delay", type=float, default=0.25, help="pause between pulses (s)")
    ap.add_argument("--pulse", type=int, default=3, help="repetitions per pattern")
    ap.add_argument("--write-timeout", type=float, default=3.0, help="timeout per write (s)")
    ap.add_argument("--no-confirm", action="store_true", help="do not ask before each send")
    args = ap.parse_args()
    if args.lengths:
        args.lengths = [int(x.strip(), 0) for x in args.lengths.split(",") if x.strip()]
    else:
        args.lengths = [2, 4, 8, 16, 20]

    if os.geteuid() != 0:
        print("Note: not running as root -- writes to /dev/hidraw* will likely fail.\n")

    devs = find_hidraw_devices()
    if args.device:
        devs = [{"node": args.device, "sysfs": "", "HID_NAME": "specified externally"}]
        reports = {}
    if not devs:
        print("No hidraw device for 1D5A:1802 found -- is the controller paired/connected?")
        print("Check first:  grep -A4 -i nokia /proc/bus/input/devices")
        print("Existing hidraw devices:")
        for line in list_all_hidraw_devices():
            print(line)
        sys.exit(1)

    reports_by_node = {}
    for dev in devs:
        name = dev.get("HID_NAME", "?")
        print(f"\n### {dev['node']}  ({name})")
        if dev.get("sysfs"):
            try:
                reports = parse_descriptor(read_report_descriptor(dev))
            except OSError as e:
                print(f"  Could not read the descriptor: {e}")
                reports = {}
            reports_by_node[dev["node"]] = reports
            print("  Report overview (bits):")
            for rid in sorted(reports, key=lambda r: (r is None, r)):
                info = reports[rid]
                print(f"    {describe(rid):22} input={info['input']:4}  output={info['output']:4}  "
                      f"feature={info['feature']:4}  -> output payload {output_report_len(info['output'])} bytes")
        if not args.dry_run and not args.send:
            probe_reports(dev, reports_by_node.get(dev["node"], {}), args)

    # Direct-send mode
    if args.send:
        rid_s, _, hexs = args.send.partition(":")
        try:
            payload = bytes.fromhex(hexs.replace(" ", "").replace(":", ""))
            rid = None if rid_s.lower() == "u" else int(rid_s, 16)
        except ValueError:
            sys.exit("--send expects ID:HEXBYTES, e.g. 01:ff00 or u:ff")
        if not devs:
            sys.exit("No target device.")
        node = devs[0]["node"]
        print(f"Sending to {node}: {describe(rid)} <- {payload.hex(' ')}")
        for _ in range(max(1, args.pulse)):
            send(node, rid, payload, args.write_timeout, "direct")
            time.sleep(args.delay)
        send(node, rid, [0x00] * len(payload), args.write_timeout, "Stop")

    if not args.dry_run and not args.send:
        cleanup(devs, reports_by_node, args)
        print("\nDone. On hits: repeat the pattern with --send ID:HEX and report back.")


if __name__ == "__main__":
    main()
