#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gc5000_rumble_probe.py -- Hidraw-Testskript fuer den Nokia Game Controller 5000
=================================================================================

Ziel: Das Output-Report-Format fuer die Vibrationsmotoren des GC5000
(VID 1D5A / PID 1802) per Try-and-Error finden. Der Controller wird unter
Linux von hid-generic bedient und bietet keinerlei Force-Feedback-Geraetemerkmale
(ff=0), aber Output-Reports lassen sich trotzdem direkt per hidraw senden
(HID_REQ_SET_REPORT ueber den Bluetooth-L2CAP-Control-Channel).

Vorgehen:
  1. Alle hidraw-Devices des Controllers finden (auch bei mehreren
     HID-Interfaces, z. B. Consumer-Control + Gamepad).
  2. Den HID-Report-Descriptor parsen und alle Output-Reports mit ID und
     Laenge auflisten (--dry-run zeigt nur das).
  3. Interaktiv Testmuster an jedes Output-Report senden (Nullen, 0xFF,
     Einzelbytes) und nach jedem Muster fragen, ob das Pad vibriert.
  4. Treffer als hex-String notieren; danach kann man das Muster per
     --send beliebig wiederholen.

Sicherheit:
  - Vor und nach jedem Muster wird ein Null-Report gesendet (Motoren aus).
  - Pro Schreibzugriff gilt ein Timeout (Standard 3 s), damit der Aufruf
    nicht haengt, wenn der Controller ein Muster ablehnt.
  - Das Skript schreibt nur Output-Reports, die der Descriptor selbst
    deklariert. Zufaellige Feature- oder Input-Reports werden nicht
    angefasst. Dennoch: Theoretisch koennen Geraetezustaende geaendert
    werden (LED, Pairing). Im Zweifel: Controller-Reset = Google-Assistant-
    Taste 10 s halten.

Nutzung (als root, sonst PermissionError):
  sudo python3 gc5000_rumble_probe.py --dry-run      # nur parsen/anzeigen
  sudo python3 gc5000_rumble_probe.py                # interaktive Probe
  sudo python3 gc5000_rumble_probe.py --send 01:ff00ff00   # Muster wiederholen
                                                      # (01 = Report-ID, 'u' = unnummeriert)
  sudo python3 gc5000_rumble_probe.py --ids u,1,2,4,5,0x30 # undeklarierte Report-IDs probieren
Hintergrund: Der GC5000-Descriptor deklariert NUR Input-Reports
(ID 2, 4, 30) -- Rumble muss also ueber einen unnummerierten oder
nicht deklarierten Report laufen. Schreibfehler (EINVAL) bedeuten,
dass Kernel oder Controller die Report-ID/Laenge ablehnen; dann
eine andere ID bzw. Laenge probieren.
Optionen: --device /dev/hidrawX, --delay, --write-timeout, --no-confirm, --lengths
"""

import argparse
import os
import signal
import sys
import time

VID = 0x1D5A
PID = 0x1802


# ----------------------------------------------------------------------------- Devices
def hid_device_dir(node):
    """Verzeichnis des zugrundeliegenden HID-Devices ermitteln.

    /sys/class/hidraw/hidrawX ist ein Symlink auf
    .../<hid-device>/hidraw/hidrawX. Die relevanten Attribute (uevent mit
    HID_ID/HID_NAME, report_descriptor) liegen NICHT im hidraw-Unterverzeichnis,
    sondern im uebergeordneten HID-Device-Verzeichnis. Je nach Kernel gibt es
    zusaetzlich einen 'device'-Symlink dorthin.
    """
    entry = os.path.basename(node)
    real = os.path.realpath(os.path.join("/sys/class/hidraw", entry))
    candidates = []
    sub = os.path.join(real, "device")
    if os.path.exists(sub):
        candidates.append(os.path.realpath(sub))
    candidates.append(os.path.dirname(os.path.dirname(real)))  # .../hidraw/hidrawX -> zwei hoch
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
    """Alle hidraw-Nodes des GC5000 inkl. Metadaten aus sysfs einsammeln."""
    devs = []
    try:
        entries = sorted(os.listdir("/sys/class/hidraw"))
    except FileNotFoundError:
        sys.exit("Kein /sys/class/hidraw -- laeuft hier ueberhaupt HID?")
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
    """Diagnose: alle vorhandenen hidraw-Geraete mit Name und ID ausgeben."""
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


# ------------------------------------------------------------------ HID-Descriptor-Parser
def parse_descriptor(data):
    """Minimal-Parser: liefert {report_id|None: {'input':bits,'output':bits,'feature':bits}}."""
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
        if b == 0xFE:  # long item: 1 Laengenbyte + Daten ueberspringen
            if i < n:
                i += 1 + data[i]
            continue
        size = b & 0x03
        if size == 3:
            size = 4
        tag = b >> 4
        typ = (b >> 2) & 0x03
        if i + size > n:
            break  # kaputter Descriptor
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


# ------------------------------------------------------------------------------- Schreiben
class WriteTimeout(Exception):
    pass


def write_report(node, report_id, payload, timeout_s):
    """Output-Report senden (buf[0] = Report-ID, bei unnummeriert 0x00)."""
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
        print(f"    ! Timeout beim Schreiben ({label}) -- Controller antwortet nicht auf dieses Muster.")
    except PermissionError:
        sys.exit("Permission denied -- Skript als root starten (sudo).")
    except OSError as e:
        print(f"    ! Schreibfehler ({label}): {e}")
    return False


# ---------------------------------------------------------------------------------- Probe
def describe(report_id):
    return f"Report-ID {report_id}" if report_id is not None else "unnummeriertes Report"


def hex_str(seq):
    return " ".join(f"{b:02x}" for b in seq)


def probe_reports(dev, reports, args):
    node = dev["node"]

    if args.ids:
        # Explizite Report-IDs durchprobieren (auch solche, die der Descriptor
        # nicht deklariert -- beim GC5000 ist genau das der interessanteste Fall,
        # da der Descriptor keine Output-Reports enthaelt).
        out_reports = []
        for token in args.ids.split(","):
            token = token.strip()
            if not token:
                continue
            rid = None if token.lower() == "u" else int(token, 0)  # dezimal oder 0x..-Hex
            for length in args.lengths:
                out_reports.append((rid, {"input": 0, "output": length * 8, "feature": 0}))
    else:
        out_reports = [
            (rid, info) for rid, info in reports.items() if info["output"] > 0
        ]
    if not out_reports:
        # Descriptor nicht auswertbar -> Fallback: unnummeriertes Report mit
        # gaengigen Laengen durchprobieren (HID akzeptiert nur exakte Laengen).
        print("  Keine Output-Reports aus dem Descriptor ermittelbar --")
        print("  Fallback: unnummeriertes Report mit typischen Laengen.")
        out_reports = [(None, {"input": 0, "output": bits * 8, "feature": 0})
                       for bits in (2, 3, 4, 5, 6, 8, 10, 16, 20)]

    for rid, info in out_reports:
        n = output_report_len(info["output"])
        print(f"\n=== {describe(rid)}, {n} Byte Payload ===")
        zeros = [0x00] * n
        full = [0xFF] * n
        patterns = [("alles 0x00 (Aus/Baseline)", zeros), ("alles 0xFF", full)]
        patterns += [(f"Byte {i} = 0xFF, Rest 0x00", zeros[:i] + [0xFF] + zeros[i + 1:])
                     for i in range(n)]
        hits = []
        for label, pat in patterns:
            print(f"\n  Sende: {label}  ->  hex {hex_str(pat)}")
            if not args.no_confirm:
                ans = input("  [Enter] senden, [s] ueberspringen, [q] Abbruch: ").strip().lower()
                if ans == "q":
                    return
                if ans == "s":
                    continue
            for _ in range(max(1, args.pulse)):
                if not send(node, rid, pat, args.write_timeout, label):
                    break
                time.sleep(args.delay)
            send(node, rid, zeros, args.write_timeout, "Stop (0x00)")  # immer aus danach
            fb = input("  Vibriert? [j]a / [n]ein / [q] Abbruch: ").strip().lower()
            if fb == "q":
                return
            if fb == "j":
                hits.append((label, hex_str(pat)))
                print(f"  >>> TREFFER! Notieren: --send {'u' if rid is None else f'{rid:02x}'}:{bytes(pat).hex()}")
        if hits:
            print(f"\n  Treffer fuer {describe(rid)}:")
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
    ap = argparse.ArgumentParser(description="Hidraw-Rumble-Probe fuer den Nokia GC5000")
    ap.add_argument("--device", help="Direkt ein hidraw-Node angeben (z. B. /dev/hidraw3)")
    ap.add_argument("--dry-run", action="store_true", help="Nur parsen und anzeigen, nichts senden")
    ap.add_argument("--send", metavar="ID:HEXBYTES",
                    help="Ein Muster direkt senden, z. B. --send 01:ff:00ff (ID 'u' = unnummeriert)")
    ap.add_argument("--ids", metavar="ID[,ID...]",
                    help="Explizite Report-IDs durchprobieren (Dezimal oder 0x-Hex), "
                         "'u' = unnummeriert; z. B. --ids u,1,2,4,0x30")
    ap.add_argument("--lengths", metavar="N[,N...]",
                    help="Payload-Laengen (Byte) fuer --ids, Standard: 2,4,8,16,20")
    ap.add_argument("--delay", type=float, default=0.25, help="Pause zwischen Pulsen (s)")
    ap.add_argument("--pulse", type=int, default=3, help="Wiederholungen pro Muster")
    ap.add_argument("--write-timeout", type=float, default=3.0, help="Timeout pro Schreibzugriff (s)")
    ap.add_argument("--no-confirm", action="store_true", help="Vor jedem Senden nicht nachfragen")
    args = ap.parse_args()
    if args.lengths:
        args.lengths = [int(x.strip(), 0) for x in args.lengths.split(",") if x.strip()]
    else:
        args.lengths = [2, 4, 8, 16, 20]

    if os.geteuid() != 0:
        print("Hinweis: nicht als root -- Schreibzugriffe auf /dev/hidraw* duerften scheitern.\n")

    devs = find_hidraw_devices()
    if args.device:
        devs = [{"node": args.device, "sysfs": "", "HID_NAME": "extern angegeben"}]
        reports = {}
    if not devs:
        print("Kein hidraw-Device fuer 1D5A:1802 gefunden -- ist der Controller gekoppelt/verbunden?")
        print("Vorher checken:  grep -A4 -i nokia /proc/bus/input/devices")
        print("Vorhandene hidraw-Geraete:")
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
                print(f"  Konnte Descriptor nicht lesen: {e}")
                reports = {}
            reports_by_node[dev["node"]] = reports
            print("  Report-Overview (Bits):")
            for rid in sorted(reports, key=lambda r: (r is None, r)):
                info = reports[rid]
                print(f"    {describe(rid):22} input={info['input']:4}  output={info['output']:4}  "
                      f"feature={info['feature']:4}  -> Output-Payload {output_report_len(info['output'])} Byte")
        if not args.dry_run and not args.send:
            probe_reports(dev, reports_by_node.get(dev["node"], {}), args)

    # Direktsendemodus
    if args.send:
        rid_s, _, hexs = args.send.partition(":")
        try:
            payload = bytes.fromhex(hexs.replace(" ", "").replace(":", ""))
            rid = None if rid_s.lower() == "u" else int(rid_s, 16)
        except ValueError:
            sys.exit("--send erwartet ID:HEXBYTES, z. B. 01:ff00 oder u:ff")
        if not devs:
            sys.exit("Kein Ziel-Device.")
        node = devs[0]["node"]
        print(f"Sende an {node}: {describe(rid)} <- {payload.hex(' ')}")
        for _ in range(max(1, args.pulse)):
            send(node, rid, payload, args.write_timeout, "Direkt")
            time.sleep(args.delay)
        send(node, rid, [0x00] * len(payload), args.write_timeout, "Stop")

    if not args.dry_run and not args.send:
        cleanup(devs, reports_by_node, args)
        print("\nFertig. Bei Treffern: Muster mit --send ID:HEX wiederholbar machen und mir bescheid geben.")


if __name__ == "__main__":
    main()
