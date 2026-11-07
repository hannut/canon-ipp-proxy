"""canon-ipp-proxy PRINTER_URI [options]          run the proxy (what the systemd unit runs)
canon-ipp-proxy install PRINTER_URI [options]  set it up as a service, with a CUPS queue
canon-ipp-proxy uninstall NAME [--purge]       remove exactly what install set up
"""
import argparse
import logging
import sys
from urllib.parse import urlsplit

from . import __version__


def serve(argv):
    from .enrich import Snmp
    from .server import Proxy
    from .state import State, default_dir

    p = argparse.ArgumentParser(prog="canon-ipp-proxy", description=(
        "Forward IPP to a Canon printer and repair its Get-Printer-Attributes replies so CUPS accepts it as a "
        "driverless (IPP Everywhere) printer."), epilog="Set it up as a service: canon-ipp-proxy install --help")
    p.add_argument("printer", help="printer URI, e.g. ipp://printer.local/ipp/print")
    p.add_argument("--listen", default="127.0.0.1:8631", help="address:port to listen on (default %(default)s)")
    snmp_options(p)
    p.add_argument("--snmp-ttl", type=int, default=60, help="seconds before an SNMP reading is renewed")
    p.add_argument("--state-dir", help="where the printer's last known attributes are kept (default: systemd "
                   "StateDirectory, else $XDG_STATE_HOME/canon-ipp-proxy/HOST)")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host, _, port = a.listen.rpartition(":")
    printer_host = urlsplit(a.printer).hostname
    state = State(a.state_dir or default_dir(printer_host))
    snmp = None if a.snmp_version == "off" else Snmp(printer_host, a.snmp_community, a.snmp_version, a.snmp_ttl,
                                                     state)
    server = Proxy((host or "127.0.0.1", int(port)), a.printer, snmp, state)
    logging.info("listening on ipp://%s/ipp/print -> %s, state in %s%s", a.listen, a.printer, state.path,
                 " (attributes cached)" if state.ipp else "")
    server.serve_forever()


def snmp_options(p):
    p.add_argument("--snmp-version", default="1", choices=["1", "2c", "off"],
                   help="SNMP for details IPP omits (default %(default)s; many Canons answer only v1)")
    p.add_argument("--snmp-community", default="public")


def manage(argv):
    from . import install

    p = argparse.ArgumentParser(prog="canon-ipp-proxy")
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("install", help="run the proxy as a systemd service and add a CUPS queue through it",
                       description="Run the proxy as a systemd service. Default: a user service on this machine "
                       "with a CUPS queue (no root). --system: a system service that serves the LAN and is "
                       "advertised over mDNS (root; sudo is asked for). Rerunning repairs or changes an instance.")
    i.add_argument("printer", help="printer URI, e.g. ipp://192.0.2.10/ipp/print")
    i.add_argument("--name", help="instance and queue name (default: the printer's name, e.g. mf645c)")
    i.add_argument("--queue", help="CUPS queue name (default: the instance name; none with --system)")
    i.add_argument("--no-queue", action="store_true", help="do not add a CUPS queue")
    i.add_argument("--system", action="store_true", help="system service for the LAN instead of a user service")
    i.add_argument("--host", metavar="USER@MACHINE", help="install as a system service on another machine over ssh")
    i.add_argument("--port", type=int, help="port to listen on (default: the first free one from 8631)")
    i.add_argument("--listen-host", help="address to listen on (default 127.0.0.1; 0.0.0.0 with --system)")
    snmp_options(i)
    u = sub.add_parser("uninstall", help="remove an instance: queue, service, settings (state with --purge)")
    u.add_argument("name")
    u.add_argument("--system", action="store_true")
    u.add_argument("--host", metavar="USER@MACHINE")
    u.add_argument("--purge", action="store_true", help="also delete the kept printer attributes and SNMP reading")
    for s in (i, u):
        s.add_argument("--dry-run", action="store_true", help="show what would be done")
    a = p.parse_args(argv)
    return (install.install if a.command == "install" else install.uninstall)(a)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("install", "uninstall"):
        return manage(argv)
    serve(argv)


if __name__ == "__main__":
    sys.exit(main())
