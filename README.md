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

Works with Steam (per-game setup, see [Games](#games)) and with games
launched outside Steam (Heroic, GOG, direct SDL/Wine) — anything that uses
Linux force feedback (evdev FF, e.g. via SDL2).

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
- `acl` (`setfacl`, recommended) — lets the daemon strip uaccess ACLs from
  the pad's device nodes so Steam cannot open the real pad

## Installation

```sh
sudo ./install.sh
```

What it does:

1. Installs `gc5000_ffd.py` to `/usr/local/bin/`
2. Installs the systemd unit (`gc5000-ffd.service`)
3. Installs the udev rules (`/etc/udev/rules.d/90-gc5000.rules` for
   auto-start, `91-gc5000-ffproxy.rules` for the stable `/dev/gc5000-ffproxy`
   symlink used by games/launchers, and `94-gc5000-realmode.rules`, which
   locks the real pad to root so user space only ever sees the proxy)
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

## Games

The real pad is muted by the daemon (`EVIOCGRAB`), so a game that opens the
real device gets silence — games must talk to the proxy. Two things make
that reliable:

1. **A stable device path for the proxy** (otherwise you depend on the `eventN`
   numbering). `/etc/udev/rules.d/91-gc5000-ffproxy.rules` — shipped and
   installed by `install.sh`; shown here for reference and manual setups:

   ```
   ACTION=="add", SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Nokia GC 5000 (FF-Proxy)", SYMLINK+="gc5000-ffproxy"
   ```

   Then `sudo udevadm control --reload` and reconnect the controller. The
   proxy is now reachable as `/dev/gc5000-ffproxy`.

2. **Pin SDL to the proxy** with `SDL_JOYSTICK_DEVICE=/dev/gc5000-ffproxy`, so
   SDL-based games (native or via Wine/Proton's SDL joystick backend) only
   enumerate the FF-capable proxy and cannot pick the real, muted pad.

How you set the variable depends on the launcher:

### Non-Steam launchers (Heroic, GOG, …)

In **Heroic**, set it globally under *Settings → Advanced → Environment
Variables*, or per game via *game page → Settings → Advanced → Environment
Variables*. Confirmed working with *Mafia II* via Heroic. Any other launcher
or a direct `env SDL_JOYSTICK_DEVICE=/dev/gc5000-ffproxy …` invocation works
the same way.

### Steam

Since the October 2026 client update, the Linux Steam client is a 64-bit
build running inside its own pressure-vessel container, and games are
launched into a nested container. Two consequences for this proxy:

- **A global `SDL_JOYSTICK_DEVICE` (e.g. set in the `.desktop` launcher) no
  longer reaches games.** The containerized client passes a curated
  environment to games (mostly Steam's own `SDL_*` variables) instead of
  inheriting the client environment. The client itself still honours the
  variable — its own controller test rumbles — which makes this failure
  mode confusing.
- Steam Input may **open and grab both** the real pad and the proxy, while
  the game enumerates both devices as well; a game can end up with two
  silent devices and no input at all.

**Lock the real pad away from user space.** When Steam Input can open the
grabbed (silent) real pad, it registers it as a controller next to the
proxy; a game that binds or rumbles that dead slot loses rumble (or
input) for no visible reason. Keep the pad exclusive to the daemon:
`94-gc5000-realmode.rules` (shipped, installed by `install.sh`) sets the
pad's event, js and hidraw nodes to mode 0600 root-only — and because
mode bits alone are not enough (logind's uaccess mechanism grants the
seated user an ACL that overrides them), the daemon strips those ACLs
from the nodes at startup (`setfacl -b`, needs the `acl` package). The
udev rule restarts the service on every reconnect, so the lock survives
power-cycling. With the lock in place, Steam sees only the FF proxy and
the dead slot disappears.

Two routes work; pick per game.

**Route A — via Steam Input (the default).** Leave Steam Input enabled and
set no launch option. Games talk to Steam's virtual controller; Steam Input
forwards input and rumble to the proxy (the client's own controller test
uses the same path). If input or rumble misbehaves on this route, check
that the real pad is actually locked away (daemon log: `ACLs stripped`);
otherwise switch that game to route B.

**Route B — direct to the proxy.** Per game:

1. *Properties → Controller*: set the Steam Input override to **Disable**.
2. *Properties → Launch options*:

   ```
   SDL_JOYSTICK_DEVICE=/dev/gc5000-ffproxy %command%
   ```

Launch options are passed through to the game environment, container or
not. Confirmed working with *Mafia II* added to Steam as a non-Steam game:
input through the proxy plus rumble `FF upload` events at the daemon.

**Grab order matters.** The daemon must `EVIOCGRAB` the real pad *before*
Steam does. In normal operation the udev rule wins that race automatically,
because the daemon starts the moment the controller connects. When
restarting the daemon manually while Steam is running, quit Steam first, or
stop the service and start the daemon in one command:

```sh
sudo systemctl stop gc5000-ffd && sudo python3 /usr/local/bin/gc5000_ffd.py -v
```

The daemon refuses to start while the pad is already grabbed (older builds
print the German "Pad ist bereits gegrabbt") — that means another daemon
instance (usually the systemd service) or Steam Input holds the grab.
Restart games after a daemon restart: they keep the old, dead proxy node
open otherwise.

**Diagnosing**, in order of usefulness:

```sh
# did the launch option arrive in the game's environment?
sudo tr '\0' '\n' < /proc/<game-pid>/environ | grep SDL_JOYSTICK_DEVICE

# who currently holds which event node?
sudo fuser -v /dev/input/event* /dev/gc5000-ffproxy

# did Steam Input open the devices (or fail to)?
grep -E 'opened for index|reserving XInput|Local Device Found' \
  ~/.steam/steam/logs/controller.txt | tail
```

Running the daemon with `-v` shows `FF upload` lines while playing — proof
that the game is actually sending rumble to the proxy.

**Game quirks that can look like proxy bugs** (both confirmed with the
NW.js/Chromium game *Sheepy: A Short Adventure*):

- Some games only register gamepads that connect *after* the game has
  started; a controller connected before launch is ignored. If a game sees
  no controller at all, power it off, start the game, wait for the menu,
  then connect the controller.
- If a game's rumble worked before and vanished after the October 2026
  client update, the likely cause is
  [ValveSoftware/steam-for-linux #13394](https://github.com/ValveSoftware/steam-for-linux/issues/13394):
  Steam Input no longer creates its virtual Xbox pad for generic evdev
  controllers, and Chromium/NW.js games only rumble through such a pad.
  In testing, no Proton version changed this; track the upstream issue and
  re-test once it is fixed.

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
- **Steam shows two controllers:** with the shipped lockdown the real pad
  stays invisible, and Steam should list only the proxy. If both appear,
  the lock is not active: check `getfacl /dev/input/eventN` for a user ACL,
  make sure the `acl` package and `94-gc5000-realmode.rules` are installed
  (`sudo ./install.sh`), and restart Steam.
- **Steam game has no gamepad input at all:** the launch option is missing
  (unsaved) or Steam Input is still enabled for that game — see
  [Steam](#steam). Verify with the `/proc/<pid>/environ` check above.
- **Game has input but no rumble:** run the daemon with `-v`; if no
  `FF upload` lines appear, the game is not sending FF at all.
  Chromium/NW.js games (e.g. *Sheepy: A Short Adventure*) only rumble
  through Steam Input's virtual Xbox pad, which the October 2026 client
  no longer creates for generic evdev controllers — a Valve regression
  ([ValveSoftware/steam-for-linux #13394](https://github.com/ValveSoftware/steam-for-linux/issues/13394)),
  not a proxy problem; changing the Proton version does not help. Some
  other games genuinely never rumble under Proton (some UE4 titles) —
  also a game-side limitation. Otherwise pin SDL to the proxy — see
  [Games](#games).
- **EVIOCGRAB fails with EBUSY / "Pad ist bereits gegrabbt":** another
  daemon instance (check `systemctl status gc5000-ffd`) or Steam Input
  holds the grab. Stop the service before running a manual instance and
  mind the grab order in [Steam](#steam).

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
