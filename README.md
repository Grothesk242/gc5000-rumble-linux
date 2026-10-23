# gc5000-rumble-linux

**Force feedback (rumble) for the "Nokia Game Controller 5000" (GC5000) on Linux.**

The GC5000 is a Bluetooth gamepad (StreamView, `1D5A:1802`) that Linux drives with
hid-generic **without any force-feedback support** (`ff=0`) — even though the pad
contains two rumble motors that work fine on Android. USB-C is charge-only, so
there is no wired path either.

This project makes rumble work anyway, in two parts:

1. **Reverse-engineered rumble protocol** — a small hidraw probe script and the
   protocol findings (see below) that would also serve an in-kernel driver.
2. **`gc5000_ffd.py`** — a user-space FF proxy daemon: it creates a virtual
   uinput gamepad ("Nokia GC 5000 (FF-Proxy)") with real force-feedback
   capabilities, forwards all input 1:1, and translates FF effects from
   games/SDL/Steam into hidraw rumble reports on the real pad.
   Ships with a systemd unit + udev rule so it starts automatically whenever
   the controller connects.

Works with Steam (the proxy shows up as the FF-capable controller) and any
game that uses Linux force feedback (evdev FF, e.g. via SDL2).

## Protocol findings (hidraw)

Discovered by probing `/dev/hidraw*` (see `gc5000-hidraw-rumble-probe.py`):

| Finding | Detail |
|---|---|
| Report ID | `0x01`, 2-byte payload, **undeclared** in the HID descriptor (only input reports 2/4/30 are declared) |
| Byte 0 | strong (right) motor, `0x00` = off, nonzero = on |
| Byte 1 | weak (left) motor, `0x00` = off, nonzero = on |
| Amplitude | **binary** — `0x01` and `0xFF` feel identical; no PWM in firmware |
| Burst behaviour | each report triggers only a short vibration burst; continuous rumble requires the host to repeat the report (heartbeat, ~70–100 ms works well) |
| Stop | report `01 00 00` stops vibration immediately |

Since amplitude is binary, the daemon maps FF effects onto the three
physically possible intensity levels:

- **Rumble / Constant** effects → continuous motor drive with heartbeat.
- **Periodic** effects (sine etc.) → the sine is quantized onto
  *off / weak motor only / both motors* (3-level amplitude), following the
  effect's waveform timing. That is the closest thing to a "swell" this
  hardware can express.
- **Spring / Damper / Inertia** etc. are rejected with EINVAL by design.

## Requirements

- Linux with Bluetooth (BlueZ), the paired GC5000
- Python 3 (standard library only — no pip packages)
- `fftest` (optional, from `linuxconsole` / `joystick` package) for testing
- Root (uinput, hidraw, EVIOCGRAB)

## Installation

```sh
sudo ./install.sh
```

What it does:

1. Installs `gc5000_ffd.py` to `/usr/local/bin/`
2. Installs the systemd unit (`gc5000-ffd.service`)
3. Installs the udev rule (`/etc/udev/rules.d/90-gc5000.rules`)
4. Reloads systemd + udev

The service is **static** (no `[Install]` section on purpose): it is started
by the udev rule whenever the controller's hidraw device appears — including
hotplug, suspend/resume and reconnects. When Bluetooth drops, the daemon
exits cleanly (exit code 0) and will be retriggered on the next connect.

## Usage

The daemon runs automatically after installation. For manual runs:

```sh
# foreground proxy with verbose FF logging
sudo python3 gc5000_ffd.py -v

# quick motor test without the proxy
sudo python3 gc5000_ffd.py vibrate --motors both --duration 1

# strong/weak motors separately
sudo python3 gc5000_ffd.py vibrate --motors strong --duration 2
sudo python3 gc5000_ffd.py vibrate --motors weak   --duration 2
```

Testing with fftest (run the proxy in one terminal first):

```sh
fftest /dev/input/eventXX   # event node of "Nokia GC 5000 (FF-Proxy)"
```

Expected: effects 0 (periodic sine — the 3-level "wave"), 1 (constant) and
the rumble effects play; unsupported types return EINVAL.

Options: `--device /dev/hidrawN` (override hidraw node), `--no-grab` (debug:
don't grab the real pad, games will see both devices), `--wait SECONDS`
(wait for the device to appear, used by the systemd unit).

## Troubleshooting

- **Service doesn't start on connect.** Note for Bluetooth HID devices: they
  do **not** expose `idVendor`/`idProduct` sysfs attributes (those are
  USB-specific). Match the parent device name instead:
  `KERNELS=="0005:1D5A:1802.*"` (bus `0005` = Bluetooth HID, then VID:PID —
  case matters). After editing rules run `sudo udevadm control --reload` and
  retrigger an already-connected device with
  `sudo udevadm trigger --action=add /dev/hidrawN`.
- **Check what's happening:** `systemctl status gc5000-ffd`,
  `journalctl -u gc5000-ffd -f`.
- **hidraw node moved** (e.g. `hidraw5` instead of `hidraw3`): the daemon
  scans sysfs by VID/PID, nothing to adjust.
- **Steam shows two controllers:** expected — the real (grabbed) pad and the
  proxy. Only the proxy has FF; rename it in Steam so games pick the right one.

## Reverse-engineering the protocol yourself

`gc5000-hidraw-rumble-probe.py` sends raw reports to the hidraw node and was
used to establish the protocol above (binary amplitude, burst behaviour,
motor byte order). Useful for other pads whose rumble is not supported by
their kernel driver: many cheap Bluetooth gamepads hide similar undeclared
output reports.

## Limitations

- Motors are binary: no analog intensity. Periodic effects are approximated
  with a 3-level quantization (see above).
- Per-effect heartbeat interval and quantization thresholds are constants at
  the top of `gc5000_ffd.py` and can be tuned.

## License

MIT — see [LICENSE](LICENSE). Reverse-engineered protocol data provided as
found; no warranty. "Nokia" is used descriptively; this project is not
affiliated with Nokia or StreamView.
