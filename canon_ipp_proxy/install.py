"""install / uninstall: a user service on this machine, or a system service for the LAN.

Nothing is written under /usr. One unit template serves both modes (%E and StateDirectory= pick the paths).
A manifest lists what install created; uninstall removes exactly that (state only with --purge).
"""
import http.client
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from importlib import resources
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from . import ipp, repair

PROG = "canon-ipp-proxy"
FIRST_PORT = 8631  # default: next to IPP's 631


class Fail(SystemExit):
    def __init__(self, msg):
        super().__init__(f"{PROG}: {msg}")


@dataclass
class Paths:
    system: bool
    bin: pathlib.Path
    unit_dir: pathlib.Path
    config: pathlib.Path
    state: pathlib.Path
    avahi: pathlib.Path | None

    @classmethod
    def for_mode(cls, system, env=os.environ, home=None):
        if system:
            return cls(True, pathlib.Path("/usr/local/bin"), pathlib.Path("/etc/systemd/system"),
                       pathlib.Path("/etc") / PROG, pathlib.Path("/var/lib") / PROG,
                       pathlib.Path("/etc/avahi/services"))
        home = pathlib.Path(home or env.get("HOME") or pathlib.Path.home())

        def xdg(var, default):
            v = env.get(var)
            return pathlib.Path(v) if v and os.path.isabs(v) else home / default  # XDG: relative values are invalid
        return cls(False, home / ".local" / "bin", xdg("XDG_DATA_HOME", ".local/share") / "systemd" / "user",
                   xdg("XDG_CONFIG_HOME", ".config") / PROG, xdg("XDG_STATE_HOME", ".local/state") / PROG, None)

    @property
    def unit(self):
        return self.unit_dir / f"{PROG}@.service"

    def env_file(self, name):
        return self.config / f"{name}.env"

    def manifest(self, name):
        return self.config / f"{name}.installed.json"

    def avahi_file(self, name):
        return self.avahi / f"{PROG}-{name}.service"

    def instances(self):
        return sorted(p.name[:-len(".installed.json")] for p in self.config.glob("*.installed.json"))


def unit_text(exe, system):
    lines = [
        f"# Installed by `{PROG} install`; removed by `{PROG} uninstall NAME`. Settings: %E/{PROG}/%i.env",
        "[Unit]",
        "Description=Canon IPP fix-up proxy (%i)",
        *(["Wants=network-online.target"] if system else []),
        "After=network-online.target",
        "",
        "[Service]",
        "Environment=ARGS=",
        f"EnvironmentFile=%E/{PROG}/%i.env",
        f"StateDirectory={PROG}/%i",
        f"ExecStart={exe} $PRINTER $ARGS",
        "Restart=on-failure",
        "RestartSec=10",
    ]
    if system:
        lines += ["DynamicUser=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
                  "NoNewPrivileges=yes", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX"]
    lines += ["", "[Install]", "WantedBy=" + ("multi-user.target" if system else "default.target"), ""]
    return "\n".join(lines)


def env_text(printer, listen, extra):
    args = " ".join(["--listen", listen] + extra)
    return f"# {PROG} settings; rerun `{PROG} install` to change them\nPRINTER={printer}\nARGS={args}\n"


def read_env(path):
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.startswith("#"):
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def listen_of(env):
    m = re.search(r"--listen[ =](\S+)", env.get("ARGS", ""))
    return m.group(1) if m else f"127.0.0.1:{FIRST_PORT}"


def port_free(host, port):
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def choose_port(paths, name, host, free=None):
    """The instance's current port when reinstalling, else the first port no other instance claims and nobody uses."""
    free = free or port_free
    taken = {}
    for env in paths.config.glob("*.env"):
        taken[env.stem] = int(listen_of(read_env(env)).rpartition(":")[2])
    if name in taken:
        return taken[name]
    port = FIRST_PORT
    while port in taken.values() or not free(host, port):
        port += 1
    return port


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "printer"


# --- talking to the printer ------------------------------------------------------------------------------------------

def get_attributes(uri, timeout=15):
    """One full Get-Printer-Attributes to URI; the parsed reply. Raises Fail when the printer does not answer."""
    u = urlsplit(uri)
    if u.scheme != "ipp" or not u.hostname:
        raise Fail(f"{uri}: give the printer as ipp://HOST/PATH (ipps is not supported)")
    req = ipp.encode(ipp.Message(b"\x02\x00", ipp.GET_PRINTER_ATTRIBUTES, 1, [[ipp.OPERATION, [
        ["attributes-charset", [ipp.string("utf-8", ipp.CHARSET)]],
        ["attributes-natural-language", [ipp.string("en", ipp.LANGUAGE)]],
        ["printer-uri", [ipp.string(uri, ipp.URI)]],
        ["requested-attributes", [ipp.string("all")]],
    ]]]))
    conn = http.client.HTTPConnection(u.hostname, u.port or 631, timeout=timeout)
    try:
        conn.request("POST", u.path or "/ipp/print", req, {"Content-Type": "application/ipp"})
        resp = conn.getresponse()
        body = resp.read()
    except OSError as e:
        raise Fail(f"{uri} does not answer ({e}). Is it on and awake? Deep sleep ends only with its sleep button.")
    finally:
        conn.close()
    try:
        msg = ipp.parse(body)
    except (ValueError, IndexError, struct.error):
        raise Fail(f"{uri} answered HTTP {resp.status} without an IPP reply; check the path (usually /ipp/print)")
    if msg.code >= 0x0400:
        raise Fail(f"{uri} refused Get-Printer-Attributes (status 0x{msg.code:04x}); check the path")
    return msg


def first(msg, name, default=""):
    v = msg.get(name)
    return ipp.text(v[0]) if v else default


def title(msg, default="printer"):
    """Human name: maker from printer-device-id plus printer-name, e.g. "Canon MF645C".

    printer-make-and-model ("CNMF645C") is not readable enough."""
    model = first(msg, "printer-name") or first(msg, "printer-make-and-model", default)
    ids = dict(p.partition(":")[::2] for p in first(msg, "printer-device-id").split(";") if ":" in p)
    maker = ids.get("MFG") or ids.get("MANUFACTURER") or ""
    return model if not maker or model.lower().startswith(maker.lower()) else f"{maker} {model}"


def avahi_text(name, port, msg):
    """Avahi static service advertising the proxy as a driverless printer (IPP Everywhere / AirPrint TXT keys)."""
    repair.repair(msg)
    fmts = [ipp.text(v) for v in msg.get("document-format-supported") or []]
    keep = [f for f in fmts if f in ("application/pdf", "image/pwg-raster", "image/urf", "image/jpeg")]
    color = any(v[1] == b"\x01" for v in msg.get("color-supported") or [])
    duplex = any(ipp.text(v).startswith("two-sided") for v in msg.get("sides-supported") or [])
    name_shown = title(msg, name)
    txt = {
        "txtvers": "1", "qtotal": "1", "rp": "ipp/print", "ty": name_shown,
        "pdl": ",".join(keep), "URF": ",".join(ipp.text(v) for v in msg.get("urf-supported") or []) or "none",
        "Color": "T" if color else "F", "Duplex": "T" if duplex else "F",
        "kind": ",".join(ipp.text(v) for v in msg.get("printer-kind") or []) or "document",
        "UUID": first(msg, "printer-uuid").removeprefix("urn:uuid:"), "note": first(msg, "printer-location"),
        "priority": "0", "product": f"({name_shown})",
    }
    records = "\n".join(f"    <txt-record>{xml_escape(k)}={xml_escape(v)}</txt-record>" for k, v in txt.items() if v)
    return f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Installed by `{PROG} install`; removed by `{PROG} uninstall {name}` -->
<service-group>
  <name replace-wildcards="yes">{xml_escape(name_shown)} on %h</name>
  <service>
    <type>_ipp._tcp</type>
    <subtype>_universal._sub._ipp._tcp</subtype>
    <port>{port}</port>
{records}
  </service>
</service-group>
"""


# --- the program file ------------------------------------------------------------------------------------------------

def zipapp_bytes():
    """This package as a self-contained executable zip (python3 shebang), built from whatever is running now."""
    buf = io.BytesIO()
    buf.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", "import sys\nfrom canon_ipp_proxy.__main__ import main\nsys.exit(main())\n")
        for f in sorted(resources.files("canon_ipp_proxy").iterdir(), key=lambda f: f.name):
            if f.name.endswith(".py"):
                z.writestr(f"canon_ipp_proxy/{f.name}", f.read_bytes())
    return buf.getvalue()


def installed_program():
    """A persistent `canon-ipp-proxy` on PATH (uv tool, pipx, distro), or None (e.g. running from uvx or a checkout)."""
    exe = shutil.which(PROG)
    if not exe:
        return None
    real = os.path.realpath(exe)
    transient = ("/.cache/", "/tmp/", "/archive-v0/", "/builds-v0/")
    return None if any(t in real for t in transient) else exe


# --- running commands ------------------------------------------------------------------------------------------------

class Runner:
    """Runs commands and writes files, logging each step; tests replace it."""

    def __init__(self, dry=False):
        self.dry = dry

    def say(self, msg):
        print(msg, flush=True)

    def run(self, *cmd, check=True):
        self.say("  $ " + shlex.join(cmd))
        if self.dry:
            return 0
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.stdout.strip():
            self.say("    " + r.stdout.strip().replace("\n", "\n    "))
        if check and r.returncode:
            raise Fail(f"`{shlex.join(cmd)}` failed (exit {r.returncode})")
        return r.returncode

    def write(self, path, data, mode=0o644):
        data = data.encode() if isinstance(data, str) else data
        try:
            if path.read_bytes() == data and (path.stat().st_mode & 0o777) == mode:
                return False
        except FileNotFoundError:
            pass
        self.say(f"  write {path}")
        if not self.dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_bytes(data)
            tmp.chmod(mode)
            os.replace(tmp, path)
        return True

    def remove(self, path):
        if path.exists() or path.is_symlink():
            self.say(f"  remove {path}")
            if not self.dry:
                path.unlink()

    def wait_for(self, host, port, seconds=15):
        if self.dry:
            return True
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                socket.create_connection((host, port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.3)
        return False


def systemctl(paths):
    return ("systemctl",) if paths.system else ("systemctl", "--user")


def have(cmd):
    return shutil.which(cmd) is not None


# --- install ---------------------------------------------------------------------------------------------------------

def install(a, r=None, paths=None):
    r = r or Runner(a.dry_run)
    if a.host:
        return remote(a, r)
    system = a.system
    if system and os.geteuid() != 0 and not a.dry_run:
        as_root()
    if not system and os.geteuid() == 0:
        raise Fail("run the user install as your own user, not root (or add --system)")
    if not have("systemctl"):
        raise Fail("systemd is required (systemctl not found)")
    paths = paths or Paths.for_mode(system)

    r.say(f"Asking {a.printer} for its attributes")
    msg = get_attributes(a.printer)
    name = a.name or slug(first(msg, "printer-name") or urlsplit(a.printer).hostname)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise Fail(f"name {name!r}: use lowercase letters, digits, - and _")
    r.say(f"  {first(msg, 'printer-make-and-model', 'printer')} answers; instance {name!r}")

    manifest = load_manifest(paths, name)
    host = a.listen_host or ("0.0.0.0" if system else "127.0.0.1")
    port = a.port or choose_port(paths, name, host)
    listen = f"{host}:{port}"
    extra = [] if a.snmp_version == "1" else ["--snmp-version", a.snmp_version]
    if a.snmp_community != "public":
        extra += ["--snmp-community", a.snmp_community]

    created = manifest.get("created", {})

    def track(kind, path):
        created.setdefault(kind, [])
        if str(path) not in created[kind]:
            created[kind].append(str(path))

    r.say("Program")
    if system:
        exe = paths.bin / PROG
        r.write(exe, zipapp_bytes(), 0o755)
        track("shared", exe)
    else:
        found = installed_program()
        if found and not zipfile.is_zipfile(found):  # uv tool / pipx / distro package: not ours to manage
            exe = pathlib.Path(found)
            r.say(f"  using {exe}")
        else:  # none, or our own copy from an earlier install: (re)write it from the running version
            exe = paths.bin / PROG
            r.write(exe, zipapp_bytes(), 0o755)
            track("shared", exe)

    r.say("Service")
    r.write(paths.unit, unit_text(exe, system))
    track("shared", paths.unit)
    r.write(paths.env_file(name), env_text(a.printer, listen, extra), 0o644)
    track("files", paths.env_file(name))
    unit = f"{PROG}@{name}.service"
    r.run(*systemctl(paths), "daemon-reload")
    r.run(*systemctl(paths), "enable", unit)
    r.run(*systemctl(paths), "restart", unit)
    created["unit"] = unit
    record = {"name": name, "printer": a.printer, "listen": listen, "mode": "system" if system else "user",
              "created": created}
    save_manifest(r, paths, name, record)  # now, so a failure below still leaves something uninstall can remove
    if not r.wait_for("127.0.0.1" if host in ("0.0.0.0", "::") else host, port):
        r.run("journalctl", *(() if system else ("--user",)), "-u", unit, "-n", "20", "--no-pager", check=False)
        hint = ""
        if system and subprocess.run(["systemctl", "show", "-p", "ExecMainStatus", "--value", unit],
                                     capture_output=True, text=True).stdout.strip() == "226":
            hint = (" This machine does not let systemd sandbox services (DynamicUser=, mount namespaces): in an "
                    "LXC container enable nesting, in podman run it with --privileged.")
        raise Fail(f"{unit} did not start listening on {listen}; journal above.{hint}")
    if not system and not linger_on():
        r.say("  start at boot without logging in")
        r.run("loginctl", "enable-linger", os.environ.get("USER", ""), check=False)

    if system and paths.avahi and paths.avahi.parent.is_dir():
        r.say("mDNS")
        r.write(paths.avahi_file(name), avahi_text(name, port, msg))
        track("files", paths.avahi_file(name))
    elif system:
        r.say("  avahi not installed: the proxy is not advertised; clients add ipp://THIS-MACHINE:%d/ipp/print" % port)
    if system and host not in ("127.0.0.1", "::1") and firewalld_active():
        r.say("Firewall")
        r.run("firewall-cmd", "--permanent", f"--add-port={port}/tcp")
        r.run("firewall-cmd", "--reload")
        created["firewall_port"] = port
        save_manifest(r, paths, name, record)

    shown = title(msg, name)
    queue = None if a.no_queue else a.queue or (None if system else re.sub(r"[^A-Za-z0-9_.-]+", "_", shown))
    old = created.get("queue")
    if queue or old:
        r.say("CUPS queue")
    if old and old != queue:
        r.run("lpadmin", "-x", old, check=False)  # renamed: CUPS cannot rename a queue
        created.pop("queue")
        save_manifest(r, paths, name, record)
    if queue:
        if not have("lpadmin"):
            raise Fail("CUPS (lpadmin) not found; rerun with --no-queue to skip the queue")
        uri = f"ipp://127.0.0.1:{port}/ipp/print"
        if r.run("lpadmin", "-p", queue, "-E", "-v", uri, "-m", "everywhere", "-D", shown,
                 "-L", first(msg, "printer-location"), check=False):
            raise Fail(f"lpadmin could not add queue {queue!r}; adding printers needs a CUPS admin group "
                       "(wheel on Fedora, lpadmin on Debian) or rerun with --no-queue")
        created["queue"] = queue

    save_manifest(r, paths, name, record)
    r.say(f"Installed {name}: ipp://{listen}/ipp/print -> {a.printer}" + (f", CUPS queue {queue!r}" if queue else ""))
    r.say(f"Status: curl -s http://127.0.0.1:{port}/status   Remove: {PROG} uninstall {name}"
          + (" --system" if system else ""))
    return 0


def as_root():
    """Run this same command again with sudo (sudo's PATH would not find a per-user install)."""
    if not have("sudo"):
        raise Fail("--system needs root")
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # a directory, or the .pyz itself
    print(f"{PROG}: --system needs root; asking sudo", file=sys.stderr, flush=True)
    os.execvp("sudo", ["sudo", "env", f"PYTHONPATH={pkg_parent}", sys.executable, "-c",
                       "import sys; from canon_ipp_proxy.__main__ import main; sys.exit(main())", *sys.argv[1:]])


def linger_on():
    try:
        out = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                             capture_output=True, text=True).stdout.strip()
        return out == "yes"
    except OSError:
        return False


def firewalld_active():
    return have("firewall-cmd") and subprocess.run(["systemctl", "is-active", "--quiet", "firewalld"]).returncode == 0


def load_manifest(paths, name):
    try:
        return json.loads(paths.manifest(name).read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise Fail(f"{paths.manifest(name)} is damaged ({e}); fix or delete it")


def save_manifest(r, paths, name, data):
    r.write(paths.manifest(name), json.dumps(data, indent=1) + "\n")


# --- uninstall -------------------------------------------------------------------------------------------------------

def uninstall(a, r=None, paths=None):
    r = r or Runner(a.dry_run)
    if a.host:
        return remote(a, r)
    if a.system and os.geteuid() != 0 and not a.dry_run:
        as_root()
    paths = paths or Paths.for_mode(a.system)
    m = load_manifest(paths, a.name)
    if not m:
        known = ", ".join(paths.instances()) or "none"
        raise Fail(f"no {'system' if a.system else 'user'} instance {a.name!r} (installed: {known})")
    c = m.get("created", {})
    if c.get("queue"):
        r.say("CUPS queue")
        r.run("lpadmin", "-x", c["queue"], check=False)
    unit = c.get("unit")
    if unit:
        r.say("Service")
        r.run(*systemctl(paths), "disable", "--now", unit, check=False)
        if a.purge:
            r.run(*systemctl(paths), "clean", "--what=state", unit, check=False)
    if c.get("firewall_port"):
        r.run("firewall-cmd", "--permanent", f"--remove-port={c['firewall_port']}/tcp", check=False)
        r.run("firewall-cmd", "--reload", check=False)
    for f in c.get("files", []):
        r.remove(pathlib.Path(f))
    others = [n for n in paths.instances() if n != a.name]
    r.remove(paths.manifest(a.name))
    if not others:
        for f in c.get("shared", []):
            r.remove(pathlib.Path(f))
        private = paths.state.parent / "private" / PROG if paths.system else None  # DynamicUser= keeps state here
        for d in (paths.config, paths.state if a.purge else None, private if a.purge else None):
            if d and d.is_dir() and not any(d.iterdir()):
                r.say(f"  remove {d}/")
                if not a.dry_run:
                    d.rmdir()
    if unit:
        r.run(*systemctl(paths), "daemon-reload", check=False)
    r.say(f"Removed {a.name}" + ("" if a.purge else f" (state kept in {paths.state / a.name}; --purge removes it)"))
    return 0


# --- another machine -------------------------------------------------------------------------------------------------

def remote(a, r):
    """Copy this program to USER@MACHINE over ssh and run the same command there with sudo."""
    if not have("ssh"):
        raise Fail("ssh not found")
    argv = [x for x in sys.argv[1:]]
    i = argv.index("--host")
    del argv[i:i + 2]
    if "--system" not in argv:
        argv.append("--system")
    tmp = f"/tmp/{PROG}-{os.getpid()}.pyz"
    r.say(f"Copying {PROG} to {a.host}")
    if not r.dry:
        subprocess.run(["ssh", a.host, f"umask 077 && cat > {tmp}"], input=zipapp_bytes(), check=True)
    try:
        return r.run("ssh", "-t", a.host, f"sudo python3 {tmp} " + shlex.join(argv), check=False)
    finally:
        if not r.dry:
            subprocess.run(["ssh", a.host, f"rm -f {tmp}"])
