#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The test WordPress at its own address, from inside a container.

    wp-relay.py --listen 127.0.0.1:<port> --to <wp-container>:80 [--pidfile F]
    wp-relay.py --stop-matching <text>     stop every relay whose --to has <text>

test-env.sh publishes WordPress on 127.0.0.1:<port> of the Docker HOST and
writes http://localhost:<port> into its home and siteurl — the address the
owner's browser opens. An agent that itself runs inside a container (the Mac
app's project container, with the Docker socket mounted) has its own
loopback: localhost:<port> there reaches nothing, so install-theme.py,
verify-wp.py, smoke-editor.py and quick-check.py could not open the site,
and a WordPress answering on any other address redirects to its siteurl
anyway. test-env.sh (container mode, H2WP_CONTAINER set) attaches the
container to the run's network and starts this relay, which forwards
127.0.0.1:<port> inside the container to the WordPress container on that
network. The same URL then works on both sides of the container wall.

Carried over from the desktop app's runner (runtime/runner.py relay()).

It listens on loopback only — anything else is refused: a relay on 0.0.0.0
would publish a WordPress installed with known credentials to whatever
network the container sits on. It runs in the foreground in its own session
(test-env.sh starts it in the background with nohup), so the shell that
started it can exit without taking it along. SIGTERM stops it and removes
the pidfile.

Exit 0 = stopped cleanly; 1 = could not listen; 2 = usage or refused address.
"""
import argparse
import ipaddress
import os
import select
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

IDLE_SECONDS = 300


def address(text):
    """(host, port) of HOST:PORT; an IPv6 host in brackets."""
    host, sep, port = str(text).rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536 or not host:
        raise ValueError(f"not HOST:PORT: {text!r}")
    return host.strip("[]"), int(port)


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            remote = socket.create_connection(self.server.target, timeout=15)
        except OSError:
            return
        with remote:
            remote.settimeout(None)
            ends = [self.request, remote]
            while True:
                ready, _, _ = select.select(ends, [], [], IDLE_SECONDS)
                if not ready:
                    return
                for source in ready:
                    try:
                        data = source.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    (remote if source is self.request else self.request).sendall(data)


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen, target):
        self.target = target
        super().__init__(listen, Forward)


def relay_processes():
    """(pid, argv) of every wp-relay.py process this machine can see: /proc
    where there is one (Linux, every container), `ps` elsewhere."""
    found = []
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            argv = [a.decode("utf-8", "replace") for a in argv if a]
            if any(a.endswith("wp-relay.py") for a in argv):
                found.append((int(entry.name), argv))
        return found
    import subprocess
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and "wp-relay.py" in args:
            found.append((int(pid), args.split()))
    return found


def stop_matching(text):
    """Stop the relays whose --to names `text` (a run's containers, when the
    run is torn down without the state file that recorded their pid)."""
    stopped = 0
    for pid, argv in relay_processes():
        if pid == os.getpid():
            continue
        to = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--to"), "")
        if text and text in to:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped += 1
            except OSError:
                pass
    return stopped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", help="127.0.0.1:<port> (loopback only)")
    ap.add_argument("--to", help="<host>:<port> to forward to")
    ap.add_argument("--pidfile", default="")
    ap.add_argument("--stop-matching", default="", metavar="TEXT")
    args = ap.parse_args(argv)
    if args.stop_matching:
        print(f"stopped {stop_matching(args.stop_matching)} relay(s)")
        return 0
    if not args.listen or not args.to:
        ap.error("--listen and --to are required")
    try:
        listen, target = address(args.listen), address(args.to)
    except ValueError as err:
        print(f"wp-relay.py: {err}", file=sys.stderr)
        return 2
    if not is_loopback(listen[0]):
        print(f"wp-relay.py: refusing to listen on {listen[0]} — loopback only", file=sys.stderr)
        return 2
    try:
        server = Relay(listen, target)
    except OSError as err:
        print(f"wp-relay.py: cannot listen on {args.listen}: {err}", file=sys.stderr)
        return 1
    try:
        os.setsid()
    except OSError:
        pass
    if args.pidfile:
        Path(args.pidfile).write_text(f"{os.getpid()}\n")

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"wp-relay: {args.listen} -> {args.to}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if args.pidfile:
            try:
                Path(args.pidfile).unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
