"""
SOL-R LED Studio - LED control for Thrustmaster SOL-R flightsticks (Windows).

Talks to the stick's vendor LED interface (MI_01) over WinUSB.

Build:
  python -m pip install customtkinter pillow pystray pyusb libusb-package pyinstaller
  python -m PyInstaller --onefile --noconsole --name SolR-LED --icon SolR-LED.ico ^
      --version-file version_info.txt --collect-all customtkinter --collect-all libusb_package solr_led_studio.py

Run:
  SolR-LED.exe           open the window (or bring the running copy to front)
  SolR-LED.exe --tray    start hidden in the system tray (used at logon)
  SolR-LED.exe --install-driver     one-time LED driver setup (run as admin; the installer does this)
  SolR-LED.exe --uninstall-driver   undo the driver setup
"""

import colorsys
import ctypes
import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

try:
    import pystray
    TRAY_OK = True
except Exception:
    TRAY_OK = False

try:
    import usb.core
    import usb.util
    import usb._interop as _interop
    import libusb_package
    USB_OK, USB_ERR = True, ""
except Exception as e:
    USB_OK, USB_ERR = False, str(e)

APP_NAME = "SOL-R LED Studio"
IS_WIN = os.name == "nt"

# ============================================================================
# Device protocol (from gort818/solr-led reverse engineering)
# ============================================================================

VID = 0x044F
PIDS = {"left": 0x042A, "right": 0x0422}
INTERFACE = 1
ENDPOINT_OUT = 0x02
PACKET_GAP = 0.015
WRITE_TIMEOUT = 500
RETRIES = 3

LED_NAMES = {
    0x00: "Thumbstick",
    0x01: "Logo bottom", 0x02: "Logo right", 0x03: "Logo left",
    0x04: "Ring top", 0x05: "Ring upper right", 0x06: "Ring right",
    0x0B: "Ring lower right", 0x0C: "Ring bottom", 0x0D: "Ring lower left",
    0x0E: "Ring left", 0x0F: "Ring upper left",
    0x11: "Button 5", 0x10: "Button 6", 0x12: "Button 7", 0x13: "Button 8",
    0x08: "Button 16", 0x07: "Button 17", 0x09: "Button 18", 0x0A: "Button 19",
}
ALL_LEDS = sorted(LED_NAMES)
RING = [0x04, 0x05, 0x06, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F]
LOGO = [0x01, 0x02, 0x03]
BANK_L = [0x11, 0x10, 0x12, 0x13]
BANK_R = [0x08, 0x07, 0x09, 0x0A]

GROUPS = {
    "All": ALL_LEDS,
    "Ring": RING,
    "Logo": LOGO,
    "Thumb": [0x00],
    "Buttons": BANK_L + BANK_R,
    "Bank L": BANK_L,
    "Bank R": BANK_R,
}

PRESETS = ["#ff6a00", "#ffb000", "#ff1f3d", "#ff3fb4", "#a23bff", "#2f7bff", "#00d5ff", "#20e070"]


# ============================================================================
# Effects (rendered by the app and streamed to the sticks)
# ============================================================================

EFFECTS = [  # id, icon, name, description, uses_profile_colors
    ("static", "●", "Static", "Effect Mode set to Static.", True),
    ("breathing", "◐", "Breathing", "Effect Mode set to Breathing.", True),
    ("wave", "≈", "Wave", "Effect Mode set to Wave.", True),
    ("spin", "↻", "Ring Spin", "Effect Mode set to Ring Spin.", True),
    ("twinkle", "✧", "Twinkle", "Effect Mode set to Twinkle.", True),
    ("spectrum", "◑", "Spectrum", "Effect Mode set to Spectrum.", False),
    ("rainbow", "≋", "Rainbow Wave", "Effect Mode set to Rainbow Wave.", False),
]
EFFECT_IDS = [e[0] for e in EFFECTS]
SMOOTHNESS = {"Smooth": 12, "Balanced": 5, "Low traffic": 2}   # frames per second sent to the sticks
CHANGE_THRESHOLD = 6   # skip LEDs whose color moved less than this (0-255) since last sent
RING_ORDER = [0x04, 0x05, 0x06, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F]  # clockwise from top


def _led_positions():
    pos = {0x00: (170, 55)}
    angles = {0x04: 90, 0x05: 45, 0x06: 0, 0x0B: 315, 0x0C: 270, 0x0D: 225, 0x0E: 180, 0x0F: 135}
    for led, a in angles.items():
        pos[led] = (170 + 48 * math.cos(math.radians(a)), 262 - 48 * math.sin(math.radians(a)))
    pos[0x03], pos[0x02], pos[0x01] = (161, 344), (179, 344), (170, 359)
    for x0, y0, grid in ((38, 238, [[0x11, 0x10], [0x12, 0x13]]), (240, 238, [[0x07, 0x08], [0x0A, 0x09]])):
        for r, row in enumerate(grid):
            for c, led in enumerate(row):
                pos[led] = (x0 + c * 34 + 14, y0 + r * 27 + 10)
    return pos


LED_POS = _led_positions()
_TWINKLE = {}
for _si in range(2):
    for _led in range(0x14):
        _r = (_led * 2654435761 + _si * 40503) % 1000 / 1000
        _TWINKLE[(_si, _led)] = (_r, 0.6 + ((_led * 7919 + _si * 31) % 100) / 100)


def effect_frame(prof, t):
    """{side: {led: (r, g, b)}} at time t, before brightness. Floats 0-255."""
    mode = prof.get("effect", "static")
    f = 0.15 + prof.get("speed", 40) / 100 * 1.85
    out = {}
    for si, side in enumerate(("left", "right")):
        frame = {}
        for led in ALL_LEDS:
            base = bytes.fromhex(prof[side][f"{led:02X}"][1:])
            x, y = LED_POS[led]
            gx = x + si * 360  # continuous x across both sticks
            k = 1.0
            color = None
            if mode == "breathing":
                k = 0.10 + 0.90 * (0.5 - 0.5 * math.cos(2 * math.pi * t * f * 0.3))
            elif mode == "wave":
                ph = gx / 720 + y / 1600 - t * f * 0.3
                k = 0.12 + 0.88 * (0.5 + 0.5 * math.sin(2 * math.pi * ph)) ** 1.5
            elif mode == "spin":
                if led in RING_ORDER:
                    head = (t * f * 1.1 * 8) % 8
                    dist = (head - RING_ORDER.index(led)) % 8
                    k = max(0.08, 1 - dist / 3.5)
            elif mode == "twinkle":
                ph, rate = _TWINKLE[(si, led)]
                k = 0.15 + 0.85 * max(0.0, math.sin(2 * math.pi * (t * f * rate * 0.5 + ph))) ** 6
            elif mode == "spectrum":
                color = colorsys.hsv_to_rgb((t * f * 0.07) % 1, 1, 1)
            elif mode == "rainbow":
                color = colorsys.hsv_to_rgb((gx / 720 + y / 1600 - t * f * 0.12) % 1, 1, 1)
            if color is not None:
                frame[led] = tuple(c * 255 for c in color)
            else:
                frame[led] = tuple(c * k for c in base)
        out[side] = frame
    return out


def frame_hex(frame_side):
    return {l: "#%02x%02x%02x" % tuple(int(v) for v in c) for l, c in frame_side.items()}


def frame_payload(frame_side, prof):
    s = max(0, min(100, prof["brightness"])) / 100
    return {l: bytes(int(v * s) for v in c) for l, c in frame_side.items()}


def build_packets(colors):
    packets = []
    if 0x00 in colors:
        packets.append(bytes([0x01, 0x88, 0x81, 0xFF, 0x00]) + colors[0x00])
    others = [led for led in colors if led != 0x00]
    for i in range(0, len(others), 2):
        pkt = bytes([0x01, 0x08, 0x85, 0xFF])
        for led in others[i:i + 2]:
            pkt += bytes([led]) + colors[led]
        packets.append(pkt)
    return packets


class SolRIO:
    """Keeps one open, claimed handle per stick and reuses it."""

    def __init__(self):
        self._backend = None
        self._open = {}
        self._last = {}      # side -> {led: bytes} last colors the stick accepted
        self.gap = 0.010     # seconds between packets; grows if the stick struggles

    def backend(self):
        if self._backend is None:
            self._backend = libusb_package.get_libusb1_backend()
        return self._backend

    def _find(self, side):
        return usb.core.find(idVendor=VID, idProduct=PIDS[side], backend=self.backend())

    def close(self, side):
        self._last.pop(side, None)
        h = self._open.pop(side, None)
        if h:
            for fn in (lambda: usb.util.release_interface(h["dev"], INTERFACE),
                       lambda: usb.util.dispose_resources(h["dev"])):
                try:
                    fn()
                except Exception:
                    pass

    def close_all(self):
        for side in list(self._open):
            self.close(side)

    def _get(self, side):
        if side in self._open:
            return self._open[side]
        dev = self._find(side)
        if dev is None:
            raise LookupError("not connected")
        try:
            usb.util.claim_interface(dev, INTERFACE)
        except Exception:
            usb.util.dispose_resources(dev)
            raise
        out_type = ep_in = in_type = None
        in_size = 64
        for intf in dev[0]:
            if intf.bInterfaceNumber != INTERFACE:
                continue
            for ep in intf:
                if ep.bEndpointAddress == ENDPOINT_OUT:
                    out_type = usb.util.endpoint_type(ep.bmAttributes)
                elif usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                    ep_in, in_type = ep.bEndpointAddress, usb.util.endpoint_type(ep.bmAttributes)
                    in_size = ep.wMaxPacketSize
        be = dev._ctx.backend
        h = {
            "dev": dev, "be": be, "handle": dev._ctx.handle,
            "write": be.bulk_write if out_type == usb.util.ENDPOINT_TYPE_BULK else be.intr_write,
            "read": be.bulk_read if in_type == usb.util.ENDPOINT_TYPE_BULK else be.intr_read,
            "ep_in": ep_in,
            "buf": usb.util.create_buffer(in_size) if ep_in else None,
        }
        self._open[side] = h
        return h

    def _drain(self, h):
        if h["ep_in"] is None:
            return
        for _ in range(8):
            try:
                if h["read"](h["handle"], h["ep_in"], INTERFACE, h["buf"], 5) <= 0:
                    return
            except Exception:
                return

    def present(self):
        """Which sticks are plugged in right now (one USB enumeration)."""
        found = set()
        for dev in usb.core.find(find_all=True, idVendor=VID, backend=self.backend()):
            for side, pid in PIDS.items():
                if dev.idProduct == pid:
                    found.add(side)
        return found

    def probe(self, side):
        if not USB_OK:
            return "error", "USB libraries missing"
        self.close(side)
        try:
            self._get(side)
            return "ok", "Connected"
        except LookupError:
            return "missing", "Not connected"
        except Exception as e:
            self.close(side)
            ds = driver_setup()
            try:
                if ds and ds.state().get(side) == "needs-setup":
                    return "setup", "Setup needed"
            except Exception:
                pass
            if "access denied" in str(e).lower():
                return "error", "In use by another app"
            return "error", "LED interface unavailable"

    def write(self, side, colors, force=False):
        last = self._last.get(side, {})
        if not force:
            colors = {l: c for l, c in colors.items() if last.get(l) != c}
        if not colors:
            return
        h = self._get(side)
        try:
            for pkt in build_packets(colors):
                data = _interop.as_array(pkt)
                for attempt in range(RETRIES):
                    try:
                        h["write"](h["handle"], ENDPOINT_OUT, INTERFACE, data, WRITE_TIMEOUT)
                        break
                    except usb.core.USBError:
                        if attempt == RETRIES - 1:
                            raise
                        self.gap = min(0.03, self.gap + 0.003)  # back off
                        self._drain(h)
                        try:
                            h["be"].clear_halt(h["handle"], ENDPOINT_OUT)
                        except Exception:
                            pass
                        time.sleep(0.05)
                self._drain(h)
                time.sleep(self.gap)
            self._last.setdefault(side, {}).update(colors)
        except Exception:
            self.close(side)
            raise


# ============================================================================
# One-time Windows driver setup for the LED interface (MI_01)
#
# Does what we did by hand: bind Microsoft's in-box WinUSB driver (winusb.inf,
# already signed and shipped with Windows) to the driverless VENDOR interface,
# add a DeviceInterfaceGUID so libusb can open it, then restart the device.
# The joystick interface (MI_00) is never touched.
# ============================================================================

USBDEVICE_CLASS = "{88BAE032-5A81-49F0-BC3D-A4FF138216D6}"  # "Universal Serial Bus devices"
LED_IFACE_IDS = {side: f"USB\\VID_{VID:04X}&PID_{pid:04X}&MI_01" for side, pid in PIDS.items()}
NO_WINDOW = 0x08000000


class DriverSetup:
    DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
    SPDRP_SERVICE, SPDRP_CLASSGUID = 0x04, 0x08
    SPDIT_CLASSDRIVER = 0x1
    DI_ENUMSINGLEINF = 0x00010000
    DI_FLAGSEX_ALLOWEXCLUDEDDRVS = 0x00000800

    def __init__(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("driver setup must run as a 64-bit program (this build is 32-bit)")
        from ctypes import wintypes as wt
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class SP_DEVINFO_DATA(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("ClassGuid", GUID), ("DevInst", wt.DWORD),
                        ("Reserved", ctypes.c_size_t)]

        class SP_DEVINSTALL_PARAMS_W(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("Flags", wt.DWORD), ("FlagsEx", wt.DWORD),
                        ("hwndParent", ctypes.c_void_p), ("InstallMsgHandler", ctypes.c_void_p),
                        ("InstallMsgHandlerContext", ctypes.c_void_p), ("FileQueue", ctypes.c_void_p),
                        ("ClassInstallReserved", ctypes.c_size_t), ("Reserved", wt.DWORD),
                        ("DriverPath", ctypes.c_wchar * 260)]

        class SP_DRVINFO_DATA_V2_W(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("DriverType", wt.DWORD), ("Reserved", ctypes.c_size_t),
                        ("Description", ctypes.c_wchar * 256), ("MfgName", ctypes.c_wchar * 256),
                        ("ProviderName", ctypes.c_wchar * 256), ("DriverDate", wt.FILETIME),
                        ("DriverVersion", ctypes.c_ulonglong)]

        self.GUID, self.DEVINFO, self.PARAMS, self.DRVINFO = GUID, SP_DEVINFO_DATA, SP_DEVINSTALL_PARAMS_W, SP_DRVINFO_DATA_V2_W
        u = uuid.UUID(USBDEVICE_CLASS)
        self.usb_class_guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))

        sa = ctypes.WinDLL("setupapi", use_last_error=True)
        nd = ctypes.WinDLL("newdev", use_last_error=True)
        P, D, B, V = ctypes.POINTER, wt.DWORD, wt.BOOL, ctypes.c_void_p
        def fn(dll, name, res, *args):
            f = getattr(dll, name)
            f.restype, f.argtypes = res, list(args)
            return f
        self.GetClassDevs = fn(sa, "SetupDiGetClassDevsW", V, V, ctypes.c_wchar_p, V, D)
        self.EnumDeviceInfo = fn(sa, "SetupDiEnumDeviceInfo", B, V, D, P(SP_DEVINFO_DATA))
        self.GetInstanceId = fn(sa, "SetupDiGetDeviceInstanceIdW", B, V, P(SP_DEVINFO_DATA), ctypes.c_wchar_p, D, P(D))
        self.GetRegProp = fn(sa, "SetupDiGetDeviceRegistryPropertyW", B, V, P(SP_DEVINFO_DATA), D, P(D), V, D, P(D))
        self.SetRegProp = fn(sa, "SetupDiSetDeviceRegistryPropertyW", B, V, P(SP_DEVINFO_DATA), D, V, D)
        self.GetParams = fn(sa, "SetupDiGetDeviceInstallParamsW", B, V, P(SP_DEVINFO_DATA), P(SP_DEVINSTALL_PARAMS_W))
        self.SetParams = fn(sa, "SetupDiSetDeviceInstallParamsW", B, V, P(SP_DEVINFO_DATA), P(SP_DEVINSTALL_PARAMS_W))
        self.BuildDrivers = fn(sa, "SetupDiBuildDriverInfoList", B, V, P(SP_DEVINFO_DATA), D)
        self.EnumDrivers = fn(sa, "SetupDiEnumDriverInfoW", B, V, P(SP_DEVINFO_DATA), D, D, P(SP_DRVINFO_DATA_V2_W))
        self.SelectDriver = fn(sa, "SetupDiSetSelectedDriverW", B, V, P(SP_DEVINFO_DATA), P(SP_DRVINFO_DATA_V2_W))
        self.DestroyDrivers = fn(sa, "SetupDiDestroyDriverInfoList", B, V, P(SP_DEVINFO_DATA), D)
        self.DestroyList = fn(sa, "SetupDiDestroyDeviceInfoList", B, V)
        self.DiInstallDevice = fn(nd, "DiInstallDevice", B, V, V, P(SP_DEVINFO_DATA), P(SP_DRVINFO_DATA_V2_W), D, P(B))

    @staticmethod
    def _fail(what):
        err = ctypes.get_last_error()
        raise OSError(err, f"{what} failed: {ctypes.FormatError(err).strip()} ({err})")

    @staticmethod
    def is_admin():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _open(self, present_only=True):
        """-> (handle, [(side, instance_id, SP_DEVINFO_DATA)]) for LED interfaces.
        present_only=False also returns ones Windows remembers but aren't plugged in."""
        flags = self.DIGCF_ALLCLASSES | (self.DIGCF_PRESENT if present_only else 0)
        h = self.GetClassDevs(None, "USB", None, flags)
        if not h or h == ctypes.c_void_p(-1).value:
            self._fail("SetupDiGetClassDevs")
        found, i = [], 0
        buf = ctypes.create_unicode_buffer(512)
        while True:
            dd = self.DEVINFO()
            dd.cbSize = ctypes.sizeof(dd)
            if not self.EnumDeviceInfo(h, i, ctypes.byref(dd)):
                break
            i += 1
            if not self.GetInstanceId(h, ctypes.byref(dd), buf, 512, None):
                continue
            iid = buf.value
            for side, prefix in LED_IFACE_IDS.items():
                if iid.upper().startswith(prefix.upper()):
                    found.append((side, iid, dd))
        return h, found

    def _service(self, h, dd):
        buf = ctypes.create_unicode_buffer(256)
        if self.GetRegProp(h, ctypes.byref(dd), self.SPDRP_SERVICE, None, buf, ctypes.sizeof(buf), None):
            return buf.value
        return ""

    @staticmethod
    def _has_guid(iid):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                rf"SYSTEM\CurrentControlSet\Enum\{iid}\Device Parameters") as k:
                for name in ("DeviceInterfaceGUIDs", "DeviceInterfaceGUID"):
                    try:
                        if winreg.QueryValueEx(k, name)[0]:
                            return True
                    except OSError:
                        pass
        except OSError:
            pass
        return False

    @staticmethod
    def _add_guid(iid):
        import winreg
        import uuid
        path = rf"SYSTEM\CurrentControlSet\Enum\{iid}\Device Parameters"
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DeviceInterfaceGUIDs", 0, winreg.REG_MULTI_SZ, ["{%s}" % uuid.uuid4()])

    @staticmethod
    def _pnputil(*args):
        return subprocess.run(["pnputil", *args], capture_output=True, text=True, creationflags=NO_WINDOW)

    def state(self):
        """-> {side: 'ok' | 'needs-setup'} for sticks that are plugged in."""
        h, found = self._open()
        try:
            return {side: ("ok" if self._service(h, dd).lower() == "winusb" and self._has_guid(iid)
                           else "needs-setup") for side, iid, dd in found}
        finally:
            self.DestroyList(h)

    def _bind_winusb(self, h, dd, log):
        inf = Path(os.environ.get("WINDIR", r"C:\Windows")) / "INF" / "winusb.inf"
        if not inf.exists():
            raise FileNotFoundError(f"{inf} not found")
        # Put the device in the USB-device class so the class driver list includes WinUSB
        cls = ctypes.create_unicode_buffer(USBDEVICE_CLASS)
        if not self.SetRegProp(h, ctypes.byref(dd), self.SPDRP_CLASSGUID, cls, ctypes.sizeof(cls)):
            self._fail("Setting device class")
        dd.ClassGuid = self.usb_class_guid
        # Only look inside winusb.inf, and allow drivers that aren't an ID match (like "Let me pick")
        params = self.PARAMS()
        params.cbSize = ctypes.sizeof(params)
        if not self.GetParams(h, ctypes.byref(dd), ctypes.byref(params)):
            self._fail("SetupDiGetDeviceInstallParams")
        params.Flags |= self.DI_ENUMSINGLEINF
        params.FlagsEx |= self.DI_FLAGSEX_ALLOWEXCLUDEDDRVS
        params.DriverPath = str(inf)
        if not self.SetParams(h, ctypes.byref(dd), ctypes.byref(params)):
            self._fail("SetupDiSetDeviceInstallParams")
        if not self.BuildDrivers(h, ctypes.byref(dd), self.SPDIT_CLASSDRIVER):
            self._fail("SetupDiBuildDriverInfoList")
        try:
            best, i = None, 0
            while True:
                drv = self.DRVINFO()
                drv.cbSize = ctypes.sizeof(drv)
                if not self.EnumDrivers(h, ctypes.byref(dd), self.SPDIT_CLASSDRIVER, i, ctypes.byref(drv)):
                    break
                i += 1
                log(f"    candidate: {drv.Description} (v{drv.DriverVersion:#x})")
                if "winusb" in drv.Description.lower() and (best is None or drv.DriverVersion > best.DriverVersion):
                    best = drv
            if best is None:
                raise RuntimeError("WinUSB driver not found in winusb.inf")
            if not self.SelectDriver(h, ctypes.byref(dd), ctypes.byref(best)):
                self._fail("SetupDiSetSelectedDriver")
            reboot = ctypes.c_int(0)
            if not self.DiInstallDevice(None, h, ctypes.byref(dd), ctypes.byref(best), 0, ctypes.byref(reboot)):
                self._fail("DiInstallDevice")
            log(f"    installed: {best.Description}" + ("  (reboot requested)" if reboot.value else ""))
        finally:
            self.DestroyDrivers(h, ctypes.byref(dd), self.SPDIT_CLASSDRIVER)

    def install(self, log=print):
        if not self.is_admin():
            raise PermissionError("administrator rights required")
        h, found = self._open()
        try:
            if not found:
                log("No SOL-R sticks plugged in - nothing to set up.")
                return 0
            for side, iid, dd in found:
                log(f"{side} stick: {iid}")
                svc = self._service(h, dd)
                if svc.lower() != "winusb":
                    log(f"  current driver: {svc or 'none'} -> installing WinUSB")
                    self._bind_winusb(h, dd, log)
                else:
                    log("  WinUSB already installed")
                if not self._has_guid(iid):
                    self._add_guid(iid)
                    log("  added DeviceInterfaceGUID")
                r = self._pnputil("/restart-device", iid)
                log("  restarted device" if r.returncode == 0 else "  couldn't restart - unplug/replug the stick")
            return len(found)
        finally:
            self.DestroyList(h)

    def uninstall(self, log=print):
        if not self.is_admin():
            raise PermissionError("administrator rights required")
        h, found = self._open(present_only=False)
        try:
            targets = [(side, iid) for side, iid, dd in found if self._service(h, dd).lower() == "winusb"]
            if not targets:
                log("No SOL-R LED interfaces with WinUSB found - nothing to undo.")
        finally:
            self.DestroyList(h)
        for side, iid in targets:
            r = self._pnputil("/remove-device", iid)
            log(f"{side} stick: {'removed WinUSB' if r.returncode == 0 else 'remove failed: ' + r.stdout.strip()}")
        self._pnputil("/scan-devices")
        return len(targets)


_DRIVER = None


def driver_setup():
    """Shared DriverSetup instance, or None off Windows / on failure."""
    global _DRIVER
    if _DRIVER is None and IS_WIN:
        try:
            _DRIVER = DriverSetup()
        except Exception:
            _DRIVER = False
    return _DRIVER or None


def run_driver_cli(action):
    """--install-driver / --uninstall-driver entry point (run elevated)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []

    def log(msg):
        lines.append(msg)

    code = 0
    try:
        ds = driver_setup()
        if ds is None:
            raise RuntimeError("driver setup is only available on 64-bit Windows")
        (ds.install if action == "install" else ds.uninstall)(log)
    except Exception as e:
        log(f"ERROR: {e}")
        code = 1
    try:
        (CONFIG_DIR / "driver-setup.log").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n") + "\n".join(lines) + "\n")
    except Exception:
        pass
    return code


def launch_elevated(arg):
    """Re-run this app with admin rights for driver setup. Returns False if the user declined."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, arg
    else:
        exe, params = sys.executable, f'"{Path(__file__).resolve()}" {arg}'
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0) > 32


# ============================================================================
# Config & profiles
# ============================================================================

APPDATA = Path(os.environ.get("APPDATA", Path.home()))
CONFIG_DIR = APPDATA / "SolR-LED"
CONFIG_PATH = CONFIG_DIR / "config.json"
ICON_PATH = CONFIG_DIR / "icon.ico"
STARTUP_CMD = APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SolR-LED.cmd"
INSTANCE_PORT = 47612


def new_profile(color="#ff6a00", enabled=True):
    return {
        "enabled": enabled, "brightness": 100, "triggers": [], "effect": "static", "speed": 40,
        "left": {f"{i:02X}": color for i in ALL_LEDS},
        "right": {f"{i:02X}": color for i in ALL_LEDS},
    }


def default_config():
    return {
        "version": 2,
        "profiles": {"Lights off": new_profile(enabled=False), "Default": new_profile()},
        "idle": "Lights off", "active": "Default",
        "link": True, "auto": True, "recent": [], "smoothness": "Balanced",
    }


def normalize_profile(p):
    base = new_profile()
    for k in ("enabled", "brightness", "triggers", "effect", "speed"):
        if k in p:
            base[k] = p[k]
    if base["effect"] not in EFFECT_IDS:
        base["effect"] = "static"
    for side in ("left", "right"):
        base[side].update({k.upper(): v.lower() for k, v in p.get(side, {}).items()})
    base["triggers"] = [t.lower() for t in base["triggers"]]
    return base


def load_config():
    cfg = default_config()
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return cfg
    if "profiles" not in data and "left" in data:  # v1 (single-scheme) config
        cfg["profiles"]["Default"] = normalize_profile(data)
        cfg["profiles"]["Default"]["enabled"] = True
        cfg["link"] = data.get("link", True)
        return cfg
    if data.get("profiles"):
        cfg["profiles"] = {n: normalize_profile(p) for n, p in data["profiles"].items()}
    for k in ("idle", "active", "link", "auto", "recent", "smoothness"):
        if k in data:
            cfg[k] = data[k]
    if cfg["smoothness"] not in SMOOTHNESS:
        cfg["smoothness"] = "Balanced"
    names = list(cfg["profiles"])
    if cfg["idle"] not in cfg["profiles"]:
        cfg["idle"] = names[0]
    if cfg["active"] not in cfg["profiles"]:
        cfg["active"] = names[0]
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)


def profile_payload(prof, side, leds=None):
    s = max(0, min(100, prof["brightness"])) / 100 if prof["enabled"] else 0
    out = {}
    for i in (ALL_LEDS if leds is None else leds):
        r, g, b = bytes.fromhex(prof[side][f"{i:02X}"][1:])
        out[i] = bytes([int(r * s), int(g * s), int(b * s)])
    return out


# ============================================================================
# Windows helpers
# ============================================================================

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260),
    ]


def running_exes():
    """Lower-case exe names of all running processes (Toolhelp32, no extra deps)."""
    if not IS_WIN:
        return set()
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return set()
    names = set()
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        names.add(entry.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return names


def launch_command(arg):
    if getattr(sys, "frozen", False):
        return f'@start "" "{sys.executable}" {arg}\r\n'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'@start "" "{pyw}" "{Path(__file__).resolve()}" {arg}\r\n'


def claim_single_instance():
    """Returns a listening socket if we're the first instance; otherwise pokes
    the running instance to show itself and returns None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        s.listen(2)
        return s
    except OSError:
        s.close()
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None


# ============================================================================
# Visual helpers
# ============================================================================

BG = "#0a0c10"
SURFACE = "#11141a"
SURFACE2 = "#181c24"
SURFACE3 = "#212632"
BORDER = "#232835"
TEXT = "#e9ecf2"
MUTED = "#7c8595"
ACCENT = "#2f9bff"
ACCENT_DIM = "#173450"
OK = "#27d980"
ERR = "#ff5566"


def mix(c1, c2, t):
    a, b = bytes.fromhex(c1[1:]), bytes.fromhex(c2[1:])
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


def hsv_hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def hex_hsv(hx):
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)


def valid_hex(s):
    s = s.strip().lstrip("#")
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return "#" + s.lower()
    return None


def rounded_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
           x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def make_icon_image(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(17, 20, 26, 255))
    w = max(3, size // 9)
    m = size * 0.2
    d.arc((m, m, size - m, size - m), start=0, end=360, fill=(47, 155, 255, 255), width=w)
    d.arc((m, m, size - m, size - m), start=200, end=250, fill=(255, 106, 0, 255), width=w)
    c = size / 2
    d.ellipse((c - size * .1, c - size * .1, c + size * .1, c + size * .1), fill=(233, 236, 242, 255))
    return img


# ============================================================================
# Widgets
# ============================================================================

class ColorWheel(tk.Canvas):
    """Hue (angle) / saturation (radius) wheel."""

    def __init__(self, master, radius, bg, command):
        pad = 6
        size = radius * 2 + pad * 2
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, cursor="crosshair")
        self.r, self.c, self.command = radius, size / 2, command
        self.h = self.s = 0.0
        self._img = ImageTk.PhotoImage(self._render(radius, bg))
        self.create_image(self.c, self.c, image=self._img)
        self.m_outer = self.create_oval(0, 0, 0, 0, outline="#000000", width=4)
        self.m_inner = self.create_oval(0, 0, 0, 0, outline="#ffffff", width=2)
        self.bind("<Button-1>", lambda e: self._pick(e, False))
        self.bind("<B1-Motion>", lambda e: self._pick(e, False))
        self.bind("<ButtonRelease-1>", lambda e: self._pick(e, True))
        self._place()

    @staticmethod
    def _render(r, bg):
        ss = 2
        n = r * 2 * ss
        c = n / 2
        bgc = tuple(bytes.fromhex(bg[1:]))
        data = bytearray()
        hsv = colorsys.hsv_to_rgb
        for y in range(n):
            dy = c - y - 0.5
            for x in range(n):
                dx = x + 0.5 - c
                d = math.hypot(dx, dy)
                if d <= c:
                    rr, gg, bb = hsv((math.atan2(dy, dx) / (2 * math.pi)) % 1, d / c, 1)
                    data += bytes((int(rr * 255), int(gg * 255), int(bb * 255)))
                else:
                    data += bytes(bgc)
        img = Image.frombytes("RGB", (n, n), bytes(data))
        return img.resize((r * 2, r * 2), Image.LANCZOS)

    def _pick(self, e, final):
        dx, dy = e.x - self.c, self.c - e.y
        d = min(math.hypot(dx, dy), self.r)
        self.h = (math.atan2(dy, dx) / (2 * math.pi)) % 1
        self.s = d / self.r
        self._place()
        self.command(self.h, self.s, final)

    def set_hs(self, h, s):
        self.h, self.s = h, s
        self._place()

    def _place(self):
        a = self.h * 2 * math.pi
        x = self.c + math.cos(a) * self.s * self.r
        y = self.c - math.sin(a) * self.s * self.r
        self.coords(self.m_outer, x - 8, y - 8, x + 8, y + 8)
        self.coords(self.m_inner, x - 8, y - 8, x + 8, y + 8)


class StickView(tk.Canvas):
    W, H = 340, 414
    RING_C, RING_R = (170, 262), 48
    RING_ANGLES = {0x04: 90, 0x05: 45, 0x06: 0, 0x0B: 315, 0x0C: 270, 0x0D: 225, 0x0E: 180, 0x0F: 135}
    BANKS = [(38, 238, [[0x11, 0x10], [0x12, 0x13]]), (240, 238, [[0x07, 0x08], [0x0A, 0x09]])]

    def __init__(self, master, app, side, k):
        super().__init__(master, width=int(self.W * k), height=int(self.H * k),
                         bg=SURFACE, highlightthickness=0)
        self.app, self.side, self.k = app, side, k
        self.leds = {}   # led -> (glow_id, core_id, prop)
        self.labels = []
        self._draw()

    # scaled primitives
    def _rr(self, x1, y1, x2, y2, r, **kw):
        k = self.k
        return rounded_rect(self, x1 * k, y1 * k, x2 * k, y2 * k, r * k, **kw)

    def _ov(self, cx, cy, r, **kw):
        k = self.k
        return self.create_oval((cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k, **kw)

    def _draw(self):
        k = self.k
        cx, cy = self.RING_C
        # base
        self._rr(14, 196, 326, 408, 30, fill="#151820", outline="#232833", width=2 * k)
        self._rr(24, 372, 316, 398, 13, fill="#cdd1d8", outline="")
        self.create_text(170 * k, 385 * k, text="S O L - R", fill="#626873",
                         font=("Segoe UI", max(7, int(8 * k)), "bold"))
        for x in (72, 268):
            self._ov(x, 318, 12, fill="#0c0e12", outline="#2a303b", width=2 * k)
            self.create_line(x * k, 309 * k, x * k, 316 * k, fill="#8b93a1", width=2 * k, capstyle="round")
        # gimbal & grip neck
        self._ov(cx, cy, 36, fill="#07080b", outline="#1d222b", width=2 * k)
        self._rr(152, 96, 188, 256, 13, fill="#cdd1d8", outline="")
        self.create_line(161 * k, 132 * k, 161 * k, 230 * k, fill="#1c2029", width=3 * k, capstyle="round")
        self._ov(cx, cy, 19, fill="#8a909a", outline="#6b717b", width=2 * k)
        self._ov(cx, cy, 11, fill="#b7bcc4", outline="")
        # ring LEDs
        R = self.RING_R
        for led, ang in self.RING_ANGLES.items():
            box = ((cx - R) * k, (cy - R) * k, (cx + R) * k, (cy + R) * k)
            g = self.create_arc(*box, start=ang - 19, extent=38, style="arc", width=17 * k)
            c = self.create_arc(*box, start=ang - 17, extent=34, style="arc", width=9 * k)
            self._reg_led(led, g, c, "outline")
        # grip head
        self._rr(100, 16, 240, 110, 36, fill="#191c24", outline="#2a303b", width=2 * k)
        for x in (131, 209):
            self._ov(x, 52, 13, fill="#232833", outline="#303643", width=k)
            self.create_text(x * k, 52 * k, text="✦", fill="#5f6673", font=("Segoe UI", max(6, int(7 * k))))
        for x in (146, 194):
            self._ov(x, 89, 7, fill="#c55a17", outline="")
        g = self._ov(170, 55, 17, outline="", width=12 * k)
        c = self._ov(170, 55, 17, outline="", width=5 * k)
        self._reg_led(0x00, g, c, "outline")
        self._ov(170, 55, 10, fill="#050608", outline="")
        # logo triangle
        apex, bl, br = (170, 330), (153, 359), (187, 359)
        for led, (a, b) in ((0x03, (apex, bl)), (0x02, (apex, br)), (0x01, (bl, br))):
            pts = (a[0] * k, a[1] * k, b[0] * k, b[1] * k)
            g = self.create_line(*pts, width=13 * k, capstyle="round")
            c = self.create_line(*pts, width=6 * k, capstyle="round")
            self._reg_led(led, g, c, "fill")
        # button banks
        for x0, y0, grid in self.BANKS:
            for r, row in enumerate(grid):
                for col, led in enumerate(row):
                    x, y = x0 + col * 34, y0 + r * 27
                    g = self._rr(x - 3, y - 3, x + 31, y + 24, 8, outline="")
                    c = self._rr(x, y, x + 28, y + 21, 6, outline="")
                    self._reg_led(led, g, c, "fill")
                    t = self.create_text((x + 14) * k, (y + 10.5) * k, text=LED_NAMES[led].split()[-1],
                                         font=("Segoe UI", max(6, int(7.5 * k)), "bold"), state="disabled")
                    self.labels.append(t)

    def _reg_led(self, led, glow, core, prop):
        self.leds[led] = (glow, core, prop)
        tag = f"led{led}"
        for it in (glow, core):
            self.addtag_withtag(tag, it)
        self.tag_bind(tag, "<Button-1>", lambda e, l=led: self.app.on_led_click(self.side, l, e.state))
        self.tag_bind(tag, "<Button-3>", lambda e, l=led: self.app.locate(self.side, l))
        self.tag_bind(tag, "<Enter>", lambda e, l=led: self._hover(l, True))
        self.tag_bind(tag, "<Leave>", lambda e, l=led: self._hover(l, False))

    def _hover(self, led, inside):
        self.config(cursor="hand2" if inside else "")
        self.app.on_led_hover(self.side, led if inside else None)

    def render(self, prof, selected, colors=None):
        on, b = prof["enabled"], prof["brightness"] / 100
        for led, (glow, core, prop) in self.leds.items():
            color = colors[led] if colors else prof[self.side][f"{led:02X}"]
            if on:
                core_c = mix(SURFACE, color, max(b, 0.35))
                glow_c = mix(SURFACE, color, 0.30 * b)
            else:
                core_c = mix(color, "#1a1e27", 0.85)
                glow_c = SURFACE
            if led in selected:
                glow_c = "#b9c3d4"
            self.itemconfig(core, **{prop: core_c})
            self.itemconfig(glow, **{prop: glow_c})
        lbl = "#0a0c10" if on and b > 0.45 else "#6b7383"
        for t in self.labels:
            self.itemconfig(t, fill=lbl)


# ============================================================================
# App
# ============================================================================

class App:
    def __init__(self, start_hidden, instance_sock):
        self.cfg = load_config()
        self.io = SolRIO() if USB_OK else None
        self.jobs = queue.Queue()
        self.ui_q = queue.Queue()
        self.status = {"left": ("missing", "Checking…"), "right": ("missing", "Checking…")}
        self.sel = {(s, l) for s in ("left", "right") for l in ALL_LEDS}
        self.target = "Both"
        self.auto_reason = None
        self.h, self.s, self.v = 0.07, 1.0, 1.0
        self._save_job = self._bright_job = None
        self._identifying = False
        self.tray = None
        self._sync_watch_state()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(APP_NAME)
        self.root.geometry("1300x840")
        self.root.minsize(1180, 740)
        try:
            self.scale = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            self.scale = 1.0
        self.F = lambda size, bold=False: ctk.CTkFont(family="Segoe UI", size=size,
                                                      weight="bold" if bold else "normal")
        self._set_window_icon()
        self._build()
        self._fix_startup_entry()
        self.refresh_everything()
        self._load_wheel_from_selection()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if start_hidden and TRAY_OK:
            self.root.withdraw()

        if instance_sock:
            threading.Thread(target=self._instance_listener, args=(instance_sock,), daemon=True).start()
        if USB_OK:
            threading.Thread(target=self._usb_worker, daemon=True).start()
            self.jobs.put(("probe", True, None))
        threading.Thread(target=self._watcher, daemon=True).start()
        threading.Thread(target=self._animator, daemon=True).start()
        self.root.after(66, self._anim_ui)
        if TRAY_OK:
            self._start_tray()
        self.root.after(100, self._pump)

    # ------------------------------------------------------------------ build
    def _card(self, parent, **kw):
        return ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=16, border_width=1,
                            border_color=BORDER, **kw)

    def _ghost_btn(self, parent, text, cmd, width=0, **kw):
        return ctk.CTkButton(parent, text=text, command=cmd, width=width, height=32, corner_radius=10,
                             fg_color=SURFACE2, hover_color=SURFACE3, text_color=TEXT,
                             border_width=1, border_color=BORDER, font=self.F(12), **kw)

    def _section(self, parent, text):
        return ctk.CTkLabel(parent, text=text.upper(), font=self.F(11, True), text_color=MUTED)

    def _set_window_icon(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            make_icon_image(256).save(ICON_PATH, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
            if IS_WIN:
                self.root.after(300, self._apply_icon)
        except Exception:
            pass

    def _apply_icon(self):
        try:
            self.root.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

    def _build(self):
        r = self.root
        r.grid_columnconfigure(1, weight=1)
        r.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_center()
        self._build_inspector()

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self.root, fg_color=SURFACE, corner_radius=0, width=236,
                          border_width=0)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        sb.grid_rowconfigure(3, weight=1)

        brand = ctk.CTkFrame(sb, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=20, pady=(22, 18))
        self._brand_icon = ctk.CTkImage(make_icon_image(96), size=(34, 34))
        ctk.CTkLabel(brand, image=self._brand_icon, text="").pack(side="left")
        tb = ctk.CTkFrame(brand, fg_color="transparent")
        tb.pack(side="left", padx=10)
        ctk.CTkLabel(tb, text="SOL-R", font=self.F(18, True), text_color=TEXT, height=20).pack(anchor="w")
        ctk.CTkLabel(tb, text="LED Studio", font=self.F(12), text_color=ACCENT, height=16).pack(anchor="w")

        self._section(sb, "Profiles").grid(row=1, column=0, sticky="w", padx=22, pady=(4, 6))
        ctk.CTkButton(sb, text="＋  New profile", command=self.new_profile, height=34, corner_radius=10,
                      fg_color="transparent", hover_color=SURFACE2, border_width=1, border_color=BORDER,
                      text_color=TEXT, font=self.F(12), anchor="w").grid(row=2, column=0, sticky="ew",
                                                                          padx=14, pady=(0, 8))
        self.profile_list = ctk.CTkScrollableFrame(sb, fg_color="transparent", corner_radius=0,
                                                   scrollbar_button_color=SURFACE, scrollbar_button_hover_color=SURFACE3)
        self.profile_list.grid(row=3, column=0, sticky="nsew", padx=6)

        bottom = ctk.CTkFrame(sb, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="ew", padx=18, pady=(8, 18))
        bottom.grid_columnconfigure(0, weight=1)

        self._section(bottom, "Automation").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.auto_sw = ctk.CTkSwitch(bottom, text="Switch profiles for games", font=self.F(12),
                                     text_color=TEXT, progress_color=ACCENT, command=self.on_auto)
        self.auto_sw.grid(row=1, column=0, sticky="w", pady=3)
        ctk.CTkLabel(bottom, text="When no game is running", font=self.F(11), text_color=MUTED,
                     height=18).grid(row=2, column=0, sticky="w", pady=(8, 2))
        self.idle_menu = ctk.CTkOptionMenu(bottom, values=["-"], command=self.on_idle, height=30,
                                           corner_radius=10, fg_color=SURFACE2, button_color=SURFACE3,
                                           button_hover_color=BORDER, dropdown_fg_color=SURFACE2,
                                           text_color=TEXT, font=self.F(12), dropdown_font=self.F(12))
        self.idle_menu.grid(row=3, column=0, sticky="ew")
        self.startup_sw = ctk.CTkSwitch(bottom, text="Start with Windows", font=self.F(12),
                                        text_color=TEXT, progress_color=ACCENT, command=self.on_startup)
        self.startup_sw.grid(row=4, column=0, sticky="w", pady=(12, 3))
        if not IS_WIN:
            self.startup_sw.configure(state="disabled")

        self._section(bottom, "Devices").grid(row=5, column=0, sticky="w", pady=(16, 6))
        self.dev_rows = {}
        for i, side in enumerate(("left", "right")):
            row = ctk.CTkFrame(bottom, fg_color=SURFACE2, corner_radius=10)
            row.grid(row=6 + i, column=0, sticky="ew", pady=2)
            dot = ctk.CTkLabel(row, text="●", font=self.F(12), text_color=MUTED, width=14)
            dot.pack(side="left", padx=(10, 4), pady=6)
            ctk.CTkLabel(row, text=f"{side.title()} stick", font=self.F(12), text_color=TEXT).pack(side="left")
            msg = ctk.CTkLabel(row, text="", font=self.F(11), text_color=MUTED)
            msg.pack(side="right", padx=10)
            self.dev_rows[side] = (dot, msg)
        self.setup_box = ctk.CTkFrame(bottom, fg_color="#2a2112", corner_radius=10, border_width=1,
                                      border_color="#5a4418")
        self.setup_box.grid(row=8, column=0, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(self.setup_box, text="LED driver not set up yet.\nOne-time setup, needs admin.",
                     font=self.F(11), text_color="#e8c27a", justify="left", anchor="w").pack(fill="x", padx=10, pady=(8, 4))
        self.setup_btn = ctk.CTkButton(self.setup_box, text="Set up LED driver", command=self.run_driver_setup,
                                       height=30, corner_radius=8, fg_color="#d08a1c", hover_color="#b67512",
                                       text_color="#111", font=self.F(12, True))
        self.setup_btn.pack(fill="x", padx=10, pady=(0, 10))
        self.setup_box.grid_remove()
        ctk.CTkButton(bottom, text="Rescan devices", command=lambda: self.jobs.put(("probe", True, None)),
                      height=28, corner_radius=8, fg_color="transparent", hover_color=SURFACE2,
                      text_color=MUTED, font=self.F(11)).grid(row=9, column=0, sticky="ew", pady=(6, 0))

    def _build_center(self):
        c = ctk.CTkFrame(self.root, fg_color="transparent")
        c.grid(row=0, column=1, sticky="nsew", padx=18, pady=18)
        c.grid_columnconfigure(0, weight=1)
        c.grid_rowconfigure(1, weight=1)

        # header
        head = ctk.CTkFrame(c, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(2, 14))
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        self.title_lbl = ctk.CTkLabel(left, text="", font=self.F(24, True), text_color=TEXT, height=30)
        self.title_lbl.pack(anchor="w")
        self.mode_pill = ctk.CTkLabel(left, text="", font=self.F(11, True), text_color=MUTED,
                                      fg_color=SURFACE2, corner_radius=8, height=22, padx=10)
        self.mode_pill.pack(anchor="w", pady=(6, 0))

        right = ctk.CTkFrame(head, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        right.pack(side="right")
        ctk.CTkLabel(right, text="Lights", font=self.F(12, True), text_color=TEXT).pack(side="left", padx=(16, 8), pady=12)
        self.lights_sw = ctk.CTkSwitch(right, text="", width=46, progress_color=ACCENT, command=self.on_lights)
        self.lights_sw.pack(side="left")
        ctk.CTkFrame(right, fg_color=BORDER, width=1, height=26).pack(side="left", padx=14)
        ctk.CTkLabel(right, text="Brightness", font=self.F(12, True), text_color=TEXT).pack(side="left", padx=(0, 10))
        self.bright = ctk.CTkSlider(right, from_=5, to=100, width=170, progress_color=ACCENT,
                                    button_color=TEXT, button_hover_color="#ffffff", command=self.on_brightness)
        self.bright.pack(side="left")
        self.bright_lbl = ctk.CTkLabel(right, text="", font=self.F(12), text_color=MUTED, width=42)
        self.bright_lbl.pack(side="left", padx=(6, 14))

        # sticks card
        card = self._card(c)
        card.grid(row=1, column=0, sticky="nsew")
        card.grid_columnconfigure((0, 1), weight=1)
        card.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 0))
        ctk.CTkLabel(bar, text="Select", font=self.F(12, True), text_color=TEXT).pack(side="left", padx=(2, 8))
        for name in GROUPS:
            ctk.CTkButton(bar, text=name, width=0, height=28, corner_radius=14, fg_color=SURFACE2,
                          hover_color=SURFACE3, border_width=1, border_color=BORDER, text_color=TEXT,
                          font=self.F(12), command=lambda n=name: self.select_group(n)).pack(side="left", padx=2)

        self.views, self.stick_frames, self._visible = {}, {}, None
        k = 0.92 * self.scale
        for i, side in enumerate(("left", "right")):
            f = ctk.CTkFrame(card, fg_color="transparent")
            f.grid(row=1, column=i, pady=(10, 0))
            self.stick_frames[side] = f
            ctk.CTkLabel(f, text=f"{side.upper()} STICK", font=self.F(11, True), text_color=MUTED).pack(anchor="w", padx=6)
            v = StickView(f, self, side, k)
            v.pack()
            self.views[side] = v

        self.hint = ctk.CTkLabel(card, text="", font=self.F(11), text_color=MUTED, anchor="center")
        self.hint.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 0))
        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 12))
        self.target_seg = ctk.CTkSegmentedButton(foot, values=["Both", "Left", "Right"], command=self.on_target,
                                                 height=28, corner_radius=10, font=self.F(12),
                                                 fg_color=SURFACE2, selected_color=ACCENT,
                                                 selected_hover_color=ACCENT, unselected_color=SURFACE2,
                                                 unselected_hover_color=SURFACE3)
        self.target_seg.set("Both")
        self.target_seg.pack(side="right")
        ctk.CTkLabel(foot, text="Groups apply to", font=self.F(11), text_color=MUTED).pack(side="right", padx=(0, 8))
        self.link_sw = ctk.CTkSwitch(foot, text="Mirror sticks", font=self.F(12), text_color=TEXT, width=40,
                                     progress_color=ACCENT, command=self.on_link)
        self.link_sw.pack(side="right", padx=18)

        # triggers card
        tc = self._card(c)
        tc.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        tc.grid_columnconfigure(0, weight=1)
        th = ctk.CTkFrame(tc, fg_color="transparent")
        th.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        ctk.CTkLabel(th, text="Game triggers", font=self.F(14, True), text_color=TEXT).pack(side="left")
        ctk.CTkButton(th, text="＋  Add game .exe", command=self.add_trigger, height=30, corner_radius=10,
                      fg_color=ACCENT, hover_color="#1f86e6", font=self.F(12, True)).pack(side="right")
        self.trig_desc = ctk.CTkLabel(tc, text="", font=self.F(11), text_color=MUTED, anchor="w", justify="left")
        self.trig_desc.grid(row=1, column=0, sticky="ew", padx=18)
        self.trig_box = ctk.CTkFrame(tc, fg_color="transparent", height=1)
        self.trig_box.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 14))

    def _build_inspector(self):
        ins = ctk.CTkFrame(self.root, fg_color="transparent", width=316)
        ins.grid(row=0, column=2, sticky="nse", padx=(0, 18), pady=18)
        ins.grid_propagate(False)
        ins.grid_columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(ins, height=560, fg_color=SURFACE, corner_radius=16, border_width=1,
                                   border_color=BORDER, segmented_button_fg_color=SURFACE2,
                                   segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color=ACCENT,
                                   segmented_button_unselected_color=SURFACE2,
                                   segmented_button_unselected_hover_color=SURFACE3,
                                   text_color=TEXT, anchor="n")
        self.tabs.grid(row=0, column=0, sticky="ew")
        cc = self.tabs.add("Color")
        ec = self.tabs.add("Effect")
        self.tabs._segmented_button.configure(font=self.F(12, True))
        cc.grid_columnconfigure(0, weight=1)
        hr = ctk.CTkFrame(cc, fg_color="transparent")
        hr.grid(row=0, column=0, sticky="ew", padx=10, pady=(2, 0))
        self.sel_lbl = ctk.CTkLabel(hr, text="", font=self.F(11), text_color=MUTED)
        self.sel_lbl.pack(side="right")
        self.color_note = ctk.CTkLabel(hr, text="", font=self.F(11), text_color="#e0a040")
        self.color_note.pack(side="left")

        self.wheel = ColorWheel(cc, int(118 * self.scale), SURFACE, self.on_wheel)
        self.wheel.grid(row=1, column=0, pady=(0, 8))

        vr = ctk.CTkFrame(cc, fg_color="transparent")
        vr.grid(row=2, column=0, sticky="ew", padx=10)
        ctk.CTkLabel(vr, text="Value", font=self.F(11, True), text_color=MUTED, width=44, anchor="w").pack(side="left")
        self.value = ctk.CTkSlider(vr, from_=0, to=100, progress_color=ACCENT, button_color=TEXT,
                                   button_hover_color="#ffffff", command=self.on_value)
        self.value.pack(side="left", fill="x", expand=True)

        hx = ctk.CTkFrame(cc, fg_color="transparent")
        hx.grid(row=3, column=0, sticky="ew", padx=10, pady=(12, 4))
        self.preview = ctk.CTkFrame(hx, width=40, height=40, corner_radius=12, fg_color="#ff6a00",
                                    border_width=1, border_color=BORDER)
        self.preview.pack(side="left")
        self.hex_entry = ctk.CTkEntry(hx, height=40, corner_radius=12, fg_color=SURFACE2, border_color=BORDER,
                                      text_color=TEXT, font=ctk.CTkFont(family="Consolas", size=14))
        self.hex_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.hex_entry.bind("<Return>", self.on_hex)
        self.hex_entry.bind("<FocusOut>", self.on_hex)

        self._section(cc, "Presets").grid(row=4, column=0, sticky="w", padx=10, pady=(12, 4))
        pr = ctk.CTkFrame(cc, fg_color="transparent")
        pr.grid(row=5, column=0, sticky="w", padx=6)
        for col in PRESETS + ["#ffffff"]:
            self._swatch(pr, col).pack(side="left", padx=2)
        self._section(cc, "Recent").grid(row=6, column=0, sticky="w", padx=10, pady=(12, 4))
        self.recent_row = ctk.CTkFrame(cc, fg_color="transparent", height=30)
        self.recent_row.grid(row=7, column=0, sticky="w", padx=6, pady=(0, 6))

        # effect tab
        ec.grid_columnconfigure(0, weight=1)
        self._section(ec, "Mode").grid(row=0, column=0, sticky="w", padx=10, pady=(2, 6))
        self.effect_btns = {}
        for i, (eid, icon, name, _desc, _uses) in enumerate(EFFECTS):
            b = ctk.CTkButton(ec, text=f"  {icon}    {name}", anchor="w", height=38, corner_radius=10,
                              fg_color=SURFACE2, hover_color=SURFACE3, border_width=1, border_color=BORDER,
                              text_color=TEXT, font=self.F(13), command=lambda e=eid: self.set_effect(e))
            b.grid(row=1 + i, column=0, sticky="ew", padx=8, pady=3)
            self.effect_btns[eid] = b
        n = len(EFFECTS)
        self.effect_desc = ctk.CTkLabel(ec, text="", font=self.F(11), text_color=MUTED, justify="left",
                                        anchor="w", wraplength=int(260 * self.scale))
        self.effect_desc.grid(row=n + 1, column=0, sticky="ew", padx=10, pady=(10, 0))
        sp = ctk.CTkFrame(ec, fg_color="transparent")
        sp.grid(row=n + 2, column=0, sticky="ew", padx=10, pady=(14, 0))
        ctk.CTkLabel(sp, text="Speed", font=self.F(11, True), text_color=MUTED, width=44, anchor="w").pack(side="left")
        self.speed = ctk.CTkSlider(sp, from_=1, to=100, progress_color=ACCENT, button_color=TEXT,
                                   button_hover_color="#ffffff", command=self.on_speed)
        self.speed.pack(side="left", fill="x", expand=True)
        self.speed_row = sp

        sm = ctk.CTkFrame(ec, fg_color="transparent")
        sm.grid(row=n + 3, column=0, sticky="ew", padx=10, pady=(14, 0))
        self._section(sm, "Smoothness").pack(anchor="w")
        self.smooth_seg = ctk.CTkSegmentedButton(sm, values=list(SMOOTHNESS), command=self.on_smoothness,
                                                 height=28, corner_radius=10, font=self.F(12),
                                                 fg_color=SURFACE2, selected_color=ACCENT,
                                                 selected_hover_color=ACCENT, unselected_color=SURFACE2,
                                                 unselected_hover_color=SURFACE3)
        self.smooth_seg.pack(fill="x", pady=(6, 4))
        ctk.CTkLabel(sm, text="Fewer updates = calmer D1/D2 activity lights,\nbut choppier motion on the sticks.",
                     font=self.F(11), text_color=MUTED, justify="left", anchor="w").pack(anchor="w")
        self.smooth_row = sm

        pc = self._card(ins)
        pc.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        pc.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(pc, text="Profile", font=self.F(14, True), text_color=TEXT).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(14, 8))
        self._ghost_btn(pc, "Rename", self.rename_profile).grid(row=1, column=0, sticky="ew", padx=(18, 4))
        self._ghost_btn(pc, "Duplicate", self.duplicate_profile).grid(row=1, column=1, sticky="ew", padx=(4, 18))
        self._ghost_btn(pc, "Re-send to sticks", lambda: self.push_all(force=True)).grid(row=2, column=0, sticky="ew", padx=(18, 4), pady=(8, 16))
        ctk.CTkButton(pc, text="Delete", command=self.delete_profile, height=32, corner_radius=10,
                      fg_color="transparent", hover_color="#3a1a20", text_color=ERR, border_width=1,
                      border_color="#4a2027", font=self.F(12)).grid(row=2, column=1, sticky="ew",
                                                                     padx=(4, 18), pady=(8, 16))

    def _swatch(self, parent, color):
        return ctk.CTkButton(parent, text="", width=24, height=24, corner_radius=8, fg_color=color,
                             hover_color=mix(color, "#ffffff", 0.2), border_width=1, border_color=BORDER,
                             command=lambda: self.set_color(color, commit=True, sync_wheel=True))

    # ---------------------------------------------------------------- helpers
    def prof(self):
        return self.cfg["profiles"][self.cfg["active"]]

    def effective_sel(self):
        sel = set(self.sel)
        if self.cfg["link"]:
            sel |= {("right" if s == "left" else "left", l) for s, l in self.sel}
        return sel

    def schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, self._save_now)

    def _save_now(self):
        self._save_job = None
        try:
            save_config(self.cfg)
        except Exception as e:
            self.hint.configure(text=f"Couldn't save settings: {e}")

    def _sync_watch_state(self):
        self.watch_auto = self.cfg["auto"]
        self.watch_idle = self.cfg["idle"]
        self.watch_map = [(exe, name) for name, p in self.cfg["profiles"].items() for exe in p["triggers"]]

    # -------------------------------------------------------------- rendering
    def refresh_everything(self):
        self.refresh_effect()
        self.refresh_sidebar()
        self.refresh_header()
        self.render_sticks()
        self.refresh_triggers()
        self.refresh_recent()
        self.refresh_devices()
        self.link_sw.select() if self.cfg["link"] else self.link_sw.deselect()
        self.auto_sw.select() if self.cfg["auto"] else self.auto_sw.deselect()
        self.startup_sw.select() if STARTUP_CMD.exists() else self.startup_sw.deselect()
        self._update_hint()

    def refresh_sidebar(self):
        for w in self.profile_list.winfo_children():
            w.destroy()
        for name, p in self.cfg["profiles"].items():
            active = name == self.cfg["active"]
            row = ctk.CTkFrame(self.profile_list, fg_color=ACCENT_DIM if active else "transparent",
                               corner_radius=10, border_width=1 if active else 0, border_color=ACCENT)
            row.pack(fill="x", pady=2, padx=2)
            dots = tk.Canvas(row, width=34, height=14, bg=ACCENT_DIM if active else SURFACE,
                             highlightthickness=0)
            dots.pack(side="left", padx=(10, 6), pady=10)
            if p["enabled"]:
                for i, led in enumerate((0x04, 0x00, 0x11)):
                    col = p["left"][f"{led:02X}"]
                    dots.create_oval(i * 11, 2, i * 11 + 10, 12, fill=col, outline="")
            else:
                dots.create_oval(0, 2, 10, 12, outline="#4a5160", width=1)
                dots.create_line(2, 11, 9, 3, fill="#4a5160")
            lbl = ctk.CTkLabel(row, text=name, font=self.F(13, active), text_color=TEXT, anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            tags = []
            if p["triggers"]:
                tags.append(f"⚡{len(p['triggers'])}")
            if name == self.cfg["idle"]:
                tags.append("idle")
            if tags:
                ctk.CTkLabel(row, text="  ".join(tags), font=self.F(10, True), text_color=ACCENT if active else MUTED
                             ).pack(side="right", padx=10)
            for w in (row, lbl, dots):
                w.bind("<Button-1>", lambda e, n=name: self.activate(n, manual=True))
        names = list(self.cfg["profiles"])
        self.idle_menu.configure(values=names)
        self.idle_menu.set(self.cfg["idle"])

    def refresh_effect(self):
        p = self.prof()
        cur = p.get("effect", "static")
        for eid, b in self.effect_btns.items():
            on = eid == cur
            b.configure(fg_color=ACCENT_DIM if on else SURFACE2, border_color=ACCENT if on else BORDER,
                        font=self.F(13, on))
        info = next(e for e in EFFECTS if e[0] == cur)
        extra = "" if cur == "static" else ("  Uses your LED colors." if info[4] else "  Replaces your LED colors while active.")
        self.effect_desc.configure(text=info[3] + extra)
        self.speed.set(p.get("speed", 40))
        self.smooth_seg.set(self.cfg["smoothness"])
        if cur == "static":
            self.speed_row.grid_remove()
            self.smooth_row.grid_remove()
        else:
            self.speed_row.grid()
            self.smooth_row.grid()
        self.color_note.configure(text="Effect overrides colors" if (cur in ("spectrum", "rainbow")) else "")

    def set_effect(self, eid):
        self.prof()["effect"] = eid
        self.refresh_effect()
        self.refresh_header()
        self.refresh_sidebar()
        self.render_sticks()
        if eid == "static":
            self.push_all(force=True)
        self.schedule_save()

    def on_smoothness(self, value):
        self.cfg["smoothness"] = value
        self.schedule_save()

    def on_speed(self, v):
        self.prof()["speed"] = int(v)
        self.schedule_save()

    def _anim_ui(self):
        try:
            if self.animating() and self.root.state() != "withdrawn":
                self.render_sticks()
        except Exception:
            pass
        self.root.after(66, self._anim_ui)

    def _animator(self):
        """Streams effect frames to the sticks (runs even when hidden in the tray).
        Only LEDs that visibly changed since they were last sent go out, and the
        frame rate follows the Smoothness setting - every packet makes the
        stick's D1/D2 activity lights blink."""
        sent = {"left": {}, "right": {}}
        state_key = None
        while True:
            try:
                if self.io and self.animating():
                    prof = self.prof()
                    key = (self.cfg["active"], prof.get("effect"), prof["brightness"],
                           self.status["left"][0], self.status["right"][0])
                    if key != state_key:  # profile/effect/brightness/connection changed: resync
                        state_key = key
                        sent = {"left": {}, "right": {}}
                    frame = effect_frame(prof, time.time())
                    for side in ("left", "right"):
                        if self.status[side][0] != "ok":
                            continue
                        last = sent[side]
                        changed = {}
                        for led, c in frame_payload(frame[side], prof).items():
                            prev = last.get(led)
                            if prev is None or max(abs(a - b) for a, b in zip(c, prev)) >= CHANGE_THRESHOLD:
                                changed[led] = c
                        if changed:
                            last.update(changed)
                            self.jobs.put(("frame", side, changed))
                    time.sleep(1 / SMOOTHNESS.get(self.cfg.get("smoothness"), 5))
                    continue
                state_key = None
            except Exception:
                pass
            time.sleep(0.1)

    def refresh_header(self):
        p = self.prof()
        self.title_lbl.configure(text=self.cfg["active"])
        if self.cfg["auto"] and self.auto_reason:
            self.mode_pill.configure(text=f"AUTO  ·  {self.auto_reason}", text_color=OK)
        elif self.cfg["auto"] and self.cfg["active"] == self.cfg["idle"]:
            self.mode_pill.configure(text="IDLE PROFILE", text_color=MUTED)
        else:
            self.mode_pill.configure(text="MANUAL", text_color=MUTED)
        eff = p.get("effect", "static")
        if eff != "static":
            name = next(e[2] for e in EFFECTS if e[0] == eff)
            self.mode_pill.configure(text=self.mode_pill.cget("text") + f"  ·  {name.upper()}")
        self.lights_sw.select() if p["enabled"] else self.lights_sw.deselect()
        self.bright.set(p["brightness"])
        self.bright_lbl.configure(text=f"{p['brightness']}%")

    def animating(self):
        p = self.prof()
        return p["enabled"] and p.get("effect", "static") != "static"

    def render_sticks(self, frame=None):
        sel = self.effective_sel()
        if frame is None and self.animating():
            frame = {s: frame_hex(f) for s, f in effect_frame(self.prof(), time.time()).items()}
        for side, v in self.views.items():
            v.render(self.prof(), {l for s, l in sel if s == side}, frame[side] if frame else None)
        n = len(sel)
        self.sel_lbl.configure(text=f"{n} LED{'s' if n != 1 else ''} selected")

    def refresh_triggers(self):
        for w in self.trig_box.winfo_children():
            w.destroy()
        p = self.prof()
        name = self.cfg["active"]
        if p["triggers"]:
            self.trig_desc.configure(text=f"“{name}” turns on automatically while any of these are running.")
        else:
            self.trig_desc.configure(text=f"Add a game's .exe to switch to “{name}” automatically while it runs.")
        for i, exe in enumerate(p["triggers"]):
            chip = ctk.CTkFrame(self.trig_box, fg_color=SURFACE2, corner_radius=14, border_width=1, border_color=BORDER)
            chip.grid(row=i // 4, column=i % 4, padx=4, pady=4, sticky="w")
            ctk.CTkLabel(chip, text=exe, font=self.F(12), text_color=TEXT).pack(side="left", padx=(12, 4), pady=4)
            ctk.CTkButton(chip, text="✕", width=22, height=22, corner_radius=11, fg_color="transparent",
                          hover_color=SURFACE3, text_color=MUTED, font=self.F(11),
                          command=lambda e=exe: self.remove_trigger(e)).pack(side="left", padx=(0, 6))

    def refresh_recent(self):
        for w in self.recent_row.winfo_children():
            w.destroy()
        recent = self.cfg.get("recent", [])[:9]
        if not recent:
            ctk.CTkLabel(self.recent_row, text="Colors you use will show up here", font=self.F(11),
                         text_color="#4f5664").pack(side="left", padx=4)
        for col in recent:
            self._swatch(self.recent_row, col).pack(side="left", padx=2)

    def refresh_devices(self):
        for side, (dot, msg) in self.dev_rows.items():
            state, text = self.status[side]
            dot.configure(text_color={"ok": OK, "error": ERR, "setup": "#e0a040"}.get(state, MUTED))
            msg.configure(text=text if state != "ok" else "Connected")
        if any(st[0] == "setup" for st in self.status.values()):
            self.setup_box.grid()
        else:
            self.setup_box.grid_remove()
        self._layout_sticks()

    def run_driver_setup(self):
        if not IS_WIN:
            return
        if not launch_elevated("--install-driver"):
            self._update_hint("Driver setup was cancelled.")
            return
        self.setup_btn.configure(state="disabled", text="Setting up…")

        def wait():
            ds = driver_setup()
            deadline = time.time() + 60
            while time.time() < deadline:
                time.sleep(1.5)
                try:
                    st = ds.state() if ds else {}
                    if st and all(v == "ok" for v in st.values()):
                        break
                except Exception:
                    pass
            time.sleep(1.5)  # let the restarted interfaces come back
            self.jobs.put(("probe", True, None))
            self.ui_q.put(("setup_done",))
        threading.Thread(target=wait, daemon=True).start()

    def _layout_sticks(self):
        """Show only connected sticks; if neither is connected, show both so
        profiles can still be edited offline."""
        visible = tuple(s for s in ("left", "right") if self.status[s][0] != "missing") or ("left", "right")
        if visible == self._visible:
            return
        self._visible = visible
        for f in self.stick_frames.values():
            f.grid_forget()
        if len(visible) == 2:
            self.stick_frames["left"].grid(row=1, column=0, pady=(10, 0))
            self.stick_frames["right"].grid(row=1, column=1, pady=(10, 0))
        else:
            self.stick_frames[visible[0]].grid(row=1, column=0, columnspan=2, pady=(10, 0))

    def _update_hint(self, text=None):
        if text is None:
            text = ("Click to select  ·  Ctrl-click to add  ·  Right-click to identify"
                    if USB_OK else f"USB libraries failed to load: {USB_ERR}")
        self.hint.configure(text=text)

    # ------------------------------------------------------------- selection
    def on_led_click(self, side, led, state):
        if state & 0x4:  # Ctrl
            key = (side, led)
            self.sel.symmetric_difference_update({key})
            if not self.sel:
                self.sel = {key}
        else:
            self.sel = {(side, led)}
        self.render_sticks()
        self._load_wheel_from_selection(prefer=(side, led))

    def on_led_hover(self, side, led):
        if led is None:
            self._update_hint()
        else:
            col = self.prof()[side][f"{led:02X}"].upper()
            self._update_hint(f"{side.title()} stick  ·  {LED_NAMES[led]}  ·  {col}")

    def select_group(self, name):
        sides = {"Both": ("left", "right"), "Left": ("left",), "Right": ("right",)}[self.target]
        self.sel = {(s, l) for s in sides for l in GROUPS[name]}
        self.render_sticks()
        self._load_wheel_from_selection()

    def on_target(self, value):
        self.target = value

    def _load_wheel_from_selection(self, prefer=None):
        key = prefer or next(iter(sorted(self.effective_sel())), None)
        if not key:
            return
        col = self.prof()[key[0]][f"{key[1]:02X}"]
        h, s, v = hex_hsv(col)
        if s > 0:
            self.h = h
        self.s, self.v = s, v
        self.wheel.set_hs(self.h, self.s)
        self.value.set(v * 100)
        self._show_color(col)

    def _show_color(self, col):
        self.preview.configure(fg_color=col)
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, col.upper())

    # ----------------------------------------------------------------- color
    def on_wheel(self, h, s, final):
        self.h, self.s = h, s
        if self.v < 0.05:
            self.v = 1.0
            self.value.set(100)
        self.set_color(hsv_hex(h, s, self.v), commit=final)

    def on_value(self, v):
        self.v = v / 100
        self.set_color(hsv_hex(self.h, self.s, self.v), commit=False)

    def on_hex(self, _e=None):
        col = valid_hex(self.hex_entry.get())
        if col:
            self.set_color(col, commit=True, sync_wheel=True)

    def set_color(self, col, commit=False, sync_wheel=False):
        prof = self.prof()
        by_side = {}
        for side, led in self.effective_sel():
            prof[side][f"{led:02X}"] = col
            by_side.setdefault(side, []).append(led)
        for side, leds in by_side.items():
            self.push(side, leds)
        if sync_wheel:
            h, s, v = hex_hsv(col)
            if s > 0:
                self.h = h
            self.s, self.v = s, v
            self.wheel.set_hs(self.h, self.s)
            self.value.set(v * 100)
        self._show_color(col)
        self.render_sticks()
        if commit:
            rec = [c for c in self.cfg.get("recent", []) if c != col]
            self.cfg["recent"] = [col] + rec[:8]
            self.refresh_recent()
            self.refresh_sidebar()
        self.schedule_save()

    # ------------------------------------------------------- profile controls
    def on_lights(self):
        self.prof()["enabled"] = bool(self.lights_sw.get())
        self.render_sticks()
        self.refresh_sidebar()
        self.push_all()

    def on_brightness(self, v):
        self.prof()["brightness"] = int(v)
        self.bright_lbl.configure(text=f"{int(v)}%")
        self.render_sticks()
        if self._bright_job:
            self.root.after_cancel(self._bright_job)
        self._bright_job = self.root.after(120, self.push_all)

    def on_link(self):
        self.cfg["link"] = bool(self.link_sw.get())
        self.render_sticks()
        self.schedule_save()

    def activate(self, name, manual=False, reason=None):
        if name not in self.cfg["profiles"]:
            return
        self.cfg["active"] = name
        self.auto_reason = None if manual else reason
        self.refresh_sidebar()
        self.refresh_header()
        self.refresh_triggers()
        self.refresh_effect()
        self.render_sticks()
        self._load_wheel_from_selection()
        self.push_all(force=True)
        if self.tray:
            self.tray.update_menu()

    def _ask_name(self, title, initial=""):
        d = ctk.CTkInputDialog(text="Profile name", title=title, fg_color=SURFACE, button_fg_color=ACCENT,
                               entry_fg_color=SURFACE2, entry_border_color=BORDER)
        d.withdraw()  # hide until it's been moved, so it doesn't flash top-left
        d.after(40, lambda: self._center_on_app(d))
        name = (d.get_input() or "").strip()
        if not name:
            return None
        if name in self.cfg["profiles"] and name != initial:
            messagebox.showwarning(APP_NAME, f"A profile named “{name}” already exists.", parent=self.root)
            return None
        return name

    def _center_on_app(self, win):
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            x, y = rx + (rw - w) // 2, ry + (rh - h) // 2
            win.tk.call("wm", "geometry", win._w, f"+{max(x, 0)}+{max(y, 0)}")  # position only, unscaled
            win.deiconify()
            win.lift()
            win.focus_force()
        except Exception:
            pass

    def new_profile(self):
        name = self._ask_name("New profile")
        if not name:
            return
        p = json.loads(json.dumps(self.prof()))
        p["triggers"], p["enabled"] = [], True
        self.cfg["profiles"][name] = p
        self._after_profiles_changed()
        self.activate(name, manual=True)

    def duplicate_profile(self):
        base = self.cfg["active"]
        n, name = 2, f"{base} copy"
        while name in self.cfg["profiles"]:
            name, n = f"{base} copy {n}", n + 1
        p = json.loads(json.dumps(self.prof()))
        p["triggers"] = []
        self.cfg["profiles"][name] = p
        self._after_profiles_changed()
        self.activate(name, manual=True)

    def rename_profile(self):
        old = self.cfg["active"]
        new = self._ask_name("Rename profile", initial=old)
        if not new or new == old:
            return
        self.cfg["profiles"] = {(new if k == old else k): v for k, v in self.cfg["profiles"].items()}
        if self.cfg["idle"] == old:
            self.cfg["idle"] = new
        self.cfg["active"] = new
        self._after_profiles_changed()
        self.refresh_header()
        self.refresh_triggers()

    def delete_profile(self):
        name = self.cfg["active"]
        if len(self.cfg["profiles"]) == 1:
            messagebox.showinfo(APP_NAME, "You need at least one profile.", parent=self.root)
            return
        if not messagebox.askyesno(APP_NAME, f"Delete profile “{name}”?", parent=self.root):
            return
        del self.cfg["profiles"][name]
        first = next(iter(self.cfg["profiles"]))
        if self.cfg["idle"] == name:
            self.cfg["idle"] = first
        self._after_profiles_changed()
        self.activate(first, manual=True)

    def _after_profiles_changed(self):
        self._sync_watch_state()
        self.refresh_sidebar()
        self.schedule_save()
        if self.tray:
            self.tray.update_menu()

    def add_trigger(self):
        path = filedialog.askopenfilename(parent=self.root, title="Choose the game's .exe",
                                          filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if not path:
            return
        exe = Path(path).name.lower()
        for name, p in self.cfg["profiles"].items():
            if exe in p["triggers"] and name != self.cfg["active"]:
                if not messagebox.askyesno(APP_NAME, f"{exe} already triggers “{name}”. Move it to this profile?", parent=self.root):
                    return
                p["triggers"].remove(exe)
        if exe not in self.prof()["triggers"]:
            self.prof()["triggers"].append(exe)
        self.refresh_triggers()
        self._after_profiles_changed()

    def remove_trigger(self, exe):
        if exe in self.prof()["triggers"]:
            self.prof()["triggers"].remove(exe)
        self.refresh_triggers()
        self._after_profiles_changed()

    # ------------------------------------------------------------ automation
    def on_auto(self):
        self.cfg["auto"] = bool(self.auto_sw.get())
        if not self.cfg["auto"]:
            self.auto_reason = None
        self._sync_watch_state()
        self.refresh_header()
        self.schedule_save()

    def on_idle(self, name):
        self.cfg["idle"] = name
        self._after_profiles_changed()
        self.refresh_header()

    def on_startup(self):
        try:
            if self.startup_sw.get():
                STARTUP_CMD.write_text(launch_command("--tray"))
            elif STARTUP_CMD.exists():
                STARTUP_CMD.unlink()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't update startup entry:\n{e}", parent=self.root)
            self.startup_sw.select() if STARTUP_CMD.exists() else self.startup_sw.deselect()

    def _fix_startup_entry(self):
        """Upgrade the old '--apply' logon entry to the tray launcher."""
        try:
            if STARTUP_CMD.exists() and "--tray" not in STARTUP_CMD.read_text():
                STARTUP_CMD.write_text(launch_command("--tray"))
        except Exception:
            pass

    def _watcher(self):
        last = None
        while True:
            try:
                if self.watch_auto and IS_WIN:
                    running = running_exes()
                    target, why = self.watch_idle, None
                    for exe, name in self.watch_map:
                        if exe in running:
                            target, why = name, exe
                            break
                    if (target, why) != last:
                        last = (target, why)
                        self.ui_q.put(("auto", target, why))
                else:
                    last = None
            except Exception:
                pass
            time.sleep(2)

    # -------------------------------------------------------------------- USB
    def push(self, side, leds, force=False):
        if self.io and not self.animating():  # while animating, the animator owns the LEDs
            self.jobs.put(("force" if force else "write", side, profile_payload(self.prof(), side, leds)))

    def push_all(self, force=False):
        self.schedule_save()
        for side in ("left", "right"):
            if self.status[side][0] != "missing":
                self.push(side, None, force=force)

    def locate(self, side, led):
        if not self.io:
            return
        white, off = {led: b"\xff\xff\xff"}, {led: b"\x00\x00\x00"}
        seq = [white, off, white, off, white]
        self._identifying = True
        for i, payload in enumerate(seq):
            self.root.after(i * 220, lambda p=payload: self.jobs.put(("force", side, p)))

        def done():
            self._identifying = False
            self.push(side, [led], force=True)
        self.root.after(len(seq) * 220, done)

    def _set_status(self, side, state, msg):
        if self.status[side] != (state, msg):
            self.status[side] = (state, msg)
            self.ui_q.put(("devices",))

    def _check_presence(self):
        """Runs ~every 0.75 s: catches unplug/replug without waiting for a write."""
        try:
            present = self.io.present()
        except Exception:
            return []
        jobs = []
        for side in ("left", "right"):
            state = self.status[side][0]
            if side not in present and state != "missing":
                self.io.close(side)
                self._set_status(side, "missing", "Disconnected")
            elif side in present and state == "missing":
                jobs.append(("probe_side", side, None))
            elif side in present and state == "setup" and time.time() - getattr(self, "_last_setup_probe", 0) > 5:
                self._last_setup_probe = time.time()
                jobs.append(("probe_side", side, None))
        return jobs

    def _usb_worker(self):
        last_check = 0
        while True:
            try:
                jobs = [self.jobs.get(timeout=0.25)]
            except queue.Empty:
                jobs = []
            if time.time() - last_check > 0.75:
                last_check = time.time()
                jobs += self._check_presence()
            if not jobs:
                continue
            time.sleep(0.03)
            while True:
                try:
                    jobs.append(self.jobs.get_nowait())
                except queue.Empty:
                    break
            merged, forced = {}, set()
            for kind, a, b in jobs:
                if kind in ("probe", "probe_side"):
                    sides = ("left", "right") if kind == "probe" else (a,)
                    for side in sides:
                        state, msg = self.io.probe(side)
                        if state == "missing":
                            msg = "Disconnected"
                        self._set_status(side, state, msg)
                        if state == "ok":
                            merged.setdefault(side, {}).update(profile_payload(self.prof(), side))
                            forced.add(side)
                elif kind in ("write", "force", "frame"):
                    if self.status[a][0] == "missing":
                        continue
                    if kind == "frame" and (not self.animating() or self._identifying):
                        continue  # stale frame after switching back to static / during identify
                    merged.setdefault(a, {}).update(b)
                    if kind == "force":
                        forced.add(a)

            def send(side, payload):
                try:
                    self.io.write(side, payload, force=side in forced)
                    self._set_status(side, "ok", "Connected")
                except LookupError:
                    self._set_status(side, "missing", "Disconnected")
                except Exception as e:
                    self._set_status(side, "error", "Write failed")
                    self.ui_q.put(("hint", f"{side.title()} stick write failed: {e}"))

            threads = [threading.Thread(target=send, args=item) for item in merged.items()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    # ------------------------------------------------------------ tray & misc
    def _start_tray(self):
        def item_pick(name):
            return pystray.MenuItem(name, lambda icon, item: self.ui_q.put(("pick", name)),
                                    checked=lambda item: self.cfg["active"] == name, radio=True)

        menu = pystray.Menu(
            pystray.MenuItem("Open SOL-R LED Studio", lambda icon, item: self.ui_q.put(("show",)), default=True),
            pystray.MenuItem("Profile", pystray.Menu(lambda: tuple(item_pick(n) for n in list(self.cfg["profiles"])))),
            pystray.MenuItem("Lights on", lambda icon, item: self.ui_q.put(("toggle",)),
                             checked=lambda item: self.prof()["enabled"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: self.ui_q.put(("quit",))),
        )
        self.tray = pystray.Icon("SolR-LED", make_icon_image(64), APP_NAME, menu)
        self.tray.run_detached()

    def _instance_listener(self, sock):
        while True:
            try:
                conn, _ = sock.accept()
                conn.close()
                self.ui_q.put(("show",))
            except OSError:
                return

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def on_close(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        self._save_now()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.io:
            self.io.close_all()
        self.root.destroy()

    def _pump(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                kind = msg[0]
                if kind == "devices":
                    self.refresh_devices()
                elif kind == "hint":
                    self._update_hint(msg[1])
                elif kind == "show":
                    self.show()
                elif kind == "setup_done":
                    self.setup_btn.configure(state="normal", text="Set up LED driver")
                    if any(st[0] == "setup" for st in self.status.values()):
                        self._update_hint(f"Driver setup didn't finish - see {CONFIG_DIR / 'driver-setup.log'}")
                elif kind == "pick":
                    self.activate(msg[1], manual=True)
                elif kind == "toggle":
                    self.prof()["enabled"] = not self.prof()["enabled"]
                    self.refresh_header()
                    self.render_sticks()
                    self.refresh_sidebar()
                    self.push_all()
                    if self.tray:
                        self.tray.update_menu()
                elif kind == "auto":
                    self.activate(msg[1], reason=msg[2])
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def run(self):
        self.root.mainloop()


def headless_apply():
    """--apply: push the idle profile once without a window."""
    if not USB_OK:
        return 1
    cfg = load_config()
    prof = cfg["profiles"][cfg["idle"]]
    io = SolRIO()
    pending = ["left", "right"]
    for _ in range(15):
        for side in list(pending):
            try:
                io.write(side, profile_payload(prof, side))
                pending.remove(side)
            except Exception:
                pass
        if not pending:
            break
        time.sleep(2)
    io.close_all()
    return 0 if not pending else 1


if __name__ == "__main__":
    if "--install-driver" in sys.argv:
        sys.exit(run_driver_cli("install"))
    if "--uninstall-driver" in sys.argv:
        sys.exit(run_driver_cli("uninstall"))
    if "--apply" in sys.argv:
        sys.exit(headless_apply())
    sock = claim_single_instance()
    if sock is None:
        sys.exit(0)
    App(start_hidden="--tray" in sys.argv, instance_sock=sock).run()
