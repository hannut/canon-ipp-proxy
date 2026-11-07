"""install / uninstall: path choices, generated files, the manifest round trip, and printer-uri rewriting."""
import argparse
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from canon_ipp_proxy import install, ipp, server

FIX = pathlib.Path(__file__).parent / "fixtures"
RAW = (FIX / "mf645c-get-printer-attributes.raw").read_bytes()
BODY = RAW[RAW.index(b"\r\n\r\n") + 4:]
URI = "ipp://192.0.2.10/ipp/print"


class FakeRunner(install.Runner):
    def __init__(self):
        super().__init__()
        self.cmds, self.out = [], io.StringIO()

    def say(self, msg):
        self.out.write(msg + "\n")

    def run(self, *cmd, check=True):
        self.cmds.append(cmd)
        return 0

    def wait_for(self, host, port, seconds=15):
        return True


def args(**kw):
    base = dict(printer=URI, name=None, queue=None, no_queue=False, system=False, host=None, port=None,
                listen_host=None, snmp_version="1", snmp_community="public", dry_run=False, purge=False)
    base.update(kw)
    return argparse.Namespace(**base)


class PathChoices(unittest.TestCase):
    def test_user_paths_follow_xdg(self):
        p = install.Paths.for_mode(False, {"HOME": "/h", "XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d",
                                           "XDG_STATE_HOME": "/s"})
        self.assertEqual(p.unit, pathlib.Path("/d/systemd/user/canon-ipp-proxy@.service"))
        self.assertEqual(p.env_file("x"), pathlib.Path("/c/canon-ipp-proxy/x.env"))
        self.assertEqual(p.state, pathlib.Path("/s/canon-ipp-proxy"))
        self.assertEqual(p.bin, pathlib.Path("/h/.local/bin"))

    def test_user_defaults_and_relative_xdg_ignored(self):
        p = install.Paths.for_mode(False, {"HOME": "/h", "XDG_CONFIG_HOME": "rel"})
        self.assertEqual(p.config, pathlib.Path("/h/.config/canon-ipp-proxy"))
        self.assertEqual(p.unit_dir, pathlib.Path("/h/.local/share/systemd/user"))
        self.assertEqual(p.state, pathlib.Path("/h/.local/state/canon-ipp-proxy"))

    def test_system_paths_avoid_usr(self):
        p = install.Paths.for_mode(True)
        for path in (p.bin, p.unit, p.env_file("x"), p.avahi_file("x")):
            self.assertFalse(str(path).startswith("/usr/") and not str(path).startswith("/usr/local/"), path)
        self.assertEqual(p.unit, pathlib.Path("/etc/systemd/system/canon-ipp-proxy@.service"))
        self.assertEqual(p.state, pathlib.Path("/var/lib/canon-ipp-proxy"))

    def test_one_template_for_both(self):
        user, system = install.unit_text("/x", False), install.unit_text("/usr/local/bin/x", True)
        for text in (user, system):
            self.assertIn("EnvironmentFile=%E/canon-ipp-proxy/%i.env", text)
            self.assertIn("StateDirectory=canon-ipp-proxy/%i", text)
        self.assertIn("DynamicUser=yes", system)
        self.assertNotIn("DynamicUser", user)
        self.assertIn("WantedBy=default.target", user)
        self.assertIn("WantedBy=multi-user.target", system)

    def test_port_choice(self):
        with tempfile.TemporaryDirectory() as d:
            p = install.Paths.for_mode(False, {"HOME": d})
            p.config.mkdir(parents=True)
            p.env_file("a").write_text("PRINTER=x\nARGS=--listen 127.0.0.1:8631\n")
            self.assertEqual(install.choose_port(p, "a", "127.0.0.1", lambda h, n: True), 8631)
            self.assertEqual(install.choose_port(p, "b", "127.0.0.1", lambda h, n: True), 8632)
            self.assertEqual(install.choose_port(p, "b", "127.0.0.1", lambda h, n: n > 8633), 8634)


class InstallUninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.paths = install.Paths.for_mode(False, {"HOME": self.tmp.name})
        for target, value in [("get_attributes", lambda uri: ipp.parse(BODY)), ("have", lambda c: True),
                              ("linger_on", lambda: True), ("installed_program", lambda: None),
                              ("firewalld_active", lambda: False), ("port_free", lambda h, n: True)]:
            patcher = mock.patch.object(install, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*") if p.is_file())

    def test_user_install_then_uninstall_leaves_nothing(self):
        r = FakeRunner()
        install.install(args(), r, self.paths)
        self.assertEqual(self.files(), [".config/canon-ipp-proxy/mf645c.env",
                                        ".config/canon-ipp-proxy/mf645c.installed.json",
                                        ".local/bin/canon-ipp-proxy",
                                        ".local/share/systemd/user/canon-ipp-proxy@.service"])
        env = install.read_env(self.paths.env_file("mf645c"))
        self.assertEqual(env, {"PRINTER": URI, "ARGS": "--listen 127.0.0.1:8631"})
        self.assertIn(("systemctl", "--user", "enable", "canon-ipp-proxy@mf645c.service"), r.cmds)
        lp = [c for c in r.cmds if c[0] == "lpadmin"][0]
        self.assertEqual(lp[1:7], ("-p", "Canon_MF645C", "-E", "-v", "ipp://127.0.0.1:8631/ipp/print", "-m"))
        self.assertEqual(lp[lp.index("-D") + 1], "Canon MF645C")
        self.assertTrue(zipfile.is_zipfile(self.paths.bin / "canon-ipp-proxy"))

        r2 = FakeRunner()
        install.install(args(), r2, self.paths)  # rerun: nothing rewritten, same port
        self.assertNotIn("write", r2.out.getvalue())

        install.uninstall(args(name="mf645c"), FakeRunner(), self.paths)
        self.assertEqual(self.files(), [])

    def test_second_instance_keeps_shared_files(self):
        install.install(args(), FakeRunner(), self.paths)
        install.install(args(name="other", printer="ipp://192.0.2.11/ipp/print"), FakeRunner(), self.paths)
        self.assertEqual(install.read_env(self.paths.env_file("other"))["ARGS"], "--listen 127.0.0.1:8632")
        install.uninstall(args(name="mf645c"), FakeRunner(), self.paths)
        self.assertTrue(self.paths.unit.exists())
        install.uninstall(args(name="other"), FakeRunner(), self.paths)
        self.assertEqual(self.files(), [])

    def test_uses_installed_program_and_leaves_it(self):
        exe = self.home / "venv-bin" / "canon-ipp-proxy"
        exe.parent.mkdir()
        exe.write_text("#!/usr/bin/python3\n")
        with mock.patch.object(install, "installed_program", lambda: str(exe)):
            install.install(args(), FakeRunner(), self.paths)
        self.assertIn(f"ExecStart={exe} $PRINTER $ARGS", self.paths.unit.read_text())
        install.uninstall(args(name="mf645c"), FakeRunner(), self.paths)
        self.assertTrue(exe.exists())

    def test_renamed_queue_replaces_old(self):
        install.install(args(queue="old"), FakeRunner(), self.paths)
        r = FakeRunner()
        install.install(args(), r, self.paths)
        self.assertIn(("lpadmin", "-x", "old"), r.cmds)
        self.assertEqual(install.load_manifest(self.paths, "mf645c")["created"]["queue"], "Canon_MF645C")

    def test_purge_cleans_state(self):
        install.install(args(), FakeRunner(), self.paths)
        r = FakeRunner()
        install.uninstall(args(name="mf645c", purge=True), r, self.paths)
        self.assertIn(("systemctl", "--user", "clean", "--what=state", "canon-ipp-proxy@mf645c.service"), r.cmds)

    def test_failed_queue_still_uninstallable(self):
        class NoAdmin(FakeRunner):
            def run(self, *cmd, check=True):
                super().run(*cmd, check=check)
                return 1 if cmd[0] == "lpadmin" else 0
        with self.assertRaises(SystemExit):
            install.install(args(), NoAdmin(), self.paths)
        install.uninstall(args(name="mf645c"), FakeRunner(), self.paths)
        self.assertEqual(self.files(), [])

    def test_unknown_instance(self):
        with self.assertRaises(SystemExit):
            install.uninstall(args(name="nope"), FakeRunner(), self.paths)


class ZipApp(unittest.TestCase):
    def test_zipapp_runs(self):
        with tempfile.TemporaryDirectory() as d:
            exe = pathlib.Path(d) / "canon-ipp-proxy"
            exe.write_bytes(install.zipapp_bytes())
            out = subprocess.run([sys.executable, "-I", str(exe), "--version"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            out = subprocess.run([sys.executable, "-I", str(exe), "install", "--help"], capture_output=True, text=True)
            self.assertIn("--system", out.stdout)


class Advertising(unittest.TestCase):
    def test_avahi_txt(self):
        text = install.avahi_text("mf645c", 8631, ipp.parse(BODY))
        for rec in ("rp=ipp/print", "Color=T", "Duplex=T", "pdl=image/urf,image/pwg-raster,image/jpeg,application/pdf",
                    "UUID=00000000-0000-1000-8000-000000000000", "<port>8631</port>", "_universal._sub._ipp._tcp"):
            self.assertIn(rec, text)

    def test_printer_uri_points_at_proxy(self):
        data, notes = server.fix_reply(BODY, None, authority="192.0.2.5:8631")
        uris = [ipp.text(v) for v in ipp.parse(data).get("printer-uri-supported")]
        self.assertEqual(uris, ["ipp://192.0.2.5:8631/ipp/print", "ipp://192.0.2.5:8631/ipp"])
        self.assertEqual(len(ipp.parse(data).get("uri-security-supported")), 2)


if __name__ == "__main__":
    unittest.main()
