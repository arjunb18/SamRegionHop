#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Arjun Bhagavatula
"""SamRegionHop - change a Samsung phone's region (CSC) from macOS, Windows or Linux. No flashing.

Unofficial; not affiliated with Samsung.

Commands:
  info            Read model, build, active CSC and wipe behaviour over adb. Read-only.
  fota [CSC ...]  Ask Samsung's update server whether an OTA path exists from the
                  phone's current build for each CSC. Read-only.
  probe           Open the modem port and send only read-only AT queries, to prove
                  the connection works before changing anything. Needs Test Mode.
  targets         List the CSCs this phone can reliably switch to: in its own bundle
                  and confirmed by Samsung's server for this model. Read-only.
  change CSC      Switch the active CSC with Samsung's pre-configurator AT commands
                  (the mechanism SamFw uses). Needs Test Mode. May wipe the phone.
  report          Turn the latest log into a privacy-masked compatibility report
                  to attach to a GitHub issue.

Safety model:
  - Modem.send() refuses any AT command outside ALLOWED_AT, so no code path can
    reach download mode, call forwarding, SIM writes or anything else.
  - The target CSC must be proven to be in the installed firmware's CSC bundle,
    and every source of evidence that is available must agree.
  - The modem port must be proven to belong to the phone adb sees: model and
    firmware from the modem, plus its serial from USB or from the modem.
  - The pending CSC is read before and after the write. The phone is only
    rebooted automatically when the write was acknowledged and the read-back
    shows the target. Once the write has been sent, the tool never claims that
    nothing changed.
  - Nothing here flashes partitions or touches the bootloader; the only write is
    Samsung's own CSC-change command.

Requirements: adb on PATH, USB debugging on, pyserial (for probe/change).
"""

import argparse
import ast
import concurrent.futures
import datetime
import glob
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.9.0"
HERE = os.path.dirname(os.path.abspath(__file__))
FOTA_URL = "https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
SAMSUNG_VID = 0x04E8
LOG_DIR = os.path.join(os.path.expanduser("~"), ".samregionhop", "logs")  # per user, so any account can run the tool
MIN_BATTERY = 50
AT_GAP = 1.0  # seconds between AT commands; the modem drops commands sent faster
DRAIN = 0.3  # seconds of silence required before sending, so late replies can't be misread
DRAIN_MAX = 3.0  # give up if the port never goes quiet
TESTMODE_WAIT = 120
# Before the pre-read we can't know whether an earlier attempt left a change pending.
UNCHANGED = ("Nothing was changed by this run. If an earlier attempt may have left a change "
             "pending, back up before rebooting.")
TESTMODE_PKG = "hwmoduletest"  # com.sec.android.app.hwmoduletest, the *#0*# screen
ADB_SERIAL = None  # set by require_device(); every later adb call targets this phone
CHANGE_SENT = None  # (target, log) once AT+PRECONFG=2 has been sent; read by main()
CURRENT_LOG = None  # the active Log, so stop messages end up in it too
REPORT_DIR = os.path.join(os.path.expanduser("~"), ".samregionhop", "reports")


def load_region_names():
    """CSC code -> human label, from regions.json. Labels only; never used for decisions."""
    try:
        with open(os.path.join(HERE, "regions.json"), encoding="utf-8") as f:
            return json.load(f)["regions"]
    except (OSError, ValueError, KeyError):
        return {}


REGION_NAMES = load_region_names()

# Every AT command this tool can send.
ALLOWED_AT = (
    r"AT",
    r"ATI",
    r"AT\+DEVCONINFO",
    r"AT\+SWATD=[01]",
    r"AT\+ACTIVATE=0,0,0",
    r"AT\+PRECONFG=2,[A-Z0-9]{3}",
    r"AT\+PRECONFG=1,0",
)
FINAL_RE = re.compile(r"(?:^|[\r\n])(OK|ERROR|\+CME ERROR[^\r\n]*)\r?\n", re.I)

# Folders that hold one sub-folder per CSC in the firmware's multi-CSC bundle.
BUNDLE_DIRS = ("/optics/configs/carriers/single", "/optics/configs/carriers",
               "/prism/omc", "/system/omc")


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    if CURRENT_LOG:
        try:
            CURRENT_LOG(f"STOPPED: {msg}")
        except Exception:
            pass
    sys.exit(code)


def adb_argv(args):
    return ["adb", *(["-s", ADB_SERIAL] if ADB_SERIAL else []), *args]


def adb(*args, check=True, timeout=20):
    try:
        out = subprocess.run(adb_argv(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        die("adb not found on PATH")
    except subprocess.TimeoutExpired:
        die(f"adb {' '.join(args)} timed out. Is the phone unlocked and responding?")
    if check and out.returncode != 0:
        die(f"adb {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def adb_rc(*args, timeout=30):
    try:
        return subprocess.run(adb_argv(args), capture_output=True, text=True,
                              timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def require_device():
    """Pin all later adb calls to the single authorized device and return its serial."""
    global ADB_SERIAL
    ADB_SERIAL = None
    ready, not_ready = [], []
    for line in adb("devices").splitlines()[1:]:
        serial_id, _, state = line.strip().partition("\t")
        if not serial_id:
            continue
        if state == "device":
            ready.append(serial_id)
        else:
            not_ready.append(state)
    if not_ready:
        die(f"phone not ready ({', '.join(not_ready)}). Unlock it and accept the "
            f"'Allow USB debugging' prompt.")
    if not ready:
        die("no phone found. Plug in USB, enable USB debugging, accept the prompt on the phone.")
    if len(ready) > 1:
        die("more than one adb device connected. Unplug the others.")
    ADB_SERIAL = ready[0]
    return ready[0]


def props():
    out = adb("shell", "getprop")
    return dict(re.findall(r"^\[([^\]]+)\]: \[([^\]]*)\]$", out, re.M))


def first(p, *keys):
    for k in keys:
        if p.get(k):
            return p[k]
    return None


def read_bundle(active):
    """{'dir', 'codes'} for the installed multi-CSC bundle, or None if the phone won't say."""
    for d in BUNDLE_DIRS:
        out = adb("shell", f"ls {d} 2>/dev/null", check=False)
        codes = sorted({n.strip("/") for n in out.split() if re.fullmatch(r"[A-Z0-9]{3}/?", n)})
        # A real bundle lists many CSCs including the active one. Anything less
        # (e.g. a folder holding only the active CSC) proves nothing.
        if len(codes) >= 3 and active in codes:
            return {"dir": d, "codes": codes}
    return None


def device_info():
    p = props()
    info = {
        "serial": first(p, "ro.serialno", "ro.boot.serialno"),
        "manufacturer": p.get("ro.product.manufacturer", ""),
        "model": p.get("ro.product.model"),
        "build": first(p, "ro.build.PDA", "ro.build.display.id"),
        "csc_build": first(p, "ril.official_cscver", "ro.csc.version"),
        "modem": first(p, "gsm.version.baseband", "ril.sw_ver"),
        "active_csc": first(p, "ril.sales_code", "ro.csc.sales_code", "persist.omc.sales_code"),
        "csc_props": {k: p[k] for k in ("ril.sales_code", "ro.csc.sales_code",
                                        "persist.omc.sales_code", "ro.boot.sales_code") if p.get(k)},
        "android": p.get("ro.build.version.release"),
        "oneui": p.get("ro.build.version.oneui"),
        "patch": p.get("ro.build.version.security_patch"),
        "changetype": p.get("ro.omc.changetype", ""),
        "chip": first(p, "ro.soc.model", "ro.hardware.chipname", "ro.board.platform"),
    }
    info["bundle"] = read_bundle(info["active_csc"])
    return info


def require_samsung(info):
    if "samsung" not in info["manufacturer"].lower():
        die(f"not a Samsung phone (manufacturer '{info['manufacturer']}')")
    for k in ("serial", "model", "build", "csc_build", "active_csc"):
        if not info[k]:
            die(f"could not read the phone's {k.replace('_', ' ')}; refusing to continue")


def wipe_status(changetype):
    v = changetype.upper()
    if "DATA_RESET_ON,TRUE" in v:
        return "yes"
    if "DATA_RESET_ON,FALSE" in v:
        return "no"
    return "unknown"


def oneui_str(v):
    # ro.build.version.oneui is e.g. 70000 -> 7.0, 80500 -> 8.5
    if not v or not v.isdigit():
        return v
    n = int(v)
    return f"{n // 10000}.{(n // 100) % 100}"


def cmd_info(_args):
    require_device()
    i = device_info()
    rows = [
        ("Model", i["model"]),
        ("Serial", i["serial"]),
        ("Build (AP)", i["build"]),
        ("CSC package", i["csc_build"]),
        ("Modem (CP)", i["modem"]),
        ("Active CSC", i["active_csc"]),
        ("CSC properties", " ".join(f"{k}={v}" for k, v in i["csc_props"].items())),
        ("Chip", i["chip"] or "unknown"),
        ("Android / One UI", f"{i['android']} / {oneui_str(i['oneui'])}"),
        ("Security patch", i["patch"]),
        ("Wipe on CSC change", {"yes": "yes", "no": "no", "unknown": "unknown, assume yes"}
            [wipe_status(i["changetype"])] + (f" ({i['changetype']})" if i["changetype"] else "")),
        ("CSCs in bundle", " ".join(i["bundle"]["codes"]) if i["bundle"] else "not readable"),
    ]
    for k, v in rows:
        print(f"{k:<20} {v}")


# ---- Samsung update server -------------------------------------------------

def parse_version_xml(xml):
    """Return (latest, entries) where entries are 'AP/CSC/CP' strings, latest first."""
    latest = re.search(r"<latest[^>]*>([^<]+)</latest>", xml)
    if not latest:
        return None, []
    values = [v.strip() for v in re.findall(r"<value[^>]*>([^<]+)</value>", xml)]
    return latest.group(1).strip(), [latest.group(1).strip()] + values


def fota_entries(model, csc):
    """(latest, entries) for csc. (None, []) when Samsung publishes nothing for it."""
    req = urllib.request.Request(FOTA_URL.format(csc=csc, model=model),
                                 headers={"User-Agent": "Kies2.0_FUS"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return parse_version_xml(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):  # the server's answer for a CSC with no firmware
            return None, []
        raise


def csc_family(model, cscver):
    """'SM-S928B', 'S928BOXM4BYH2' -> 'OXM' (the multi-CSC bundle name)."""
    code = model.replace("SM-", "")
    if cscver and cscver.startswith(code) and len(cscver) >= len(code) + 3:
        return cscver[len(code):len(code) + 3]
    return None


BUILD_YEARS = "WXYZABCDEFGH"  # Samsung's year letters from 2023 on


def build_key(build):
    """Sortable (year, month, sequence) from a build like S928BXXS4BYH2, or None."""
    if not build or len(build) < 4 or build[-3] not in BUILD_YEARS:
        return None
    return (BUILD_YEARS.index(build[-3]), build[-2], build[-1])


def cmd_fota(args):
    if args.build:
        model, build, active = args.model, args.build.upper(), None
    else:
        require_device()
        i = device_info()
        model, build, active = i["model"], i["build"], i["active_csc"]
    given = [c.upper() for c in args.csc]
    if active:  # always include the region the phone is on
        cscs = list(dict.fromkeys([active, *given]))
    else:
        cscs = given or die("give a CSC to check")
    print(f"Phone: {model}, build {build}\n")
    for csc in cscs:
        try:
            latest, entries = fota_entries(model, csc)
        except Exception as e:
            print(f"{csc}: could not query update server ({e})")
            continue
        builds = {e.split("/")[0] for e in entries}
        if not latest:
            print(f"{csc}: no firmware published for {model}")
        elif build == latest.split("/")[0]:
            print(f"{csc}: already on latest ({latest.split('/')[0]})")
        elif build in builds:
            print(f"{csc}: OTA path exists, {build} -> {latest.split('/')[0]}")
        else:
            print(f"{csc}: NO OTA path. {build} was never released for {csc}, "
                  f"so the server has no update for it. Latest is {latest.split('/')[0]}; "
                  f"needs a full-firmware update (Smart Switch or flash).")


class Refused(Exception):
    """A target CSC failed a check; the message says why."""


_FETCH = object()  # "look it up on Samsung's server"


def check_target(info, target, notes, active_entries=_FETCH, target_entries=_FETCH):
    """Prove target is in the installed CSC bundle.

    Returns (evidence, target_entries, server_confirmed) or raises Refused. Warnings go
    to `notes`. active_entries/target_entries can be passed in when already fetched.
    """
    model, active = info["model"], info["active_csc"]
    installed = csc_family(model, info["csc_build"])
    if not installed:
        raise Refused(f"couldn't tell which CSC bundle {info['csc_build']} is; refusing to guess")
    evidence = []

    bundle = info["bundle"]
    if bundle:
        if target not in bundle["codes"]:
            raise Refused(f"{target} is not in this phone's CSC bundle ({' '.join(bundle['codes'])}). "
                          f"Switching to it would need a full firmware flash.")
        if not adb("shell", f"ls {bundle['dir']}/{target} 2>/dev/null", check=False).split():
            raise Refused(f"{target}'s folder in the phone's CSC bundle is empty; refusing to switch to it")
        evidence.append(f"{target} has a populated folder in the phone's installed CSC bundle")

    entries = None
    try:
        if active_entries is _FETCH:
            _, active_entries = fota_entries(model, active)
        if target_entries is _FETCH:
            _, target_entries = fota_entries(model, target)
        entries = target_entries
    except Exception as e:
        notes.append(f"could not reach Samsung's update server ({e})")
        entries = None
    if entries is not None:
        if not active_entries:
            # Silence even about the CSC the phone runs means the server is refusing
            # us, so its silence about the target proves nothing either way.
            notes.append(f"Samsung's server returned nothing for {active} either; ignoring it")
            entries = None
        elif not entries:
            raise Refused(f"Samsung publishes no {target} firmware for {model}, so {target} is not a "
                          f"region this phone's firmware serves.")
        else:
            families = {csc_family(model, e.split("/")[1]) for e in entries if e.count("/") == 2}
            if families != {installed}:
                raise Refused(f"{target} firmware for {model} uses the "
                              f"{'/'.join(sorted(f or '?' for f in families))} bundle, but the phone "
                              f"has {installed}. Switching would need a full firmware flash.")
            evidence.append(f"all {len(entries)} {target} firmwares Samsung published for "
                            f"{model} use the {installed} bundle, same as installed")
            keys = sorted(k for k in (build_key(e.split("/")[0]) for e in entries) if k)
            mine = build_key(info["build"])
            if mine and keys and keys[0] < mine < keys[-1]:
                evidence.append(f"{target} shipped {installed} builds both before and after "
                                f"your build {info['build']}")

    if not evidence:
        raise Refused(f"could not confirm {target} is in this phone's CSC bundle; refusing to guess.")
    return evidence, entries or [], bool(entries)


def validate_target(info, target):
    """check_target for the change command: returns (evidence, target_entries) or exits."""
    notes = []
    try:
        evidence, entries, _ = check_target(info, target, notes)
    except Refused as e:
        for n in notes:
            print(f"warning: {n}")
        die(str(e))
    for n in notes:
        print(f"warning: {n}")
    return evidence, entries


def region_label(code):
    return REGION_NAMES.get(code, "unknown region")


def reliable_targets(info):
    """([(code, evidence)], [(code, reason)]) for every other CSC in the phone's bundle.

    Reliable means: listed in the phone's bundle with a populated folder AND confirmed by
    Samsung's server as using the phone's package. Anything less is not offered.
    """
    model, active = info["model"], info["active_csc"]
    if not info["bundle"]:
        return [], [("*", "the phone doesn't list its regions, so none can be confirmed")]
    codes = [c for c in info["bundle"]["codes"] if c != active]
    try:
        _, active_entries = fota_entries(model, active)
    except Exception as e:
        return [], [("*", f"couldn't reach Samsung's update server ({e})")]
    if not active_entries:
        return [], [("*", f"Samsung's server returned nothing for {active}, so nothing can be confirmed")]

    def fetch(code):
        try:
            return code, fota_entries(model, code)[1], None
        except Exception as e:
            return code, None, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fetched = {c: (entries, err) for c, entries, err in pool.map(fetch, codes)}

    ok, rejected = [], []
    for code in codes:
        entries, err = fetched[code]
        if err is not None:
            rejected.append((code, f"server lookup failed ({err})"))
            continue
        try:
            evidence, _, confirmed = check_target(info, code, [], active_entries, entries)
        except Refused as e:
            rejected.append((code, str(e)))
            continue
        if confirmed:
            ok.append((code, evidence))
        else:
            rejected.append((code, "Samsung's server didn't confirm it"))
    return ok, rejected


def cmd_targets(args):
    require_device()
    info = device_info()
    require_samsung(info)
    active = info["active_csc"]
    print(f"Checking which regions {info['model']} (on {active} — {region_label(active)}) can "
          f"reliably switch to...\n")
    ok, rejected = reliable_targets(info)
    for code, _ in sorted(ok, key=lambda x: region_label(x[0])):
        print(f"  {code}  {region_label(code)}")
    if not ok:
        print("  (none)")
    if rejected:
        print(f"\nNot offered: {len(rejected)}" + ("" if args.verbose else " (use -v to see why)"))
        if args.verbose or any(c == "*" for c, _ in rejected):
            for code, reason in rejected:
                print(f"  {code}: {reason}")
    print(f"\nReliable targets: {' '.join(sorted(c for c, _ in ok)) or 'none'}")


# ---- phone state checks ----------------------------------------------------

def battery_level():
    out = adb("shell", "dumpsys battery", check=False)
    if "UPDATES STOPPED" in out.upper():  # values faked with `dumpsys battery set`
        return None
    m = re.search(r"^\s*level:\s*(\d+)", out, re.M)
    return int(m.group(1)) if m else None


def account_types():
    """Signed-in account types, or None if the phone wouldn't say."""
    out = adb("shell", "dumpsys account", check=False)
    if not re.search(r"^\s*Accounts:\s*\d+", out, re.M):
        return None
    return set(re.findall(r"Account \{name=[^,]*, type=([^}\s]+)\}", out))


def esim_present():
    """True/False, or None if the phone wouldn't say."""
    out = adb("shell", "dumpsys isub", check=False)
    if not re.search(r"isEmbedded", out, re.I):
        return None
    return bool(re.search(r"isEmbedded[=:]\s*(true|1)\b", out, re.I))


def focused_window():
    out = adb("shell", "dumpsys window | grep mCurrentFocus", check=False)
    m = re.search(r"mCurrentFocus=Window\{[^}]*\s(\S+)\}", out)
    return m.group(1) if m else ""


def wait_for_testmode(skip):
    if skip:
        return
    print("On the phone: turn on Developer options > Stay awake (a locked screen makes the "
          "modem refuse commands)\nand, if you see it there, '3GPP AT commands'. Then open the "
          "Phone app, dial *#0*# and leave the Test Mode screen open.")
    deadline = time.time() + TESTMODE_WAIT
    while time.time() < deadline:
        if TESTMODE_PKG in focused_window().lower():
            print("Test Mode is open.")
            return
        time.sleep(2)
    die(f"Test Mode (*#0*#) was not detected on screen (focused: '{focused_window()}'). "
        f"Nothing was changed.")


def port_busy(port):
    """True if another process has the port (or its /dev/tty. twin) open."""
    if sys.platform == "win32":
        return False  # Windows never shares a COM port; a busy one fails to open instead
    for path in sorted({port, port.replace("/dev/cu.", "/dev/tty."),
                        port.replace("/dev/tty.", "/dev/cu.")}):
        try:
            out = subprocess.run(["lsof", "-t", path], capture_output=True, text=True,
                                 timeout=10).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue  # can't tell; the exclusive open still guards
        if any(p.isdigit() and int(p) != os.getpid() for p in out.split()):
            return True
    return False


# ---- modem -----------------------------------------------------------------

def mask(text):
    """Hide the IMEI, phone number (PN) and unique number (UN) before text is logged."""
    text = re.sub(r"(IMEI\d?\()(\d+)(\))", lambda m: m.group(1) + "*" * len(m.group(2)) + m.group(3), text)
    return re.sub(r"\b(PN|UN)\(([^)]*)\)", lambda m: f"{m.group(1)}({'*' * len(m.group(2))})", text)


class Log:
    def __init__(self, name):
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(LOG_DIR, f"{stamp}-{name}.log")
        self.f = open(self.path, "a", encoding="utf-8")
        global CURRENT_LOG
        CURRENT_LOG = self

    def __call__(self, text):
        self.f.write(f"[{datetime.datetime.now():%H:%M:%S}] {mask(text)}\n")
        self.f.flush()


class ModemError(OSError):
    pass


class Modem:
    def __init__(self, port, log):
        import serial
        self.port, self.log = port, log
        self.ser = serial.Serial(port, 115200, timeout=0.5, write_timeout=5, exclusive=True)
        self.late = ""  # what arrived between the previous reply and the last send

    def _drain(self):
        """Read and return anything still arriving from an earlier command.

        Reads rather than reset_input_buffer(), so late replies are seen, not lost.
        """
        junk, start = b"", time.time()
        quiet_until = start + DRAIN
        while True:
            if self.ser.in_waiting:
                junk += self.ser.read(self.ser.in_waiting)
                quiet_until = time.time() + DRAIN
            elif time.time() >= quiet_until:
                break
            else:
                time.sleep(0.05)
            if time.time() - start > DRAIN_MAX:
                raise ModemError(f"modem port never went quiet (last data {junk[-60:]!r})")
        if junk:
            self.log(f"late data before next command: {junk!r}")
        return junk.decode(errors="replace")

    def send(self, command, timeout=10.0, until=None):
        if not any(re.fullmatch(p, command) for p in ALLOWED_AT):
            raise ValueError(f"refusing to send AT command outside the allowlist: {command!r}")
        self.late = self._drain()
        # write() is bounded by write_timeout. No flush(): tcdrain has no timeout
        # and can block forever if the port stalls.
        self.ser.write((command + "\r\n").encode())
        buf, deadline = "", time.time() + timeout
        while time.time() < deadline:
            buf += self.ser.read(self.ser.in_waiting or 1).decode(errors="replace")
            if FINAL_RE.search(buf):
                break
            if until and re.search(until, buf):
                buf += self._drain()  # anything trailing (e.g. an ERROR) belongs to this reply
                break
        self.log(f"> {command}\n{buf.strip()}")
        time.sleep(AT_GAP)
        return buf

    def close(self):
        self.ser.close()


def result_code(resp):
    m = FINAL_RE.search(resp)
    return m.group(1).upper() if m else None


def oneline(resp):
    return mask(" | ".join(l.strip() for l in resp.splitlines() if l.strip())) or "(no reply)"


def is_modem_port_path(device, plat=None):
    """True for the kind of port path a USB modem gets on this OS."""
    plat = plat or sys.platform
    if plat == "darwin":
        return device.startswith("/dev/cu.")
    if plat.startswith("linux"):
        return device.startswith("/dev/ttyACM")
    if plat == "win32":
        return re.fullmatch(r"COM\d+", device) is not None
    return False


PORT_HINT = {"win32": " On Windows this needs Samsung's USB driver for mobile phones installed.",
             "linux": " On Linux, ModemManager can grab the port (stop it), and your user needs access "
                      "to /dev/ttyACM* (the dialout group)."}


def find_modem_ports(serial_id):
    """[(port, usb_serial_matches)] for Samsung serial ports that may be this phone."""
    from serial.tools import list_ports
    samsung = [p for p in list_ports.comports()
               if p.vid == SAMSUNG_VID and is_modem_port_path(p.device)]
    mine = [(p.device, True) for p in samsung if p.serial_number == serial_id]
    if mine:
        return sorted(mine)
    if samsung and not any(p.serial_number for p in samsung):
        # macOS didn't report USB serials; check_identity() must then prove it via the modem.
        return sorted((p.device, False) for p in samsung)
    return []


def open_modem(serial_id, explicit_port, log):
    """Return (modem, usb_serial_matches) for a port that answers AT."""
    try:
        import serial  # noqa: F401
    except ImportError:
        die("pyserial missing. Run it with the launcher: ./csc (csc.bat on Windows)")
    ports = [(explicit_port, False)] if explicit_port else []
    for _ in range(20):
        ports = ports or find_modem_ports(serial_id)
        if ports:
            break
        time.sleep(1)
    if not ports:
        hint = next((v for k, v in PORT_HINT.items() if sys.platform.startswith(k)), "")
        die("the phone's modem port did not appear. Check Test Mode is open and the cable "
            f"carries data.{hint} Nothing was changed.")
    problems = []
    for port, usb_match in ports:
        if port_busy(port):
            die(f"{port} is open in another app (Smart Switch? a terminal?). Close it first. "
                f"Nothing was changed.")
        try:
            modem = Modem(port, log)
        except Exception as e:
            log(f"open {port} failed: {e}")
            problems.append(f"{port}: {e}")
            continue
        answered = False
        # A freshly exposed modem can swallow the first AT while it starts up (it
        # announces "+USB READY"), so try the harmless AT a few times.
        for _ in range(3):
            try:
                if result_code(modem.send("AT", timeout=4)) == "OK":
                    answered = True
                    break
            except Exception as e:
                log(f"probing {port} failed: {e!r}")
                problems.append(f"{port}: {e}")
                break
        if answered:
            print(f"Modem port: {port}")
            return modem, usb_match
        modem.close()
    die(f"no port answered AT ({'; '.join(problems) or ', '.join(p for p, _ in ports)}). "
        f"Nothing was changed.")


def check_identity(modem, info, usb_match):
    """Prove the modem we're talking to is the phone adb sees, or exit."""
    resp = modem.send("AT+DEVCONINFO")
    fields = dict(re.findall(r"([A-Z]+)\(([^)]*)\)", resp))
    if fields.get("MN"):
        if fields["MN"] != info["model"]:
            die(f"modem reports model {fields['MN']}, adb reports {info['model']}. "
                f"Nothing was changed.")
        if fields.get("SN") and fields["SN"] != info["serial"]:
            die(f"modem reports serial {fields['SN']}, adb reports {info['serial']}. "
                f"Nothing was changed.")
        if fields.get("VER") and info["build"] not in fields["VER"]:
            die(f"modem reports firmware {fields['VER']}, adb reports {info['build']}. "
                f"Nothing was changed.")
    else:
        resp = modem.send("ATI")
        if info["model"] not in resp:
            die(f"modem did not identify as {info['model']} ({oneline(resp)}). "
                f"Nothing was changed.")
    if not usb_match and fields.get("SN") != info["serial"]:
        die(f"couldn't prove this modem port belongs to phone {info['serial']} (macOS gave "
            f"no USB serial and the modem gave none). Nothing was changed.")
    return fields


def cmd_probe(args):
    serial_id = require_device()
    info = device_info()
    require_samsung(info)
    wait_for_testmode(args.no_focus_check)
    log = Log("probe")
    modem, usb_match = open_modem(serial_id, args.port, log)
    try:
        fields = check_identity(modem, info, usb_match)
    finally:
        modem.close()
    proof = [f"model {info['model']}"]
    if fields.get("VER"):
        proof.append(f"firmware {info['build']}")
    if usb_match:
        proof.append("USB serial")
    if fields.get("SN") == info["serial"]:
        proof.append("modem serial")
    print(f"Modem matches the adb phone {info['serial']} by: {', '.join(proof)}.")
    print(f"Probe OK. Only read-only commands were sent. Log: {log.path}")


# ---- change ----------------------------------------------------------------

def reply_lines(resp, cmd):
    """Meaningful lines of a reply: no blanks, no echo of cmd, and no '#OK#' marker.

    '#OK#' is how this phone closes the payload of some replies (seen in
    AT+DEVCONINFO). It is the only extra line tolerated; anything else stays.
    """
    return [l.strip() for l in resp.splitlines()
            if l.strip() and l.strip() != cmd and l.strip() != "#OK#"]


# Some phones answer AT+SWATD with no final OK, only a routing notice, e.g. on
# SM-S928B: "[ATD]: CHANGE TO DDEXE" for SWATD=0 and "[ATD]: CHANGE TO ATD" for 1.
SWATD_TARGET = {"0": "DDEXE", "1": "ATD"}
SWATD_DONE = r"\[ATD\]:\s*CHANGE TO (DDEXE|ATD)\b"


def swatd_ok(resp, mode):
    """True if AT+SWATD=<mode> was confirmed, by a final OK or the matching routing notice."""
    if re.search(r"ERROR|\bNG\b|FAIL", resp, re.I):
        return False
    seen = re.findall(r"\[ATD\]:\s*CHANGE TO (\w+)", resp)
    if any(t != SWATD_TARGET[mode] for t in seen):
        return False  # a notice naming the wrong route outweighs any OK
    return result_code(resp) == "OK" or bool(seen)


def read_pending(modem):
    """The CSC the pre-configurator holds, from AT+PRECONFG=1,0, or None.

    The reply must be exactly +PRECONFG:1,<csc> then OK (after the echo); any
    other line in it, or any other format, gives None.
    """
    cmd = "AT+PRECONFG=1,0"
    resp = modem.send(cmd)
    print(f"> {cmd}\n  {oneline(resp)}")
    lines = reply_lines(resp, cmd)
    if len(lines) != 2 or lines[1].upper() != "OK":
        return None, resp
    m = re.fullmatch(r"\+?PRECONFG:\s*1,\s*([A-Z0-9]{3})", lines[0], re.I)
    return (m.group(1).upper() if m else None), resp


def any_pending(resp):
    """Every CSC mentioned in +PRECONFG:1,XXX lines of a reply, however messy."""
    return set(c.upper() for c in re.findall(r"PRECONFG:\s*1,\s*([A-Z0-9]{3})\b", resp, re.I))


def preconfg_ok(resp, cmd):
    """True only for a clean acknowledgement of cmd (AT+PRECONFG=2,<csc>).

    After dropping the echo, the reply must be a final OK, optionally preceded by
    +PRECONFG:2,OK lines, and nothing else.
    """
    lines = reply_lines(resp, cmd)
    if not lines or lines[-1].upper() != "OK":
        return False
    return all(re.fullmatch(r"\+?PRECONFG:\s*2,\s*OK", l, re.I) for l in lines[:-1])


def state_unknown(target, log, reason):
    log(f"RESULT: state unknown ({reason})")
    print(f"\nSTATE UNKNOWN: the change command reached the phone but the result couldn't be "
          f"confirmed ({reason}).\nAssume the next reboot switches the phone to {target} and "
          f"WIPES it. Finish your backup before rebooting.\nAfter it restarts, run "
          f"step 1 (Read phone) or `./csc info` to see which CSC it's on. Log: {log.path}", flush=True)
    sys.exit(2)


def cmd_change(args):
    global CHANGE_SENT
    target = args.csc.upper()
    if not re.fullmatch(r"[A-Z0-9]{3}", target):
        die("CSC must be a 3-character code, e.g. INS")

    serial_id = require_device()
    info = device_info()
    require_samsung(info)
    if info["serial"] != serial_id:
        die(f"adb serial {serial_id} doesn't match ro.serialno {info['serial']}")
    active = info["active_csc"]
    if active == target:
        die(f"phone is already on {target}")

    evidence, target_entries = validate_target(info, target)
    battery = battery_level()
    if battery is None or battery < MIN_BATTERY:
        die(f"battery is {battery if battery is not None else 'unknown'}%; charge to "
            f"{MIN_BATTERY}%+ first so the reset can't die halfway")
    accounts = account_types()
    esim = esim_present()
    wipe = wipe_status(info["changetype"])

    print(f"\nChange CSC {active} -> {target} on {info['model']} ({info['serial']})")
    print(f"Firmware {info['build']} / {info['csc_build']}, battery {battery}%\n")
    print(f"Why {target} is a valid choice:")
    for e in evidence:
        print(f"  - {e}")
    print("\nWhat will happen:")
    print("  - " + {"yes": "The phone WILL factory reset after the reboot (ro.omc.changetype). "
                           "Back up first.",
                    "no": "The firmware reports no data reset, but back up first anyway.",
                    "unknown": "The phone will most likely factory reset after the reboot. "
                               "Back up first."}[wipe])
    if accounts is None or "com.google" in accounts:
        print("  - " + ("A Google account is signed in" if accounts else
                        "If a Google account is signed in (couldn't check)") +
              ": after the reset you must sign in with that same account (Factory Reset "
              "Protection). Know the password.")
    if accounts is None or "com.osp.app.signin" in accounts:
        print("  - " + ("A Samsung account is signed in" if accounts else
                        "If a Samsung account is signed in (couldn't check)") +
              ": know its password too (Reactivation Lock).")
    if esim:
        print("  - An eSIM is installed and may be erased by the reset. Check how to "
              "re-download it from your carrier first.")
    elif esim is None:
        print("  - Couldn't tell if an eSIM is installed. If you use one, it may be erased.")
    target_builds = {e.split("/")[0] for e in target_entries}
    if target_entries and info["build"] not in target_builds:
        print(f"  - {target} has no OTA update path from your build {info['build']} either. "
              f"Updates still need one full-firmware update (e.g. Smart Switch) afterwards.")
    print("  - Firmware, bootloader and Knox are not touched.")
    print("  - Once you confirm, treat the phone as if it WILL be wiped at its next reboot, "
          "even if something goes wrong midway.")

    wait_for_testmode(args.no_focus_check)
    log = Log(f"change-{active}-{target}")
    log(f"device {info}")
    modem, usb_match = open_modem(serial_id, args.port, log)
    swatd_open = written = False
    accepted, after, resp, late = False, None, "", ""
    try:
        check_identity(modem, info, usb_match)
        if args.dry_run:
            print(f"\nDry run: every check passed and the modem is the right phone. "
                  f"Nothing was changed. Log: {log.path}")
            return
        try:
            typed = input(f"\nType {target} to change the CSC (anything else aborts): ")
        except EOFError:
            typed = ""
        if typed.strip().upper() != target:
            die("aborted. Nothing was changed.")
        # The prompt can sit for a while; make sure the phone is still in Test Mode.
        if not args.no_focus_check and TESTMODE_PKG not in focused_window().lower():
            die("Test Mode is no longer on screen (did the phone lock?). Nothing was changed.")

        swatd_open = True
        for cmd in ("AT+SWATD=0", "AT+ACTIVATE=0,0,0", "AT+SWATD=1"):
            resp = modem.send(cmd, until=SWATD_DONE if "SWATD" in cmd else None)
            print(f"> {cmd}\n  {oneline(resp)}")
            if cmd == "AT+SWATD=0" and not swatd_ok(resp, "0"):
                die(f"modem refused SWATD. {UNCHANGED}")
        if not swatd_ok(resp, "1"):
            die(f"modem didn't confirm SWATD=1. {UNCHANGED}")
        swatd_open = False

        # Read before writing: the reply format must be understood, and a change
        # left pending by an earlier run must be caught before anyone reboots.
        before, raw = read_pending(modem)
        if before is None:
            # Unparseable, but a pending change mentioned anywhere must still stop us.
            others = sorted(any_pending(raw) - {active})
            if not others:
                die(f"couldn't read the CSC from the modem cleanly. {UNCHANGED}")
            before = others[0]
        if before != active:
            print(f"\nSTOPPED: the modem already holds {before}"
                  f"{' (the CSC you asked for)' if before == target else ''} while the phone "
                  f"runs {active}, probably from an earlier attempt.\nThe next reboot will "
                  f"likely switch the phone to {before} and WIPE it. Nothing new was written.\n"
                  f"Finish your backup before rebooting, then run step 1 (Read phone) or `./csc info`. "
                  f"Log: {log.path}")
            sys.exit(2)

        written, CHANGE_SENT = True, (target, log)
        cmd = f"AT+PRECONFG=2,{target}"
        resp = modem.send(cmd, timeout=20)
        print(f"> {cmd}\n  {oneline(resp)}")
        accepted = preconfg_ok(resp, cmd)
        after, _ = read_pending(modem)
        late = modem.late.strip()  # anything that arrived after the change command's reply
    except BaseException as e:
        if isinstance(e, SystemExit):
            raise
        reason = "interrupted" if isinstance(e, KeyboardInterrupt) else f"error: {e!r}"
        if written:
            state_unknown(target, log, reason)
        die(f"{reason}. {UNCHANGED}")
    finally:
        if swatd_open:
            try:
                restored = swatd_ok(modem.send("AT+SWATD=1", until=SWATD_DONE), "1")
            except Exception as e:
                log(f"cleanup SWATD=1 failed: {e}")
                restored = False
            if not restored:
                print("note: couldn't confirm the modem switched back to normal. Restart the "
                      "phone to restore it (back up first if an earlier attempt may have left "
                      "a change pending).", file=sys.stderr)
        try:
            modem.close()
        except Exception:
            pass

    if accepted and not late and after == target:
        log(f"RESULT: change to {target} confirmed; rebooting")
        print(f"\nThe phone acknowledged the change and reports the pending CSC as {target}. "
              f"Rebooting to apply it...")
        if adb_rc("reboot") != 0:
            print("adb reboot failed. Reboot by hand: hold Side + Volume Down.")
        print(f"After it restarts, run step 1 (Read phone) or `./csc info` to confirm. Log: {log.path}")
        return
    if late:
        print(f"\nThe phone sent more after its reply to the change command: {oneline(late)}")
    if after == target:
        log(f"RESULT: pending {target} but reply not clean; not rebooted")
        print(f"\nThe phone reports the pending CSC as {target}, but "
              f"{'it sent extra data afterwards' if accepted else f'its reply was not clean ({oneline(resp)})'}"
              f". Not rebooting automatically.\nReboot by hand to apply it "
              f"(this WIPES the phone), then run step 1 (Read phone) or `./csc info`. Log: {log.path}")
        sys.exit(2)
    if after == active and not accepted:
        log(f"RESULT: change not accepted; still {active}")
        print(f"\nSTATE UNCERTAIN: the phone didn't accept the change ({oneline(resp)}) and "
              f"still reports {active}, so it most likely did not take. It did receive the "
              f"command, though, so a reboot might still switch it to {target} and wipe it.\n"
              f"Finish your backup before rebooting, then run step 1 (Read phone) or `./csc info`. Log: {log.path}")
        sys.exit(2)
    if after == active:
        state_unknown(target, log, f"the phone acknowledged the change but the read-back still "
                                   f"shows {active}, which may only be the active CSC")
    state_unknown(target, log, f"read-back gave {after or 'nothing usable'}")


def mask_report(text, serials):
    """Hide everything identifying before a log is shared publicly."""
    text = mask(text)  # IMEI, phone number, unique number
    for sn in sorted({s for s in serials if s and len(s) >= 6}, key=len, reverse=True):
        text = text.replace(sn, sn[:3] + "*" * (len(sn) - 3))
    text = re.sub(r"\bSN\(([^)]{3})([^)]*)\)", lambda m: f"SN({m.group(1)}{'*' * len(m.group(2))})", text)
    return text


def os_label():
    """e.g. 'macOS 26.2', 'Windows 11', 'Linux 6.8.0' (without spawning any command)."""
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if sys.platform == "win32":
        return f"Windows {platform.release()}"
    return f"{platform.system()} {platform.release()}"


def cmd_report(args):
    logs = sorted(glob.glob(os.path.join(LOG_DIR, "*.log")), key=os.path.getmtime)
    if args.log:
        logs = [args.log]
    if not logs:
        die(f"no logs found in {LOG_DIR}. Run a step (probe, dry run or change) first.")
    path = logs[-1]
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    device = {}
    m = re.search(r"\] device (\{.*\})\n", raw)
    if m:
        try:
            device = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            device = {}
    serials = [device.get("serial")] + re.findall(r"SN\(([^)]+)\)", raw) + re.findall(r"MDM([A-Z0-9]{8,})", raw)
    body = re.sub(r"^\[[^\]]*\] device \{.*\}\n", "", raw, flags=re.M)
    oneui = oneui_str(device.get("oneui")) if device.get("oneui") else "?"
    rows = [("Tool version", __version__), ("Computer OS", os_label()),
            ("Model", device.get("model", "?")), ("Chip", device.get("chip") or "?"),
            ("Build", device.get("build", "?")), ("CSC package", device.get("csc_build", "?")),
            ("Region", device.get("active_csc", "?")),
            ("Android / One UI", f"{device.get('android', '?')} / {oneui}"),
            ("Wipe flag", device.get("changetype") or "?"), ("Log", os.path.basename(path))]
    result = re.findall(r"(?:RESULT|STOPPED): .*", raw)
    report = ["# SamRegionHop compatibility report", "",
              "| Field | Value |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows], "",
              f"**Outcome:** {result[-1] if result else 'not recorded (run ended without a result line)'}",
              "", "## Modem exchange (identifiers masked)", "", "```", body.strip(), "```", ""]
    text = mask_report("\n".join(report), serials)
    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR, os.path.basename(path).replace(".log", "-report.md"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Report saved: {out}\n\nSerial, IMEI and phone number are masked. Read it before sharing, "
          f"then attach it to a new issue on the project's GitHub page.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show build and CSC details (read-only)")
    t = sub.add_parser("targets", help="list regions this phone can reliably switch to (read-only)")
    t.add_argument("-v", "--verbose", action="store_true", help="also say why each other region isn't offered")
    r = sub.add_parser("report", help="make a privacy-masked compatibility report from the latest log")
    r.add_argument("--log", help="use this log file instead of the latest")
    f = sub.add_parser("fota", help="check whether OTA updates can reach this build (read-only)")
    f.add_argument("csc", nargs="*", help="CSCs to check (the phone's active CSC is always included)")
    f.add_argument("--build", help="check this build instead of reading the phone, e.g. S928BXXS4BYH2")
    f.add_argument("--model", default="SM-S928B", help="model for --build (default SM-S928B)")
    for name, helptext in (("probe", "test the modem connection with read-only commands"),
                           ("change", "switch the active CSC")):
        c = sub.add_parser(name, help=helptext)
        if name == "change":
            c.add_argument("csc", help="target CSC, e.g. INS")
            c.add_argument("--dry-run", action="store_true",
                           help="run every check, then stop before changing anything")
        c.add_argument("--port", help="modem port override, e.g. /dev/cu.usbmodem1234, /dev/ttyACM0 or COM5")
        c.add_argument("--no-focus-check", action="store_true",
                       help="don't require the Test Mode screen to be detected")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows pipes default to a legacy codepage that can't print "—" or "Türkiye"
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if hasattr(signal, "SIGBREAK"):  # Windows: the UI's Stop button sends Ctrl-Break
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        # A background launcher can leave SIGINT ignored, which would silently disable
        # Ctrl-C and the UI's Stop button. Make sure it raises KeyboardInterrupt.
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except ValueError:
        pass  # not the main thread (only happens under test)
    try:
        {"info": cmd_info, "fota": cmd_fota, "probe": cmd_probe, "change": cmd_change,
         "targets": cmd_targets, "report": cmd_report}[args.cmd](args)
    except (KeyboardInterrupt, Exception) as e:
        reason = "interrupted" if isinstance(e, KeyboardInterrupt) else f"unexpected error: {e!r}"
        if CHANGE_SENT:
            state_unknown(CHANGE_SENT[0], CHANGE_SENT[1], reason)
        die(f"{reason}. No CSC change was attempted.")


if __name__ == "__main__":
    main()
