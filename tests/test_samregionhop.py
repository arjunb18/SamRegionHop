# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Arjun Bhagavatula
"""End-to-end tests for samregionhop against a simulated phone.

The phone is simulated at the boundaries the tool talks to:
  - adb (and lsof): subprocess.run is replaced by FakePhone.
  - modem: FakeModem serves AT commands on a real pseudo-terminal, so the real
    pyserial I/O and response parsing are exercised.
Samsung's update server is replaced by fixtures captured from the real server.
"""

import contextlib
import io
import os
import pty
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import samregionhop as g  # noqa: E402

if sys.platform == "win32":  # the simulated modem needs POSIX pseudo-terminals
    raise unittest.SkipTest("run these tests on macOS or Linux")

FIXTURES = os.path.join(HERE, "fixtures")
REAL_SLEEP = time.sleep  # the harness stubs time.sleep; helper threads need the real one
SERIAL = "R5CX10TEST"
MODEL = "SM-S928B"
BUILD = "S928BXXS4BYH2"
FOCUS_TESTMODE = ("mCurrentFocus=Window{1a2b u0 com.sec.android.app.hwmoduletest/"
                  "com.sec.android.app.hwmoduletest.HwModuleTest}")
FOCUS_HOME = "mCurrentFocus=Window{1a2b u0 com.sec.android.app.launcher/.LauncherActivity}"
IDENTITY = ["AT", "AT+DEVCONINFO"]
UNLOCK = ["AT+SWATD=0", "AT+ACTIVATE=0,0,0", "AT+SWATD=1"]
CHANGE_CMDS = [*UNLOCK, "AT+PRECONFG=1,0", "AT+PRECONFG=2,INS", "AT+PRECONFG=1,0"]


class FakeModem:
    """Samsung modem AT port on a pseudo-terminal. Replies mimic real captures.

    AT+PRECONFG=1,0 reports the pending CSC: the active one until a write lands.
    """

    def __init__(self, overrides=None, serial=SERIAL, model=MODEL, build=BUILD, csc="THL"):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.overrides = overrides or {}
        self.serial, self.model, self.build, self.pending = serial, model, build, csc
        self.received = []
        self.alive = True
        self.helpers = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def reply(self, cmd):
        if cmd in self.overrides:
            r = self.overrides[cmd]
            return r(self) if callable(r) else r
        if cmd == "AT":
            return "OK"
        if cmd == "ATI":
            return f"Manufacturer: SAMSUNG\r\nModel: {self.model}\r\n\r\nOK"
        if cmd == "AT+DEVCONINFO":
            b = self.build
            return (f"+DEVCONINFO: MN({self.model});BASE({self.model});"
                    f"VER({b}/S928BOXM4BYH2/{b}/{b});HIDVER({b}/S928BOXM4BYH2/{b}/{b});"
                    f"MNC();MCC();PRD(THL);SN({self.serial});"
                    f"IMEI(350000000000001);UN(ABCDEF);CON(AT,MTP);LOCK(NONE)#\r\n\r\nOK")
        if cmd in ("AT+SWATD=0", "AT+SWATD=1"):
            return "OK"
        if cmd == "AT+ACTIVATE=0,0,0":
            return "+ACTIVATE:0,UNREGISTED\r\n\r\nOK"  # real phones answer this
        if cmd.startswith("AT+PRECONFG=2,"):
            self.pending = cmd[-3:]
            return "+PRECONFG:2,OK\r\n\r\nOK"
        if cmd == "AT+PRECONFG=1,0":
            return f"+PRECONFG:1,{self.pending}\r\n\r\nOK"
        return "ERROR"

    def _loop(self):
        buf = b""
        while self.alive:
            # Poll so close() never has to interrupt a blocked read (hangs on macOS).
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                line, _, buf = buf.partition(b"\r")
                buf = buf.lstrip(b"\n")
                cmd = line.decode().strip()
                if not cmd:
                    continue
                self.received.append(cmd)
                r = self.reply(cmd)
                if r is None:
                    continue  # silent modem
                # Echo, then the reply framed the way the real modem frames it.
                os.write(self.master, (cmd + "\r\r\n" + r + "\r\n").encode())

    def start_noise(self, line=b"+NOISE\r\n", every=0.02):
        """Emit unsolicited lines forever, like a port that never goes quiet."""
        def emit():
            while self.alive:
                # Only write when there's room, so a full buffer can never block us.
                if select.select([], [self.master], [], 0.05)[1]:
                    with contextlib.suppress(OSError):
                        os.write(self.master, line)
                REAL_SLEEP(every)
        t = threading.Thread(target=emit, daemon=True)
        self.helpers.append(t)
        t.start()

    def say_later(self, text, delay=0.05):
        """Send text after a delay, like a late second reply."""
        t = threading.Timer(delay, lambda: os.write(self.master, text.encode()))
        self.helpers.append(t)
        t.start()

    def close(self):
        self.alive = False
        self.thread.join()
        for t in self.helpers:
            t.join(timeout=2)
        for fd in (self.master, self.slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def readback_sequence(*values):
    """Override for AT+PRECONFG=1,0 that answers each call with the next value."""
    it = iter(values)
    return lambda m: next(it)


class FakePhone:
    """Answers adb commands the way a Galaxy S24 Ultra on THL does."""

    def __init__(self, **kw):
        self.devices = kw.get("devices", f"List of devices attached\n{SERIAL}\tdevice\n")
        self.props = {
            "ro.serialno": SERIAL,
            "ro.product.manufacturer": "samsung",
            "ro.product.model": MODEL,
            "ro.build.PDA": BUILD,
            "ril.official_cscver": "S928BOXM4BYH2",
            "gsm.version.baseband": BUILD,
            "ril.sales_code": "THL",
            "ro.csc.sales_code": "THL",
            "ro.build.version.release": "15",
            "ro.build.version.oneui": "70000",
            "ro.build.version.security_patch": "2025-08-01",
            "ro.omc.changetype": "DATA_RESET_ON,TRUE",
        }
        self.props.update(kw.get("props", {}))
        self.files = kw.get("files", {})  # path -> ls output
        self.battery = kw.get("battery", "  AC powered: false\n  level: 85\n")
        self.accounts = kw.get("accounts", "User UserInfo{0:Owner:c13}:\n  Accounts: 2\n"
                                           "    Account {name=a@gmail.com, type=com.google}\n"
                                           "    Account {name=a@x.com, type=com.osp.app.signin}")
        self.isub = kw.get("isub", "  isEmbedded=false")
        self.focus = kw.get("focus", FOCUS_TESTMODE)
        self.reboot_rc = kw.get("reboot_rc", 0)
        self.lsof = kw.get("lsof", "")
        self.calls, self.pinned = [], []

    def run(self, argv, **_kw):
        if argv[0] == "lsof":
            out = self.lsof.get(argv[2], "") if isinstance(self.lsof, dict) else self.lsof
            return subprocess.CompletedProcess(argv, 0, out, "")
        assert argv[0] == "adb", argv
        args = argv[1:]
        if args[:1] == ["-s"]:
            assert args[1] == SERIAL, args
            args = args[2:]
            self.pinned.append(True)
        else:
            self.pinned.append(args == ["devices"])
        self.calls.append(args)
        out, rc = "", 0
        if args == ["devices"]:
            out = self.devices
        elif args == ["shell", "getprop"]:
            out = "\n".join(f"[{k}]: [{v}]" for k, v in self.props.items())
        elif args[:1] == ["shell"] and args[1].startswith("ls "):
            d = args[1].split()[1]
            out, rc = (self.files[d], 0) if d in self.files else ("", 1)
        elif args == ["shell", "dumpsys battery"]:
            out = "Current Battery Service state:\n" + self.battery
        elif args == ["shell", "dumpsys account"]:
            out = self.accounts
        elif args == ["shell", "dumpsys isub"]:
            out = self.isub
        elif args == ["shell", "dumpsys window | grep mCurrentFocus"]:
            out = self.focus
        elif args == ["reboot"]:
            rc = self.reboot_rc
        else:
            raise AssertionError(f"unexpected adb call: {args}")
        return subprocess.CompletedProcess(argv, rc, out, "")

    @property
    def rebooted(self):
        return ["reboot"] in self.calls


def fixture_fota(model, csc):
    path = os.path.join(FIXTURES, f"{csc}.xml")
    if not os.path.exists(path):
        return None, []  # what fota_entries returns for Samsung's 403
    with open(path) as f:
        return g.parse_version_xml(f.read())


def offline(model, csc):
    raise OSError("network down")


BUNDLE_DIR = "/optics/configs/carriers/single"
GOOD_BUNDLE = {BUNDLE_DIR: "EUX INS THL XME XSP", f"{BUNDLE_DIR}/INS": "conf etc",
               f"{BUNDLE_DIR}/THL": "conf etc"}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.modem = None
        g.ADB_SERIAL = None
        g.CHANGE_SENT = None
        g.CURRENT_LOG = None

    def tearDown(self):
        if self.modem:
            self.modem.close()

    def run_tool(self, argv, phone=None, modem=None, typed="INS", fota=fixture_fota, usb_match=True,
                 drain=0):
        self.phone = phone or FakePhone()
        if modem is not None:
            self.modem = modem
        ports = [(self.modem.path, usb_match)] if self.modem else []
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(g.subprocess, "run", self.phone.run))
            st.enter_context(mock.patch.object(g, "fota_entries", fota))
            st.enter_context(mock.patch.object(g, "find_modem_ports", lambda s: ports))
            st.enter_context(mock.patch.object(g, "LOG_DIR", self.tmp.name))
            st.enter_context(mock.patch.object(g, "AT_GAP", 0))
            st.enter_context(mock.patch.object(g, "DRAIN", drain))
            st.enter_context(mock.patch.object(g, "DRAIN_MAX", 1.0))
            st.enter_context(mock.patch.object(g, "TESTMODE_WAIT", 0.2))
            st.enter_context(mock.patch.object(g.time, "sleep", lambda s: None))
            inp = typed if callable(typed) else (lambda prompt="": typed)
            st.enter_context(mock.patch("builtins.input", inp))
            st.enter_context(mock.patch.object(sys, "argv", ["samregionhop", *argv]))
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            try:
                g.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def sent(self):
        return self.modem.received if self.modem else []

    def logs(self):
        text = ""
        for name in os.listdir(self.tmp.name):
            with open(os.path.join(self.tmp.name, name)) as f:
                text += f.read()
        return text

    def assertNoWrite(self):
        self.assertNotIn("AT+PRECONFG=2,INS", self.sent())
        self.assertFalse(self.phone.rebooted)


# ---------------------------------------------------------------------------

class TestAllowlist(unittest.TestCase):
    def test_modem_refuses_commands_outside_allowlist(self):
        modem = FakeModem()
        try:
            m = g.Modem(modem.path, lambda t: None)
            for bad in ["AT+CFUN=1,1", "AT+FUS?", "AT+FUN=0", "ATD*21*123#;", "AT+KSTRINGB=0,3",
                        "AT+PRECONFG=2,INS\r\nAT+CFUN=1,1", "AT+PRECONFG=2,ins", "AT+SWATD=2",
                        "AT+PRECONFG=2,INSX", "at", "AT ", ""]:
                with self.assertRaises(ValueError, msg=bad):
                    m.send(bad)
            m.close()
            time.sleep(0.3)  # give the fake modem time to see any stray bytes
            self.assertEqual(modem.received, [], "nothing may reach the modem")
        finally:
            modem.close()

    def test_source_has_no_at_literals_outside_allowlist(self):
        with open(g.__file__) as f:
            src = f.read()
        literals = re.findall(r"""["'](AT[^"']*)["']""", src)
        literals = [l.replace("{target}", "INS") for l in literals]
        self.assertTrue(literals)
        notices = set(g.SWATD_TARGET.values())  # routing names in modem notices, never sent
        for lit in literals:
            if lit.startswith("AT\\+") or lit in notices:
                continue  # the allowlist patterns themselves
            self.assertTrue(any(re.fullmatch(p, lit) for p in g.ALLOWED_AT), lit)


class TestParsing(unittest.TestCase):
    def test_result_codes_from_real_captures(self):
        self.assertEqual(g.result_code("AT\r\r\nOK\r\n"), "OK")
        self.assertEqual(g.result_code("\r\n+PRECONFG:2,OK\r\n\r\nOK\r\n"), "OK")
        self.assertEqual(g.result_code("\r\n+CME Error:PACM(AP),PROTECTED_NO_TOK\r\n"),
                         "+CME ERROR:PACM(AP),PROTECTED_NO_TOK")
        self.assertEqual(g.result_code("\r\nERROR\r\n"), "ERROR")
        self.assertIsNone(g.result_code("\r\n+PRECONFG:2,OK\r\n"))  # no final code yet
        self.assertIsNone(g.result_code(""))

    def test_preconfg_ok_is_strict(self):
        cmd = "AT+PRECONFG=2,INS"
        echo = cmd + "\r\r\n"
        self.assertTrue(g.preconfg_ok(echo + "+PRECONFG:2,OK\r\n\r\nOK\r\n", cmd))
        self.assertTrue(g.preconfg_ok(echo + "OK\r\n", cmd))
        self.assertTrue(g.preconfg_ok("\r\n+PRECONFG:2,OK\r\n\r\nOK\r\n", cmd))  # no echo
        for bad in ["+PRECONFG:2,NG", "+PRECONFG:2,NOK", "+PRECONFG:2,NOT_ALLOWED",
                    "+PRECONFG:2,0", "+PRECONFG:2,OKAY", "NG", "FAIL", "PRECONFG 2,NG",
                    "+PRECONFG:2,OK\r\nNG"]:
            self.assertFalse(g.preconfg_ok(echo + bad + "\r\n\r\nOK\r\n", cmd), bad)
        self.assertFalse(g.preconfg_ok(echo + "+PRECONFG:2,OK\r\n", cmd))  # no final OK
        self.assertFalse(g.preconfg_ok(echo + "ERROR\r\n", cmd))
        self.assertFalse(g.preconfg_ok("", cmd))

    def test_hash_ok_marker_is_tolerated_but_nothing_else(self):
        cmd = "AT+PRECONFG=2,INS"
        self.assertTrue(g.preconfg_ok("+PRECONFG:2,OK\r\n#OK#\r\n\r\nOK\r\n", cmd))
        for bad in ["#NG#", "#ERROR#", "#OK#x", "##OK##"]:
            self.assertFalse(g.preconfg_ok(f"+PRECONFG:2,OK\r\n{bad}\r\n\r\nOK\r\n", cmd), bad)
        self.assertEqual(g.reply_lines("AT\r\r\n#OK#\r\nOK\r\n", "AT"), ["OK"])

    def test_swatd_ok_real_captures(self):
        real0 = "[DR]: CHANGE TO DDEXEAT+SWATD=0\r\n[ATD]: CHANGE TO DDEXE"
        real1 = "[DR]: CHANGE TO ATDAT+SWATD=1\r\n[ATD]: CHANGE TO ATD"
        self.assertTrue(g.swatd_ok(real0, "0"))
        self.assertTrue(g.swatd_ok(real1, "1"))
        self.assertFalse(g.swatd_ok(real0, "1"))  # wrong direction
        self.assertFalse(g.swatd_ok(real1, "0"))
        self.assertTrue(g.swatd_ok("AT+SWATD=0\r\r\nOK\r\n", "0"))  # plain OK still fine
        self.assertFalse(g.swatd_ok(real0 + "\r\nERROR\r\n", "0"))
        self.assertFalse(g.swatd_ok("[ATD]: CHANGE TO DDEXE\r\n+CME ERROR: 3\r\n", "0"))
        self.assertFalse(g.swatd_ok("[ATD]: CHANGE TO DDEXE\r\n[ATD]: CHANGE TO ATD", "0"))
        self.assertFalse(g.swatd_ok("[DR]: CHANGE TO DDEXE", "0"))  # needs the [ATD] notice
        self.assertFalse(g.swatd_ok("", "0"))
        self.assertFalse(g.swatd_ok("[ATD]: CHANGE TO ATD\r\nOK\r\n", "0"))  # OK can't override
        self.assertFalse(g.swatd_ok("[ATD]: CHANGE TO DDEXE\r\nOK\r\n", "1"))

    def test_csc_family(self):
        self.assertEqual(g.csc_family("SM-S928B", "S928BOXM4BYH2"), "OXM")
        self.assertIsNone(g.csc_family("SM-S928B", "S921BOXM4BYH2"))
        self.assertIsNone(g.csc_family("SM-S928B", ""))
        self.assertIsNone(g.csc_family("SM-S928B", None))

    def test_build_key_orders_builds(self):
        self.assertLess(g.build_key("S928BXXU1AWM9"), g.build_key("S928BXXS4BYH2"))
        self.assertLess(g.build_key("S928BXXS4BYH2"), g.build_key("S928BXXU6EZIF"))
        self.assertIsNone(g.build_key("S928BXXU1AMA1"))  # M is not a year letter
        self.assertLess(g.build_key("S928BXXU6EZIF"), g.build_key("S928BXXU7FAA1"))  # 2026 < 2027

    def test_wipe_status(self):
        self.assertEqual(g.wipe_status("DATA_RESET_ON,TRUE"), "yes")
        self.assertEqual(g.wipe_status("data_reset_on,false"), "no")
        self.assertEqual(g.wipe_status(""), "unknown")

    def test_mask_hides_imei(self):
        self.assertEqual(g.mask("SN(R5C);IMEI(350000000000001);"), "SN(R5C);IMEI(***************);")
        self.assertEqual(g.mask("MN(SM);PN(910000000000,);UN(ABCD1234);SN(R5C)"),
                         "MN(SM);PN(*************);UN(********);SN(R5C)")

    def test_real_fota_data_explains_the_stuck_phone(self):
        _, thl = fixture_fota(MODEL, "THL")
        _, eux = fixture_fota(MODEL, "EUX")
        self.assertIn(BUILD, {e.split("/")[0] for e in eux})
        self.assertNotIn(BUILD, {e.split("/")[0] for e in thl})

    def test_fota_403_means_no_firmware(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(g.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(g.fota_entries(MODEL, "XAA"), (None, []))
        err = urllib.error.HTTPError("u", 500, "Oops", {}, None)
        with mock.patch.object(g.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                g.fota_entries(MODEL, "INS")


class TestReadOnlyCommands(Harness):
    def test_info_is_read_only(self):
        self.assertEqual(self.run_tool(["info"]), 0)
        self.assertIn("THL", self.out)
        self.assertIn("yes", self.out)  # wipe on change
        for call in self.phone.calls:
            self.assertIn(call[0], ("devices", "shell"))
            if call[0] == "shell":
                self.assertRegex(call[1], r"^(getprop|ls |dumpsys )")

    def test_fota_offline_build(self):
        self.assertEqual(self.run_tool(["fota", "THL", "INS", "EUX", "--build", BUILD]), 0)
        self.assertIn("THL: NO OTA path", self.out)
        self.assertIn("INS: NO OTA path", self.out)
        self.assertIn("EUX: OTA path exists", self.out)
        self.assertEqual(self.phone.calls, [])

    def test_fota_from_phone_always_includes_active_csc(self):
        self.assertEqual(self.run_tool(["fota", "EUX"]), 0, self.err)
        self.assertLess(self.out.index("THL:"), self.out.index("EUX:"))  # active region first
        self.assertEqual(self.out.count("EUX:"), 1)
        self.assertEqual(self.run_tool(["fota", "THL", "EUX"]), 0, self.err)
        self.assertEqual(self.out.count("THL:"), 1)  # no duplicate

    def test_probe_sends_only_read_only_at(self):
        self.assertEqual(self.run_tool(["probe"], modem=FakeModem()), 0, self.err)
        self.assertEqual(self.sent(), IDENTITY)
        self.assertIn("USB serial, modem serial", self.out)
        self.assertFalse(self.phone.rebooted)
        self.assertNotIn("350000000000001", self.out + self.logs())

    def test_every_adb_call_is_pinned_to_the_phone(self):
        self.assertEqual(self.run_tool(["change", "INS"], modem=FakeModem()), 0, self.err)
        self.assertTrue(all(self.phone.pinned), self.phone.calls)


class TestChangeHappyPath(Harness):
    def test_change_to_ins(self):
        code = self.run_tool(["change", "INS"], modem=FakeModem())
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.sent(), [*IDENTITY, *CHANGE_CMDS])
        self.assertTrue(self.phone.rebooted)
        self.assertIn("reports the pending CSC as INS", self.out)
        self.assertIn("WILL factory reset", self.out)
        self.assertIn("A Google account is signed in", self.out)
        self.assertIn("A Samsung account is signed in", self.out)
        self.assertIn("use the OXM bundle", self.out)
        self.assertIn("both before and after", self.out)
        self.assertIn("INS has no OTA update path", self.out)
        self.assertIn("AT+PRECONFG=2,INS", self.logs())
        self.assertNotIn("350000000000001", self.logs())

    def test_change_with_real_s928b_swatd_replies(self):
        modem = FakeModem({"AT+SWATD=0": "[DR]: CHANGE TO DDEXE\r\n[ATD]: CHANGE TO DDEXE",
                           "AT+SWATD=1": "[DR]: CHANGE TO ATD\r\n[ATD]: CHANGE TO ATD"})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 0, self.err)
        self.assertEqual(self.sent(), [*IDENTITY, *CHANGE_CMDS])
        self.assertTrue(self.phone.rebooted)

    def test_modem_swallowing_first_at_while_starting_up(self):
        replies = iter(["+USB  READY", "OK"])  # real S928B: first AT lost to the startup notice
        modem = FakeModem({"AT": lambda m: next(replies)})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 0, self.err)
        self.assertEqual(self.sent()[:3], ["AT", "AT", "AT+DEVCONINFO"])

    def test_bundle_listing_is_used_when_readable(self):
        phone = FakePhone(files=GOOD_BUNDLE)
        self.assertEqual(self.run_tool(["change", "INS"], phone, FakeModem()), 0, self.err)
        self.assertIn("populated folder", self.out)
        self.assertIn("use the OXM bundle", self.out)  # both sources checked

    def test_server_blocking_us_falls_back_to_bundle(self):
        phone = FakePhone(files=GOOD_BUNDLE)
        nothing = lambda model, csc: (None, [])  # noqa: E731  (403 for every CSC)
        self.assertEqual(self.run_tool(["change", "INS"], phone, FakeModem(), fota=nothing), 0, self.err)
        self.assertIn("returned nothing for THL either", self.out)

    def test_reboot_permission_error_is_reported_not_fatal(self):
        phone = FakePhone()
        real = phone.run
        def run(argv, **kw):
            if argv[-1:] == ["reboot"]:
                raise PermissionError("adb not executable")
            return real(argv, **kw)
        phone.run = run
        self.assertEqual(self.run_tool(["change", "INS"], phone, FakeModem()), 0, self.err)
        self.assertIn("Reboot by hand", self.out)

    def test_whitespace_after_reply_does_not_cancel_reboot(self):
        def ok_then_blank(m):
            m.pending = "INS"
            m.say_later("\r\n", delay=0.05)
            return "+PRECONFG:2,OK\r\n\r\nOK"
        modem = FakeModem({"AT+PRECONFG=2,INS": ok_then_blank})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem, drain=0.3), 0, self.err)
        self.assertTrue(self.phone.rebooted)

    def test_reboot_failure_is_reported_not_fatal(self):
        phone = FakePhone(reboot_rc=1)
        self.assertEqual(self.run_tool(["change", "INS"], phone, FakeModem()), 0, self.err)
        self.assertIn("Reboot by hand", self.out)

    def test_devconinfo_unsupported_falls_back_to_ati_when_usb_proves_serial(self):
        modem = FakeModem({"AT+DEVCONINFO": "ERROR"})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 0, self.err)
        self.assertEqual(self.sent()[:3], ["AT", "AT+DEVCONINFO", "ATI"])

    def test_modem_serial_proves_identity_without_usb_serial(self):
        self.assertEqual(self.run_tool(["change", "INS"], modem=FakeModem(), usb_match=False), 0, self.err)


class TestChangeRefusesBeforeTouchingModem(Harness):
    """Each of these must stop before a single AT command is sent."""

    def assertRefused(self, code, needle):
        self.assertEqual(code, 1)
        self.assertIn(needle, self.err + self.out)
        self.assertEqual(self.sent(), [])
        self.assertFalse(self.phone.rebooted)

    def test_invalid_code(self):
        self.assertRefused(self.run_tool(["change", "IN"], modem=FakeModem()), "3-character")
        self.assertRefused(self.run_tool(["change", "IN;S"], modem=FakeModem()), "3-character")

    def test_already_on_target(self):
        self.assertRefused(self.run_tool(["change", "THL"], modem=FakeModem()), "already on THL")

    def test_target_not_in_bundle_listing(self):
        phone = FakePhone(files={BUNDLE_DIR: "EUX THL XME XSP"})
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "not in this phone's CSC bundle")

    def test_target_folder_empty(self):
        phone = FakePhone(files={BUNDLE_DIR: "EUX INS THL XME"})
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "folder in the phone's CSC bundle is empty")

    def test_bundle_says_yes_but_server_publishes_nothing(self):
        phone = FakePhone(files={BUNDLE_DIR: "AIS THL TMV DTC", f"{BUNDLE_DIR}/AIS": "conf"})
        self.assertRefused(self.run_tool(["change", "AIS"], phone, FakeModem(), typed="AIS"),
                           "publishes no AIS firmware")

    def test_target_with_no_published_firmware(self):
        self.assertRefused(self.run_tool(["change", "XAA"], modem=FakeModem(), typed="XAA"),
                           "publishes no XAA firmware")

    def test_target_from_another_bundle(self):
        def fota(model, csc):
            if csc == "THL":
                return fixture_fota(model, csc)
            return g.parse_version_xml("<latest o='1'>S928BXXU6EZIF/S928BOYM6EZIF/S928BXXU6EZIF</latest>")
        self.assertRefused(self.run_tool(["change", "TMB"], modem=FakeModem(), typed="TMB", fota=fota),
                           "uses the OYM bundle")

    def test_unverifiable_when_offline_and_bundle_unreadable(self):
        self.assertRefused(self.run_tool(["change", "INS"], modem=FakeModem(), fota=offline),
                           "refusing to guess")

    def test_server_blocking_us_and_bundle_unreadable(self):
        nothing = lambda model, csc: (None, [])  # noqa: E731
        self.assertRefused(self.run_tool(["change", "INS"], modem=FakeModem(), fota=nothing),
                           "refusing to guess")

    def test_tiny_bundle_listing_is_not_trusted(self):
        # A folder holding only the active CSC must not count as the bundle.
        phone = FakePhone(files={"/optics/configs/carriers": "THL"})
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem(), fota=offline),
                           "refusing to guess")

    def test_low_battery(self):
        self.assertRefused(self.run_tool(["change", "INS"], FakePhone(battery="  level: 30\n"), FakeModem()),
                           "battery is 30%")

    def test_unknown_battery(self):
        self.assertRefused(self.run_tool(["change", "INS"], FakePhone(battery=""), FakeModem()),
                           "battery is unknown")

    def test_faked_battery(self):
        phone = FakePhone(battery="  (UPDATES STOPPED -- use 'reset' to restart)\n  level: 100\n")
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "battery is unknown")

    def test_not_samsung(self):
        phone = FakePhone(props={"ro.product.manufacturer": "Google"})
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "not a Samsung")

    def test_missing_props(self):
        phone = FakePhone(props={"ril.official_cscver": ""})
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "csc build")

    def test_unauthorized_adb(self):
        phone = FakePhone(devices=f"List of devices attached\n{SERIAL}\tunauthorized\n")
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "not ready")

    def test_two_phones(self):
        phone = FakePhone(devices=f"List of devices attached\n{SERIAL}\tdevice\nOTHER\tdevice\n")
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "more than one")

    def test_no_phone(self):
        phone = FakePhone(devices="List of devices attached\n\n")
        self.assertRefused(self.run_tool(["change", "INS"], phone, FakeModem()), "no phone found")

    def test_test_mode_not_open(self):
        self.assertRefused(self.run_tool(["change", "INS"], FakePhone(focus=FOCUS_HOME), FakeModem()),
                           "Test Mode (*#0*#) was not detected")

    def test_no_modem_port(self):
        self.assertRefused(self.run_tool(["change", "INS"]), "modem port did not appear")

    def test_port_used_by_another_app(self):
        self.assertRefused(self.run_tool(["change", "INS"], FakePhone(lsof="4242\n"), FakeModem()),
                           "open in another app")


class TestWarnings(Harness):
    def test_unreadable_accounts_still_warn(self):
        phone = FakePhone(accounts="Permission Denial: can't dump AccountManager")
        self.assertEqual(self.run_tool(["change", "INS", "--dry-run"], phone, FakeModem()), 0, self.err)
        self.assertIn("If a Google account is signed in (couldn't check)", self.out)
        self.assertIn("If a Samsung account is signed in (couldn't check)", self.out)

    def test_no_accounts_no_warning(self):
        phone = FakePhone(accounts="User UserInfo{0:Owner:c13}:\n  Accounts: 0\n")
        self.assertEqual(self.run_tool(["change", "INS", "--dry-run"], phone, FakeModem()), 0, self.err)
        self.assertNotIn("Factory Reset Protection", self.out)

    def test_unreadable_isub_warns(self):
        phone = FakePhone(isub="Can't find service: isub")
        self.assertEqual(self.run_tool(["change", "INS", "--dry-run"], phone, FakeModem()), 0, self.err)
        self.assertIn("Couldn't tell if an eSIM", self.out)

    def test_esim_present_warns(self):
        phone = FakePhone(isub="  isEmbedded=true")
        self.assertEqual(self.run_tool(["change", "INS", "--dry-run"], phone, FakeModem()), 0, self.err)
        self.assertIn("An eSIM is installed", self.out)


class TestChangeStopsBeforeWrite(Harness):
    """Failures before AT+PRECONFG=2 must say nothing changed and exit 1."""

    def check(self, code, needle):
        self.assertEqual(code, 1, self.out + self.err)
        self.assertIn(needle, self.err)
        self.assertIn("Nothing was changed", self.err)
        self.assertNoWrite()

    def test_silent_port(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem({"AT": None})), "no port answered AT")
        self.assertEqual(self.sent(), ["AT", "AT", "AT"])  # retried, then gave up

    def test_wrong_model_on_modem(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(model="SM-S921B")), "modem reports model")
        self.assertEqual(self.sent(), IDENTITY)

    def test_wrong_serial_on_modem(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(serial="OTHERPHONE")), "modem reports serial")

    def test_wrong_firmware_on_modem(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(build="S928BXXU6EZIF")), "modem reports firmware")

    def test_identity_unprovable_without_any_serial(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(serial=""), usb_match=False),
                   "couldn't prove this modem port belongs")

    def test_ati_fallback_without_usb_serial_is_not_enough(self):
        modem = FakeModem({"AT+DEVCONINFO": "ERROR"})
        self.check(self.run_tool(["change", "INS"], modem=modem, usb_match=False),
                   "couldn't prove this modem port belongs")

    def test_ati_without_model(self):
        modem = FakeModem({"AT+DEVCONINFO": "ERROR", "ATI": "OK"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "did not identify")

    def test_dry_run(self):
        self.assertEqual(self.run_tool(["change", "INS", "--dry-run"], modem=FakeModem()), 0, self.err)
        self.assertEqual(self.sent(), IDENTITY)
        self.assertIn("Nothing was changed", self.out)
        self.assertNoWrite()

    def test_wrong_confirmation(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(), typed="yes"), "aborted")
        self.assertEqual(self.sent(), IDENTITY)

    def test_eof_at_confirmation(self):
        def eof(prompt=""):
            raise EOFError
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(), typed=eof), "aborted")

    def test_ctrl_c_at_confirmation(self):
        def interrupt(prompt=""):
            raise KeyboardInterrupt
        self.check(self.run_tool(["change", "INS"], modem=FakeModem(), typed=interrupt), "interrupted")
        self.assertEqual(self.sent(), IDENTITY)

    def test_test_mode_lost_while_waiting_at_prompt(self):
        phone = FakePhone()
        def lock_then_confirm(prompt=""):
            phone.focus = FOCUS_HOME
            return "INS"
        self.check(self.run_tool(["change", "INS"], phone, FakeModem(), typed=lock_then_confirm),
                   "no longer on screen")
        self.assertEqual(self.sent(), IDENTITY)

    def test_swatd_refused(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem({"AT+SWATD=0": "ERROR"})), "refused SWATD")
        # Nothing after SWATD=0 except the cleanup that restores SWATD=1.
        self.assertEqual(self.sent(), [*IDENTITY, "AT+SWATD=0", "AT+SWATD=1"])

    def test_swatd0_wrong_direction_notice(self):
        modem = FakeModem({"AT+SWATD=0": "[ATD]: CHANGE TO ATD"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "refused SWATD")

    def test_swatd1_real_notice_missing_after_real_swatd0(self):
        modem = FakeModem({"AT+SWATD=0": "[ATD]: CHANGE TO DDEXE", "AT+SWATD=1": None})
        self.check(self.run_tool(["change", "INS"], modem=modem), "didn't confirm SWATD=1")

    def test_error_trailing_a_swatd_notice_is_caught(self):
        def notice_then_error(m):
            m.say_later("\r\n+CME ERROR: 3\r\n", delay=0.02)
            return "[ATD]: CHANGE TO DDEXE"
        modem = FakeModem({"AT+SWATD=0": notice_then_error})
        self.check(self.run_tool(["change", "INS"], modem=modem, drain=0.2), "refused SWATD")

    def test_unconfirmed_cleanup_tells_user_to_restart(self):
        modem = FakeModem({"AT+SWATD=0": "ERROR", "AT+SWATD=1": None})
        self.check(self.run_tool(["change", "INS"], modem=modem), "refused SWATD")
        self.assertIn("Restart the phone to restore it", self.err)

    def test_confirmed_cleanup_says_nothing_extra(self):
        modem = FakeModem({"AT+SWATD=0": "ERROR"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "refused SWATD")
        self.assertNotIn("Restart the phone", self.err)

    def test_swatd1_not_confirmed(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem({"AT+SWATD=1": "ERROR"})),
                   "didn't confirm SWATD=1")
        self.assertEqual(self.sent(), [*IDENTITY, *UNLOCK, "AT+SWATD=1"])  # + cleanup retry

    def test_pre_read_unreadable(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem({"AT+PRECONFG=1,0": "ERROR"})),
                   "couldn't read the CSC")
        self.assertEqual(self.sent(), [*IDENTITY, *UNLOCK, "AT+PRECONFG=1,0"])

    def test_pre_read_with_unsolicited_line(self):
        modem = FakeModem({"AT+PRECONFG=1,0": "RING\r\n+PRECONFG:1,THL\r\n\r\nOK"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "couldn't read the CSC")

    def test_pre_read_unfamiliar_format(self):
        modem = FakeModem({"AT+PRECONFG=1,0": "+PRECONFG:1,THL,INS\r\n\r\nOK"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "couldn't read the CSC")

    def test_noisy_port_before_write(self):
        def chatter(m):
            m.start_noise(every=0.005)
            return "OK"
        modem = FakeModem({"AT+SWATD=1": chatter})
        # Either the drain gives up or the noisy reply can't be read; both must stop
        # before the write. (check() asserts no write and no reboot.)
        self.check(self.run_tool(["change", "INS"], modem=modem, drain=0.05), "Nothing was changed")
        self.assertRegex(self.err, "never went quiet|couldn't read the CSC")

    def test_termios_error_before_write(self):
        import termios
        real_send = g.Modem.send
        def send(self_, cmd, timeout=10.0, **kw):
            if cmd == "AT+ACTIVATE=0,0,0":
                raise termios.error(9, "Bad file descriptor")
            return real_send(self_, cmd, timeout, **kw)
        with mock.patch.object(g.Modem, "send", send):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "error: ")


    def test_ctrl_c_during_unlock_restores_swatd(self):
        real_send = g.Modem.send
        def send(self_, cmd, timeout=10.0, **kw):
            if cmd == "AT+ACTIVATE=0,0,0":
                raise KeyboardInterrupt  # before ACTIVATE is written
            return real_send(self_, cmd, timeout, **kw)
        with mock.patch.object(g.Modem, "send", send):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "interrupted")
        self.assertEqual(self.sent(), [*IDENTITY, "AT+SWATD=0", "AT+SWATD=1"])

    def test_usb_drop_before_write(self):
        import serial
        real_send = g.Modem.send
        def send(self_, cmd, timeout=10.0, **kw):
            if cmd == "AT+SWATD=1":
                raise serial.SerialException("device disconnected")
            return real_send(self_, cmd, timeout, **kw)
        with mock.patch.object(g.Modem, "send", send):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "error: ")


class TestChangeFindsEarlierPendingChange(Harness):
    """A CSC already pending must never be met with 'reboot normally'."""

    def check(self, pending, needle):
        modem = FakeModem({"AT+PRECONFG=1,0": f"+PRECONFG:1,{pending}\r\n\r\nOK"})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 2, self.out + self.err)
        self.assertIn(needle, self.out)
        self.assertIn("WIPE", self.out)
        self.assertNotIn("reboot the phone normally", self.out + self.err)
        self.assertEqual(self.sent(), [*IDENTITY, *UNLOCK, "AT+PRECONFG=1,0"])
        self.assertFalse(self.phone.rebooted)

    def test_other_csc_pending(self):
        self.check("EUX", "already holds EUX while")

    def test_target_already_pending(self):
        self.check("INS", "already holds INS (the CSC you asked for)")

    def test_pending_change_inside_messy_reply(self):
        modem = FakeModem({"AT+PRECONFG=1,0": "+PRECONFG:1,INS\r\nRING\r\n\r\nOK"})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 2, self.out + self.err)
        self.assertIn("already holds INS", self.out)
        self.assertNotIn("reboot the phone normally", self.out + self.err)
        self.assertNoWrite()

    def test_messy_reply_showing_only_active_never_says_reboot_normally(self):
        modem = FakeModem({"AT+PRECONFG=1,0": "RING\r\n+PRECONFG:1,THL\r\n\r\nOK"})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 1)
        self.assertIn("back up before rebooting", self.err)
        self.assertNotIn("reboot the phone normally", self.err)
        self.assertNoWrite()

class TestChangeAfterWriteNeverClaimsNothingChanged(Harness):
    """Once AT+PRECONFG=2 is sent: no 'nothing changed', no auto-reboot unless confirmed."""

    def check(self, code, needle):
        self.assertEqual(code, 2, self.out + self.err)
        self.assertIn(needle, self.out + self.err)
        self.assertNotIn("Nothing was changed", self.out + self.err)
        self.assertIn("AT+PRECONFG=2,INS", self.sent())
        self.assertFalse(self.phone.rebooted)

    def test_write_rejected_ng(self):
        modem = FakeModem({"AT+PRECONFG=2,INS": "+PRECONFG:2,NG\r\n\r\nOK"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNCERTAIN")
        self.assertIn("didn't accept the change", self.out)
        self.assertEqual(self.sent()[-1], "AT+PRECONFG=1,0")  # read back anyway

    def test_write_cme_error(self):
        modem = FakeModem({"AT+PRECONFG=2,INS": "+CME Error:PACM(AP),PROTECTED_NO_TOK"})
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNCERTAIN")

    def test_write_silent(self):
        self.check(self.run_tool(["change", "INS"], modem=FakeModem({"AT+PRECONFG=2,INS": None})),
                   "STATE UNCERTAIN")

    def test_write_unusual_reply_but_pending_is_target(self):
        def odd(m):
            m.pending = "INS"
            return "+PRECONFG:2,NOT_ALLOWED\r\n\r\nOK"
        self.check(self.run_tool(["change", "INS"], modem=FakeModem({"AT+PRECONFG=2,INS": odd})),
                   "its reply was not clean")

    def test_readback_still_old_csc(self):
        modem = FakeModem({"AT+PRECONFG=1,0": readback_sequence(
            "+PRECONFG:1,THL\r\n\r\nOK", "+PRECONFG:1,THL\r\n\r\nOK")})
        # Acknowledged but still THL: the read-back may only show the active CSC.
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNKNOWN")
        self.assertIn("acknowledged the change", self.out)
        self.assertNotIn("most likely did not take", self.out)

    def test_readback_unavailable(self):
        modem = FakeModem({"AT+PRECONFG=1,0": readback_sequence("+PRECONFG:1,THL\r\n\r\nOK", "ERROR")})
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNKNOWN")

    def test_readback_needs_exact_code(self):
        modem = FakeModem({"AT+PRECONFG=1,0": readback_sequence(
            "+PRECONFG:1,THL\r\n\r\nOK", "+PRECONFG:1,INSX\r\n\r\nOK")})
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNKNOWN")

    def test_readback_unexpected_third_csc(self):
        modem = FakeModem({"AT+PRECONFG=1,0": readback_sequence(
            "+PRECONFG:1,THL\r\n\r\nOK", "+PRECONFG:1,XME\r\n\r\nOK")})
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNKNOWN")

    def test_late_ng_after_ok_cancels_reboot(self):
        def ok_then_ng(m):
            m.pending = "INS"
            m.say_later("\r\n+PRECONFG:2,NG\r\n", delay=0.05)
            return "OK"
        modem = FakeModem({"AT+PRECONFG=2,INS": ok_then_ng})
        # Real drain window, so the late line is caught by the next command's drain.
        self.check(self.run_tool(["change", "INS"], modem=modem, drain=0.3), "sent more after")
        self.assertIn("PRECONFG:2,NG", self.out)

    def test_noisy_port_after_write(self):
        def ok_then_noise(m):
            m.pending = "INS"
            m.start_noise()
            return "+PRECONFG:2,OK\r\n\r\nOK"
        modem = FakeModem({"AT+PRECONFG=2,INS": ok_then_noise})
        self.check(self.run_tool(["change", "INS"], modem=modem, drain=0.05), "STATE UNKNOWN")
        self.assertIn("never went quiet", self.out)

    def test_termios_error_after_write(self):
        import termios
        real_send = g.Modem.send
        def send(self_, cmd, timeout=10.0, **kw):
            r = real_send(self_, cmd, timeout, **kw)
            if cmd.startswith("AT+PRECONFG=2,"):
                raise termios.error(9, "Bad file descriptor")
            return r
        with mock.patch.object(g.Modem, "send", send):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "STATE UNKNOWN")

    def test_unexpected_bug_after_write(self):
        with mock.patch.object(g, "read_pending", side_effect=[("THL", "+PRECONFG:1,THL\r\n\r\nOK"), RuntimeError("boom")]):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "STATE UNKNOWN")

    def test_hash_ok_marker_in_replies_does_not_block_a_normal_change(self):
        modem = FakeModem({
            "AT+PRECONFG=1,0": lambda m: f"+PRECONFG:1,{m.pending}\r\n#OK#\r\n\r\nOK",
            "AT+PRECONFG=2,INS": lambda m: (setattr(m, "pending", "INS"),
                                           "+PRECONFG:2,OK\r\n#OK#\r\n\r\nOK")[1]})
        self.assertEqual(self.run_tool(["change", "INS"], modem=modem), 0, self.err)
        self.assertTrue(self.phone.rebooted)

    def test_readback_with_extra_negative_line(self):
        modem = FakeModem({"AT+PRECONFG=1,0": readback_sequence(
            "+PRECONFG:1,THL\r\n\r\nOK", "+PRECONFG:2,NG\r\n+PRECONFG:1,INS\r\n\r\nOK")})
        self.check(self.run_tool(["change", "INS"], modem=modem), "STATE UNKNOWN")

    def test_error_after_try_block_still_warns(self):
        with mock.patch.object(g, "adb_rc", side_effect=RuntimeError("boom")):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "STATE UNKNOWN")
        self.assertNotIn("No CSC change was attempted", self.out + self.err)

    def test_ctrl_c_during_reboot_still_warns(self):
        with mock.patch.object(g, "adb_rc", side_effect=KeyboardInterrupt):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "STATE UNKNOWN")

    def test_ctrl_c_after_write(self):
        real_send = g.Modem.send
        calls = {"n": 0}
        def send(self_, cmd, timeout=10.0, **kw):
            if cmd == "AT+PRECONFG=1,0":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise KeyboardInterrupt
            return real_send(self_, cmd, timeout, **kw)
        with mock.patch.object(g.Modem, "send", send):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "STATE UNKNOWN")

    def test_usb_drop_after_write(self):
        import serial
        real_send = g.Modem.send
        def send(self_, cmd, timeout=10.0, **kw):
            r = real_send(self_, cmd, timeout, **kw)
            if cmd.startswith("AT+PRECONFG=2,"):
                raise serial.SerialException("device disconnected")
            return r
        with mock.patch.object(g.Modem, "send", send):
            self.check(self.run_tool(["change", "INS"], modem=FakeModem()), "STATE UNKNOWN")


class TestMisc(Harness):
    def test_busy_tty_twin_is_detected(self):
        modem = FakeModem()
        twin = modem.path.replace("/dev/cu.", "/dev/tty.")
        phone = FakePhone(lsof={twin: "4242\n"})
        with mock.patch.object(g, "find_modem_ports", lambda s: [("/dev/cu.usbmodemX", True)]):
            busy = None
            with mock.patch.object(g.subprocess, "run", phone.run):
                busy = g.port_busy("/dev/cu.usbmodemX")
        self.assertFalse(busy)  # twin of a different path
        with mock.patch.object(g.subprocess, "run", FakePhone(lsof={"/dev/tty.usbmodemX": "4242\n"}).run):
            self.assertTrue(g.port_busy("/dev/cu.usbmodemX"))
        with mock.patch.object(g.subprocess, "run", FakePhone(lsof={"/dev/cu.usbmodemX": str(os.getpid())}).run):
            self.assertFalse(g.port_busy("/dev/cu.usbmodemX"))  # our own process
        modem.close()

    def test_unexpected_error_before_modem_is_safe(self):
        with mock.patch.object(g, "battery_level", side_effect=RuntimeError("boom")):
            code = self.run_tool(["change", "INS"], modem=FakeModem())
        self.assertEqual(code, 1)
        self.assertIn("No CSC change was attempted", self.err)
        self.assertEqual(self.sent(), [])


class TestTargets(Harness):
    BUNDLE = {BUNDLE_DIR: "EUX INS THL XME XSP", f"{BUNDLE_DIR}/EUX": "conf",
              f"{BUNDLE_DIR}/INS": "conf", f"{BUNDLE_DIR}/THL": "conf", f"{BUNDLE_DIR}/XME": "conf"}

    def test_only_reliable_regions_are_offered(self):
        # XME: folder but no Samsung firmware (fixture absent). XSP: no folder. THL: current.
        self.assertEqual(self.run_tool(["targets", "-v"], FakePhone(files=self.BUNDLE)), 0, self.err)
        self.assertIn("Reliable targets: EUX INS", self.out)
        self.assertIn("XME: Samsung publishes no XME firmware", self.out)
        self.assertIn("XSP: XSP's folder in the phone's CSC bundle is empty", self.out)
        self.assertIn("India", self.out)  # labels come from regions.json
        self.assertNotIn("THL:", self.out)  # the current region is not a target

    def test_targets_from_another_bundle_are_not_offered(self):
        def fota(model, csc):
            if csc == "EUX":
                return g.parse_version_xml("<latest o='1'>S928BXXU6EZIF/S928BOYM6EZIF/S928BXXU6EZIF</latest>")
            return fixture_fota(model, csc)
        self.assertEqual(self.run_tool(["targets", "-v"], FakePhone(files=self.BUNDLE), fota=fota), 0, self.err)
        self.assertIn("Reliable targets: INS", self.out)
        self.assertIn("uses the OYM bundle", self.out)

    def test_unreadable_bundle_offers_nothing(self):
        self.assertEqual(self.run_tool(["targets"]), 0, self.err)
        self.assertIn("Reliable targets: none", self.out)
        self.assertIn("doesn't list its regions", self.out)

    def test_offline_offers_nothing(self):
        self.assertEqual(self.run_tool(["targets"], FakePhone(files=self.BUNDLE), fota=offline), 0, self.err)
        self.assertIn("Reliable targets: none", self.out)
        self.assertIn("couldn't reach Samsung", self.out)

    def test_server_blocking_offers_nothing(self):
        nothing = lambda model, csc: (None, [])  # noqa: E731
        self.assertEqual(self.run_tool(["targets"], FakePhone(files=self.BUNDLE), fota=nothing), 0, self.err)
        self.assertIn("Reliable targets: none", self.out)

    def test_targets_is_read_only(self):
        self.run_tool(["targets"], FakePhone(files=self.BUNDLE))
        for call in self.phone.calls:
            self.assertIn(call[0], ("devices", "shell"))
            if call[0] == "shell":
                self.assertRegex(call[1], r"^(getprop|ls |dumpsys )")


class TestReport(Harness):
    def test_report_masks_identifiers(self):
        modem = FakeModem({"AT+PRECONFG=1,0": "ERROR"})
        self.run_tool(["change", "INS"], modem=modem)  # stops at the pre-read: a typical report case
        with mock.patch.object(g, "REPORT_DIR", os.path.join(self.tmp.name, "reports")):
            self.assertEqual(self.run_tool(["report"]), 0, self.err)
        path = re.search(r"Report saved: (\S+)", self.out).group(1)
        text = open(path).read()
        self.assertIn("SM-S928B", text)
        self.assertIn(BUILD, text)
        self.assertIn("STOPPED: couldn't read the CSC", text)
        self.assertNotIn(SERIAL, text)
        self.assertNotIn("350000000000001", text)
        self.assertIn("R5C*******", text)

    def test_report_without_logs(self):
        self.assertEqual(self.run_tool(["report"]), 1)
        self.assertIn("no logs found", self.err)

    def test_stop_messages_are_logged(self):
        self.run_tool(["change", "INS"], modem=FakeModem(), typed="no")
        self.assertIn("STOPPED: aborted", self.logs())


class TestPortDiscovery(unittest.TestCase):
    def port(self, device, vid, serial):
        return mock.Mock(device=device, vid=vid, serial_number=serial)

    def test_picks_only_this_phones_ports(self):
        ports = [self.port("/dev/cu.usbmodemA", 0x04E8, SERIAL),
                 self.port("/dev/cu.usbmodemB", 0x04E8, "OTHER"),
                 self.port("/dev/cu.usbserial1", 0x0403, SERIAL),
                 self.port("/dev/tty.usbmodemA", 0x04E8, SERIAL)]
        with mock.patch("serial.tools.list_ports.comports", return_value=ports):
            self.assertEqual(g.find_modem_ports(SERIAL), [("/dev/cu.usbmodemA", True)])

    def test_other_samsung_only(self):
        with mock.patch("serial.tools.list_ports.comports",
                        return_value=[self.port("/dev/cu.usbmodemB", 0x04E8, "OTHER")]):
            self.assertEqual(g.find_modem_ports(SERIAL), [])

    def test_port_paths_per_os(self):
        cases = {"darwin": (["/dev/cu.SAMSUNG_MDM1", "/dev/cu.usbmodem1"], ["/dev/tty.usbmodem1", "COM5"]),
                 "linux": (["/dev/ttyACM0", "/dev/ttyACM12"], ["/dev/ttyUSB0", "/dev/ttyS0", "COM5"]),
                 "win32": (["COM5", "COM12"], ["COM", "COM5a", "/dev/ttyACM0", "\\\\.\\COM5"])}
        for plat, (good, bad) in cases.items():
            for d in good:
                self.assertTrue(g.is_modem_port_path(d, plat), (plat, d))
            for d in bad:
                self.assertFalse(g.is_modem_port_path(d, plat), (plat, d))
        self.assertFalse(g.is_modem_port_path("/dev/cu.x", "freebsd"))

    def test_windows_and_linux_discovery(self):
        for plat, dev in (("win32", "COM7"), ("linux", "/dev/ttyACM0")):
            ports = [self.port(dev, 0x04E8, SERIAL), self.port("COM3" if plat == "win32" else "/dev/ttyACM1",
                                                               0x0403, SERIAL)]
            with mock.patch.object(g.sys, "platform", plat), \
                    mock.patch("serial.tools.list_ports.comports", return_value=ports):
                self.assertEqual(g.find_modem_ports(SERIAL), [(dev, True)], plat)

    def test_windows_skips_lsof(self):
        with mock.patch.object(g.sys, "platform", "win32"), \
                mock.patch.object(g.subprocess, "run", side_effect=AssertionError("lsof called")):
            self.assertFalse(g.port_busy("COM5"))

    def test_no_serials_reported(self):
        with mock.patch("serial.tools.list_ports.comports",
                        return_value=[self.port("/dev/cu.usbmodemA", 0x04E8, None)]):
            self.assertEqual(g.find_modem_ports(SERIAL), [("/dev/cu.usbmodemA", False)])


if __name__ == "__main__":
    unittest.main()
