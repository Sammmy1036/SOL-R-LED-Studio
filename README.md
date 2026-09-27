# SOL-R LED Studio

**Custom RGB lighting for the Thrustmaster SOL-R 2 flightsticks on Windows.**

SOL-R LED Studio lets you set the color of every individually addressable LED on both SOL-R sticks, save lighting profiles, run animated effects, and switch profiles automatically when a game launches. It needs no T.A.R.G.E.T. scripts or control-panel tricks, and it doesn't interfere with how your games, vJoy, or HidHide see the sticks.

![SOL-R LED Studio screenshot](docs/screenshot.png)

---

## Features

- **Per-LED control.** Click any of the 20 LED zones on the interactive stick diagram: the thumbstick ring, the 8-segment gimbal ring, the 3-segment logo, and both 4-button banks. Ctrl-click selects several at once.
- **Group control.** Set everything, the ring, the logo, the thumbstick, or either button bank in one click, on the left stick, the right stick, or both.
- **Color wheel.** Pick hue and saturation, adjust the value slider, type a hex code, or use presets and your recent colors. Changes show on the sticks live.
- **Identify.** Right-click any zone to blink that LED on the physical stick.
- **Mirror sticks.** Edit one stick and the other follows.
- **Lighting effects.** Breathing, Wave, Ring spin, Twinkle, Spectrum, and Rainbow wave, each with adjustable speed.
- **Profiles.** Every profile has its own colors, effect, brightness, and on/off state.
- **Game triggers.** Link a profile to a game's `.exe`. It turns on automatically while the game runs, and the sticks return to your idle profile (for example "Lights off") when the game closes.
- **System tray.** The app runs quietly in the tray, so switching profiles and running effects keep working in the background. Start it with Windows if you like.
- **Hot-plug aware.** Unplugged sticks show as disconnected within about a second and disappear from the diagram. Plug them back in and your profile is reapplied automatically.
- **One-click driver setup.** The installer (or a button inside the app) does the one-time Windows driver setup for you, and uninstalling reverses it.

## Works alongside your existing setup

The SOL-R exposes its **joystick** (axes and buttons) and its **LEDs** on two separate USB interfaces. SOL-R LED Studio only ever talks to the LED interface, so:

- ✅ **HidHide:** it works even when the sticks are hidden by HidHide, and you **don't need to add SOL-R LED Studio to HidHide's application list**. HidHide filters the joystick (HID) interface; the LED interface isn't a HID device, so HidHide never gets in the way.
- ✅ **vJoy / remapping:** it works even when the sticks are "vJoy'ed" through Joystick Gremlin or a similar feeder. Your feeder keeps reading the physical sticks while the app controls the lights.
- ✅ **T.A.R.G.E.T. and games:** nothing about how Windows, games, or Thrustmaster's software see the joystick changes.
- ✅ **Plain setups:** you don't need any of the above. Sticks plugged in normally work just the same.

---

## Installation

1. Download **`SolR-LED-Setup.exe`** from the [Releases](https://github.com/Sammmy1036/SolR-LED-Studio/releases) page.
2. **Plug in your SOL-R sticks**, then run the installer. It may ask for administrator rights once, to set up the LED driver (see [How it works](#how-it-works)).
3. Launch **SOL-R LED Studio** from the Start menu.

If the sticks weren't plugged in during installation, that's fine. When you connect them later, the app shows **Setup needed** under *Devices* with a **Set up LED driver** button. Click it, approve the admin prompt, and the sticks connect within a few seconds.

> **Windows SmartScreen:** the installer isn't code-signed, so Windows may show an "unknown publisher" warning. Click **More info → Run anyway**.

### Requirements

- Windows 10 or 11, 64-bit
- Thrustmaster SOL-R flightstick(s) (left `044F:042A`, right `044F:0422`)

---

## Using the app

| Action | How |
|---|---|
| Color one LED | Click it on the diagram, then pick a color |
| Color several LEDs | Ctrl-click to add LEDs to the selection |
| Color a group | Use the **Select** chips (All, Ring, Logo, Thumb, Buttons, Bank L, Bank R) and **Groups apply to** Both / Left / Right |
| Find an LED on the stick | Right-click it on the diagram (**Identify**) |
| Turn lights on/off | **Lights** switch at the top |
| Animate | **Effect** tab → choose a mode and speed |
| New profile | **＋ New profile** in the sidebar (starts as a copy of the current one) |
| Auto-switch for a game | Select the profile → **＋ Add game .exe** → choose the game's executable |
| Choose the idle profile | Sidebar → *When no game is running* |
| Run at startup | Sidebar → **Start with Windows** (starts hidden in the tray) |

Closing the window hides the app to the system tray. Right-click the tray icon to switch profiles, toggle the lights, or quit.

---

## Known limitations

### LEDs that can't be controlled

These lights are managed by the stick's firmware and **can't be recolored or turned off**:

- The **D1 / D2** indicators
- The **throttle position LEDs** that light up when throttle up/down is applied

If someone is able to probe and find the LED ID command, please reach out.

### D1/D2 flicker during effects

When an animated effect is running, the **D1 / D2 indicators flicker**. **This is harmless.** The firmware flashes these indicators every time the stick *receives a command* from the PC, and animated effects work by sending the stick a stream of color updates. Each update produces a short blink. Static lighting sends nothing once it's applied, so D1/D2 stay steady.

Lowering the effect **Smoothness** may reduce how often updates are sent, but the flicker can't be eliminated while an animation runs. If it's bothersome, I recommend sticking to a Static profile.

### Other notes

- Effects are animated by the app and streamed to the sticks, so **the app must be running** (the tray is fine) for effects to animate. Static colors stay on the sticks without the app.
- Only **one program at a time** can use the LED interface.
- The LED protocol comes from community reverse engineering. A future Thrustmaster firmware update could change it.

---

## How it works

### Two interfaces, one of them unused

Each SOL-R stick is a USB *composite device* with two interfaces:

| Interface | What it is | Driver |
|---|---|---|
| `MI_00` | The joystick: axes and buttons (HID game controller) | Windows HID (`HidUsb`) |
| `MI_01` | A vendor-specific interface that controls the RGB LEDs | **None** out of the box |

Games, vJoy feeders, HidHide, and T.A.R.G.E.T. all work with `MI_00`. The LED interface `MI_01` normally shows up in Device Manager as a **VENDOR** device with no driver, which means no program can talk to it at all.

### One-time driver setup

To reach the LED interface, SOL-R LED Studio attaches Microsoft's own **WinUSB** driver to `MI_01`, and only to `MI_01`. WinUSB ships with Windows and is signed by Microsoft, so there's nothing third-party to install and no driver-signing workaround. The setup (`SolR-LED.exe --install-driver`) does three things automatically:

1. Uses the Windows SetupAPI to bind the built-in **WinUSB** driver (`winusb.inf`) to each stick's `MI_01` interface. This is the same as choosing *Update driver → Let me pick → Universal Serial Bus devices → WinUsb Device* in Device Manager.
2. Adds a **DeviceInterfaceGUID** to the interface's registry key, which libusb needs to open an interface on a composite device.
3. Restarts the interface so the change takes effect.

The joystick interface is never modified. Uninstalling the app runs `--uninstall-driver`, which removes WinUSB from those interfaces and returns them to their original driverless state. A log of each run is written to `%APPDATA%\SolR-LED\driver-setup.log`.

### Talking to the LEDs

The app opens the LED interface with **libusb** (via `pyusb`) and sends small USB packets to endpoint `0x02`. Each LED takes an ID and an RGB value:

```
Thumbstick:   01 88 81 FF  00  RR GG BB
Other LEDs:   01 08 85 FF  id RR GG BB  [id RR GG BB]     (two LEDs per packet)
```

| ID | LED | ID | LED |
|---|---|---|---|
| `0x00` | Thumbstick | `0x0B` | Ring lower right |
| `0x01` | Logo bottom | `0x0C` | Ring bottom |
| `0x02` | Logo right | `0x0D` | Ring lower left |
| `0x03` | Logo left | `0x0E` | Ring left |
| `0x04` | Ring top | `0x0F` | Ring upper left |
| `0x05` | Ring upper right | `0x11` / `0x10` / `0x12` / `0x13` | Buttons 5 / 6 / 7 / 8 |
| `0x06` | Ring right | `0x08` / `0x07` / `0x09` / `0x0A` | Buttons 16 / 17 / 18 / 19 |

To stay reliable, the app keeps one open handle per stick, paces its packets, drains any replies from the stick, and retries if a transfer times out.

### Effects, profiles, and game detection

- **Effects** are computed by the app about 2–12 times a second (depending on *Smoothness*). Only LEDs whose color visibly changed are sent, and both sticks are updated in parallel.
- **Profiles** are saved in `%APPDATA%\SolR-LED\config.json`.
- **Game triggers** work by checking the list of running processes every 2 seconds (through the Windows Toolhelp API, with no extra services). When a trigger's `.exe` appears, its profile activates. When the game exits, the idle profile returns.
- **Hot-plug** detection re-enumerates USB devices about every ¾ second.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Stick shows **Setup needed** | Click **Set up LED driver** in the sidebar and approve the admin prompt |
| Stick shows **In use by another app** | Close any other tool using the LEDs (only one program at a time) |
| Colors don't change after replugging into a different USB port | Click **Rescan devices**. If it says Setup needed, run the setup again |
| Driver setup failed | Check `%APPDATA%\SolR-LED\driver-setup.log` |
| Old icon still showing after an update | Run `ie4uinit.exe -show` to refresh the Windows icon cache |

---

## Building from source

```bat
python -m pip install customtkinter pillow pystray pyusb libusb-package pyinstaller
python -m PyInstaller --onefile --noconsole --name SolR-LED --icon SolR-LED.ico ^
    --collect-all customtkinter --collect-all libusb_package solr_led_studio.py
```

To build the installer, open `SolR-LED-Setup.iss` in [Inno Setup 6](https://jrsoftware.org/isinfo.php) with `dist\SolR-LED.exe` and `SolR-LED.ico` next to it, and compile.

Command-line options:

| Option | What it does |
|---|---|
| *(none)* | Open the app, or bring the running copy to the front |
| `--tray` | Start hidden in the system tray |
| `--install-driver` | One-time LED driver setup (run as administrator) |
| `--uninstall-driver` | Undo the driver setup (run as administrator) |

---

## Credits

- **[gort818/solr-led](https://github.com/gort818/solr-led)**: huge thanks to gort818 for reverse engineering the SOL-R LED protocol, including the LED IDs and packet format. SOL-R LED Studio's lighting control is built on that work.
- [libusb](https://libusb.info/) / [pyusb](https://github.com/pyusb/pyusb), [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter), [Pillow](https://python-pillow.org/), and [pystray](https://github.com/moses-palmer/pystray).

## Disclaimer

SOL-R LED Studio is an independent community project. It is **not affiliated with, endorsed by, or supported by Thrustmaster or Guillemot Corporation**. Thrustmaster and SOL-R are trademarks of their respective owners. Use at your own risk.
