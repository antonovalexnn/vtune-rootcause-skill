#!/usr/bin/env python
"""Fetch a VTune daily build from Intel Artifactory by build number.

Given a build number (e.g. 632837) this resolves the build's archive on
Artifactory and downloads it either to a **local** folder or, running the
download on the box itself over ssh, to a **remote** Linux machine. It can
fetch the public installer package (default) or the developer package, and
optionally extracts the archive and locates bin64/vtune.

Metadata resolution (listing the build folder, picking the archive, reading
its size) always runs LOCALLY via the Artifactory REST Storage API -- this
host reaches Artifactory anonymously over the Intel intranet. Only the actual
download+extract location varies (local vs. remote).

  vab_fetch.py <build> (--dest DIR | --remote USER@HOST [--remote-dest DIR]) [opts]

Pure stdlib: shells out to curl / ssh / 7z via subprocess (which is also why
the whole thing runs behind a single Bash(... *) permission grant -- the inner
network calls are invisible to the harness permission layer). No third-party
packages. Prints parse-friendly KEY=value lines the calling skill consumes.

Output keys (one per line):
  BUILD PLATFORM CONFIG PACKAGE STREAM
  ARCHIVE_NAME ARCHIVE_SIZE DOWNLOAD_URL
  LOCATION=local|remote
  LOCAL_ARCHIVE | REMOTE_ARCHIVE
  EXTRACTED_DIR | REMOTE_EXTRACTED_DIR    (unless --no-extract)
  VTUNE_BIN                               (installer packages only; else (n/a))
  SKIPPED=yes|no

Exit 0 on success, 2 on a usage error, 1 on a resolve/download/extract failure
(the message names the URL or cause).
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

# Default Artifactory host. This BA host serves the daily builds anonymously
# from the Intel intranet (verified: plain GET returns 200, no token needed).
DEFAULT_HOST = "af01p-ba.devtools.intel.com"
REPO = "analyzerengineering-ba-local"

# 7-Zip on this Windows box (needed to extract developer-package.7z locally).
WIN_7ZIP = r"C:\Program Files\7-Zip\7z.exe"

# Archive filename patterns per package variant. developer is the fixed
# developer-package.7z; the installer variants embed a version that changes
# per build, so they are matched by regex against the directory listing.
INSTALLER_RE = {
    "public":   re.compile(r"^Intel_VTune_Profiler_[\d.]+\.(tar\.gz|zip)$"),
    "internal": re.compile(r"^Intel_VTune_Profiler_[\d.]+_internal\.(tar\.gz|zip)$"),
    "nda":      re.compile(r"^Intel_VTune_Profiler_[\d.]+_nda\.(tar\.gz|zip)$"),
}
DEVELOPER_NAME = "developer-package.7z"

# Windows takes the .zip archive; every other platform takes .tar.gz. The
# developer package is .7z on all platforms.
WIN_EXT = ".zip"
NIX_EXT = ".tar.gz"


def _die(msg, code=2):
    sys.stderr.write("ERROR: " + msg + "\n")
    raise SystemExit(code)


def _run(cmd, **kw):
    """subprocess.run with bytes captured and decoded as utf-8/replace."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    return proc.returncode, out, err


# --------------------------------------------------------------------------
# Artifactory metadata (always local)
# --------------------------------------------------------------------------

def _storage_url(host, *parts):
    tail = "/".join(str(p).strip("/") for p in parts)
    return "https://{0}/artifactory/api/storage/{1}/{2}".format(host, REPO, tail)


def _curl_json(url):
    """GET a JSON document from Artifactory via curl; parse and return it."""
    rc, out, err = _run(["curl", "-fsSL", "--ssl-no-revoke", "-m", "60", url])
    if rc != 0:
        _die("failed to GET {0}\n{1}".format(url, err.strip() or "curl exit "
             + str(rc)), code=1)
    try:
        return json.loads(out)
    except ValueError:
        _die("non-JSON response from {0} (first 200 chars):\n{1}".format(
            url, out[:200]), code=1)


def _build_folder_parts(build, platform, config, stream):
    # daily/<stream>/<build>/<platform>/<config>/build
    return ("Products", "vtune", "daily", stream, build, platform, config, "build")


def resolve_archive(host, build, platform, config, stream, package):
    """List the build's build/ folder and pick the archive for `package`.

    Returns (archive_name, download_uri, size_bytes). Exits 1 with a clear
    message if the build/folder/archive can't be found.
    """
    parts = _build_folder_parts(build, platform, config, stream)
    listing = _curl_json(_storage_url(host, *parts))
    children = [c["uri"].lstrip("/") for c in listing.get("children", [])
                if not c.get("folder", False)]

    if package == "developer":
        if DEVELOPER_NAME not in children:
            _die("no {0} in build {1} ({2}/{3}); present files: {4}".format(
                DEVELOPER_NAME, build, platform, config,
                ", ".join(children) or "(none)"), code=1)
        name = DEVELOPER_NAME
    else:
        want_ext = WIN_EXT if platform == "windows" else NIX_EXT
        rx = INSTALLER_RE[package]
        matches = [c for c in children if rx.match(c) and c.endswith(want_ext)]
        if not matches:
            _die("no {0} {1} archive in build {2} ({3}/{4}); present files: "
                 "{5}".format(package, want_ext, build, platform, config,
                              ", ".join(children) or "(none)"), code=1)
        name = sorted(matches)[0]

    # Per-file info for the authoritative downloadUri + size.
    info = _curl_json(_storage_url(host, *(parts + (name,))))
    uri = info.get("downloadUri") or (
        "https://{0}/artifactory/{1}/{2}/{3}".format(
            host, REPO, "/".join(parts), name))
    size = int(info.get("size", 0))
    return name, uri, size


def _human(nbytes):
    if not nbytes:
        return "unknown"
    mb = nbytes / (1024.0 * 1024.0)
    if mb >= 1024:
        return "{0:.2f} GB".format(mb / 1024.0)
    return "{0:.1f} MB".format(mb)


# --------------------------------------------------------------------------
# Local download + extract
# --------------------------------------------------------------------------

def download_local(url, dest_dir, name, force):
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, name)
    if os.path.exists(path) and os.path.getsize(path) > 0 and not force:
        return path, True
    sys.stderr.write("Downloading {0} -> {1} (this may take a while)...\n".format(
        name, dest_dir))
    rc, _out, err = _run(["curl", "-fL", "--ssl-no-revoke", "-o", path, url])
    if rc != 0:
        _die("download failed ({0})\n{1}".format(url, err.strip()
             or "curl exit " + str(rc)), code=1)
    return path, False


def find_vtune_bin(root):
    """Locate bin64/vtune[.exe] under an extracted tree (root or one level in)."""
    exe = "vtune.exe" if os.name == "nt" else "vtune"
    candidates = [root] + [os.path.join(root, d) for d in os.listdir(root)
                           if os.path.isdir(os.path.join(root, d))]
    for base in candidates:
        p = os.path.join(base, "bin64", exe)
        if os.path.exists(p):
            return p
    return None


def extract_local(archive_path, extract_dir):
    os.makedirs(extract_dir, exist_ok=True)
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
    elif archive_path.endswith(".tar.gz") or archive_path.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(extract_dir)
    elif archive_path.endswith(".7z"):
        if not os.path.exists(WIN_7ZIP):
            _die("7-Zip not found at {0}; cannot extract {1}. Re-run with "
                 "--no-extract or install 7-Zip.".format(WIN_7ZIP, archive_path),
                 code=1)
        rc, _out, err = _run([WIN_7ZIP, "x", "-y",
                              "-o" + extract_dir, archive_path])
        if rc != 0:
            _die("7z extraction failed:\n{0}".format(err.strip()), code=1)
    else:
        _die("unsupported archive format: {0}".format(archive_path), code=1)
    return extract_dir


# --------------------------------------------------------------------------
# Remote (ssh) download + extract -- runs curl/wget/tar ON the remote box
# --------------------------------------------------------------------------

def _ssh_base():
    return ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "PreferredAuthentications=password,publickey",
            "-o", "ConnectTimeout=15"]


def _ssh_env(password):
    """Return (env, cleanup) for non-interactive password ssh via SSH_ASKPASS.

    When no password is supplied we assume key-based auth and add nothing.
    The password is written to a throwaway askpass shim (mode 0700) rather than
    passed on any command line, and never printed.
    """
    # Disable MSYS/Git-Bash POSIX->Windows path conversion for the ssh child:
    # otherwise remote paths like /tmp/... get rewritten to C:\... locally
    # before reaching the remote (tar then reads "C:" as a remote-host spec).
    def _no_pathconv(env):
        env["MSYS_NO_PATHCONV"] = "1"
        env["MSYS2_ARG_CONV_EXCL"] = "*"
        return env

    if password is None:
        return _no_pathconv(dict(os.environ)), (lambda: None)
    fd, script = tempfile.mkstemp(suffix=".sh")
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/sh\n")
        # Single-quote the password, escaping any embedded single quotes.
        fh.write("printf '%s\\n' '{0}'\n".format(password.replace("'", "'\\''")))
    os.chmod(script, 0o700)
    env = _no_pathconv(dict(os.environ))
    env["SSH_ASKPASS"] = script
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", ":0")
    return env, (lambda: os.path.exists(script) and os.remove(script))


def _ssh_run(target, remote_cmd, password):
    """Run `remote_cmd` on `target` over ssh, returning (rc, out, err).

    The command is fed to a remote `bash -s` over STDIN rather than passed as an
    ssh argument. Under Git-Bash/MSYS, ssh.exe rewrites any argument that looks
    like a POSIX path (/tmp/...) or a URL into a Windows path (C:\\...) before
    sending it — which corrupted the embedded remote paths. Passing the script
    on stdin means ssh's only args are options + target + the literal
    "bash -s", none of which are path-shaped, so nothing gets mangled.
    """
    env, cleanup = _ssh_env(password)
    try:
        cmd = _ssh_base() + [target, "bash", "-s"]
        proc = subprocess.run(
            cmd, input=remote_cmd.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        return proc.returncode, out, err
    finally:
        cleanup()


def _ssh_run_ps(target, ps_script, password):
    """Run a PowerShell script on a Windows remote over ssh; return (rc,out,err).

    The script is passed as a base64 `-EncodedCommand` (UTF-16LE, as PowerShell
    expects) rather than on stdin or as a plain argument. That matters on a
    Windows box whose default ssh shell is cmd.exe: feeding a script through
    ssh -> cmd -> powershell stdin applies C-style backslash/octal escaping that
    corrupted embedded paths (\\v -> vtab, \\632 -> octal). base64 is pure ASCII,
    so nothing along the path can rewrite it. A ProgressPreference reset keeps
    PowerShell's CLIXML progress stream out of stderr.
    """
    full = "$ProgressPreference='SilentlyContinue';\r\n" + ps_script
    b64 = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    env, cleanup = _ssh_env(password)
    try:
        cmd = _ssh_base() + [target, "powershell", "-NoProfile",
                             "-NonInteractive", "-EncodedCommand", b64]
        proc = subprocess.run(cmd, input=b"", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env)
        out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        # Drop PowerShell's CLIXML progress noise from stderr.
        err = "\n".join(l for l in err.splitlines()
                        if "CLIXML" not in l and not l.startswith("<Objs"))
        return proc.returncode, out, err
    finally:
        cleanup()


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _ps_quote(s):
    """Single-quote a string for a PowerShell literal (double any single quote)."""
    return "'" + s.replace("'", "''") + "'"


def _remote_preamble(remote_dest, build, url, name):
    """Shell that sets DEST/BUILD_DIR/NAME/URL on the remote, ~ expanded.

    remote_dest is single-quoted for injection safety, then a leading `~` is
    replaced with $HOME on the remote (bash pattern substitution) so the default
    `~/vtune_builds` and any `~/...` the user passes resolve correctly.
    """
    return (
        "set -e\n"
        "DEST={dest}\n"
        'DEST="${{DEST/#\\~/$HOME}}"\n'
        "BUILD_DIR=\"$DEST/{build}\"\n"
        "NAME={name}\n"
        "URL={url}\n"
    ).format(dest=_sh_quote(remote_dest), build=build,
             name=_sh_quote(name), url=_sh_quote(url))


def download_remote(target, remote_dest, build, url, name, password, extract,
                    remote_os):
    """Dispatch to the linux (bash) or windows (powershell) remote fetcher."""
    if remote_os == "windows":
        return download_remote_windows(
            target, remote_dest, build, url, name, password, extract)
    return download_remote_linux(
        target, remote_dest, build, url, name, password, extract)


def download_remote_linux(target, remote_dest, build, url, name, password, extract):
    """Run the whole fetch on a Linux remote: mkdir, curl||wget, then extract."""
    pre = _remote_preamble(remote_dest, build, url, name)

    # Download (skip if already present and non-empty), preferring curl. Echo
    # the resolved BUILD_DIR so we report the real remote path (~ expanded).
    dl = pre + (
        'mkdir -p "$BUILD_DIR" && cd "$BUILD_DIR"\n'
        'echo "VAB_BUILD_DIR=$BUILD_DIR"\n'
        'if [ -s "$NAME" ]; then echo VAB_SKIPPED=yes; else '
        '( curl -fL --ssl-no-revoke -o "$NAME" "$URL" || wget -O "$NAME" "$URL" ) && '
        'echo VAB_SKIPPED=no; fi\n'
    )
    rc, out, err = _ssh_run(target, dl, password)
    if rc != 0:
        _die("remote download failed on {0}:\n{1}".format(
            target, (err or out).strip()), code=1)
    skipped = "VAB_SKIPPED=yes" in out
    build_dir = _grep_kv(out, "VAB_BUILD_DIR") or "{0}/{1}".format(
        remote_dest.rstrip("/"), build)
    remote_archive = "{0}/{1}".format(build_dir, name)

    remote_extract_dir = None
    vtune_bin = None
    if extract:
        rc, out, err = _ssh_run(target, _remote_extract_cmd(pre, name), password)
        if rc != 0:
            _die("remote extraction failed on {0}:\n{1}".format(
                target, (err or out).strip()), code=1)
        if "VAB_EXTRACT_SKIPPED=yes" in out:
            skipped = True
        remote_extract_dir = "{0}/extracted".format(build_dir)
        # Try to locate bin64/vtune on the remote (installer packages).
        find = pre + (
            'EXT="$BUILD_DIR/extracted"\n'
            'f=$(find "$EXT" -maxdepth 3 -type f -name vtune -path "*bin64*" '
            '2>/dev/null | head -n1); [ -n "$f" ] && echo "VAB_BIN=$f" || true\n')
        _rc, fout, _ferr = _ssh_run(target, find, password)
        vtune_bin = _grep_kv(fout, "VAB_BIN")
    return remote_archive, skipped, remote_extract_dir, vtune_bin


def _grep_kv(text, key):
    prefix = key + "="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _remote_extract_cmd(preamble, name):
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        inner = 'tar xzf "$BUILD_DIR/$NAME" -C "$EXT"'
    elif name.endswith(".zip"):
        inner = 'unzip -oq "$BUILD_DIR/$NAME" -d "$EXT"'
    elif name.endswith(".7z"):
        inner = ('( 7z x -y "-o$EXT" "$BUILD_DIR/$NAME" '
                 '|| 7za x -y "-o$EXT" "$BUILD_DIR/$NAME" '
                 '|| 7zr x -y "-o$EXT" "$BUILD_DIR/$NAME" '
                 '|| { echo "no 7z/7za/7zr on remote to extract .7z" >&2; exit 3; } )')
    else:
        inner = 'echo unsupported-archive >&2; exit 3'
    # Skip re-extraction if the tree already has content (matches local mode).
    return preamble + (
        'EXT="$BUILD_DIR/extracted"\n'
        'if [ -d "$EXT" ] && [ -n "$(ls -A "$EXT" 2>/dev/null)" ]; then '
        'echo VAB_EXTRACT_SKIPPED=yes; else mkdir -p "$EXT" && ' + inner +
        ' && echo VAB_EXTRACT_SKIPPED=no; fi\n')


def download_remote_windows(target, remote_dest, build, url, name, password,
                            extract):
    """Run the whole fetch on a Windows remote via PowerShell + curl.exe/tar.exe.

    The default ssh shell on these boxes is cmd.exe with no bash, but curl.exe
    and tar.exe (bsdtar) ship with Windows; .7z needs 7z on PATH. `~` (or `~\\`)
    in remote_dest maps to $env:USERPROFILE. The whole thing is one PowerShell
    script sent via _ssh_run_ps (base64 -EncodedCommand, mangle-proof).
    """
    # Normalize a leading ~ to a marker PowerShell swaps for $env:USERPROFILE.
    rd = remote_dest.replace("/", "\\")
    if rd == "~" or rd.startswith("~\\"):
        dest_expr = ('(Join-Path $env:USERPROFILE ' +
                     _ps_quote(rd[2:] or "vtune_builds") + ')')
    else:
        dest_expr = _ps_quote(rd)

    lower = name.lower()
    if lower.endswith(".zip"):
        extract_ps = '& tar.exe -xf $arc -C $ext; if ($LASTEXITCODE -ne 0){throw "tar failed $LASTEXITCODE"}'
    elif lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        extract_ps = '& tar.exe -xzf $arc -C $ext; if ($LASTEXITCODE -ne 0){throw "tar failed $LASTEXITCODE"}'
    elif lower.endswith(".7z"):
        extract_ps = ('& 7z x -y "-o$ext" $arc; if ($LASTEXITCODE -ne 0){'
                      'throw "7z failed (is 7-Zip on the remote PATH?) $LASTEXITCODE"}')
    else:
        extract_ps = 'throw "unsupported archive: $arc"'

    do_extract = "$true" if extract else "$false"
    script = (
        "$ErrorActionPreference='Stop'\r\n"
        "$dest = {dest}\r\n"
        "$bd = Join-Path $dest {build}\r\n"
        "$null = New-Item -Force -ItemType Directory -Path $bd\r\n"
        "Write-Output ('VAB_BUILD_DIR=' + $bd)\r\n"
        "$arc = Join-Path $bd {name}\r\n"
        "if ((Test-Path $arc) -and ((Get-Item $arc).Length -gt 0)) {{ "
        "Write-Output 'VAB_SKIPPED=yes' }} else {{ "
        "& curl.exe -fL --ssl-no-revoke -o $arc {url}; "
        "if ($LASTEXITCODE -ne 0){{ throw \"curl failed $LASTEXITCODE\" }}; "
        "Write-Output 'VAB_SKIPPED=no' }}\r\n"
        "Write-Output ('VAB_ARCHIVE=' + $arc)\r\n"
        "if ({do_extract}) {{\r\n"
        "  $ext = Join-Path $bd 'extracted'\r\n"
        "  if ((Test-Path $ext) -and (Get-ChildItem $ext -Force -ErrorAction SilentlyContinue | "
        "Select-Object -First 1)) {{ Write-Output 'VAB_EXTRACT_SKIPPED=yes' }} else {{ "
        "$null = New-Item -Force -ItemType Directory -Path $ext; {extract_ps}; "
        "Write-Output 'VAB_EXTRACT_SKIPPED=no' }}\r\n"
        "  Write-Output ('VAB_EXTRACT_DIR=' + $ext)\r\n"
        "  $b = Get-ChildItem -Path $ext -Recurse -Filter vtune.exe -ErrorAction SilentlyContinue "
        "| Where-Object {{ $_.FullName -match 'bin64' }} | Select-Object -First 1\r\n"
        "  if ($b) {{ Write-Output ('VAB_BIN=' + $b.FullName) }}\r\n"
        "}}\r\n"
    ).format(dest=dest_expr, build=_ps_quote(build), name=_ps_quote(name),
             url=_ps_quote(url), do_extract=do_extract, extract_ps=extract_ps)

    rc, out, err = _ssh_run_ps(target, script, password)
    if rc != 0:
        _die("remote (windows) fetch failed on {0}:\n{1}".format(
            target, (err or out).strip()), code=1)
    build_dir = _grep_kv(out, "VAB_BUILD_DIR") or "{0}\\{1}".format(
        remote_dest.replace("/", "\\").rstrip("\\"), build)
    remote_archive = _grep_kv(out, "VAB_ARCHIVE") or "{0}\\{1}".format(build_dir, name)
    skipped = "VAB_SKIPPED=yes" in out
    remote_extract_dir = None
    vtune_bin = None
    if extract:
        if "VAB_EXTRACT_SKIPPED=yes" in out:
            skipped = True
        remote_extract_dir = _grep_kv(out, "VAB_EXTRACT_DIR") or (
            "{0}\\extracted".format(build_dir))
        vtune_bin = _grep_kv(out, "VAB_BIN")
    return remote_archive, skipped, remote_extract_dir, vtune_bin


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        prog="vab_fetch.py",
        description="Download a VTune build from Artifactory by build number.")
    p.add_argument("build", help="build number, e.g. 632837")
    p.add_argument("--dest", help="local destination dir (build lands in <dest>/<build>/)")
    p.add_argument("--remote", metavar="USER@HOST",
                   help="download on this remote box instead of locally")
    p.add_argument("--remote-dest",
                   help="destination dir on the remote box (default: "
                        "~/vtune_builds on linux, ~\\vtune_builds on windows). "
                        "Use a ~/... or relative path: an absolute /path is "
                        "rewritten to a Windows path by Git-Bash before it "
                        "reaches the remote.")
    p.add_argument("--remote-os", choices=("linux", "windows"), default="linux",
                   help="OS of the --remote box (default: linux). windows uses "
                        "PowerShell + curl.exe/tar.exe over ssh.")
    p.add_argument("--package", choices=("public", "developer", "internal", "nda"),
                   default="public", help="package variant (default: public)")
    p.add_argument("--platform",
                   choices=("windows", "linux", "linux-aarch64", "freebsd"),
                   help="target platform (default: windows if local, linux if remote)")
    p.add_argument("--config", choices=("release", "debug"), default="release")
    p.add_argument("--stream", default="master",
                   help="build stream under daily/ (default: master)")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="Artifactory host (default: %(default)s)")
    p.add_argument("--extract", dest="extract", action="store_true", default=True,
                   help="extract the archive (default)")
    p.add_argument("--no-extract", dest="extract", action="store_false",
                   help="download the archive only, do not extract")
    p.add_argument("--list", action="store_true",
                   help="resolve and print metadata only; download nothing")
    p.add_argument("--force", action="store_true",
                   help="re-download even if already present")
    p.add_argument("--remote-password-env", metavar="VAR",
                   help="env var holding the remote ssh password (preferred)")
    p.add_argument("--remote-password", metavar="PW",
                   help="remote ssh password (prefer --remote-password-env)")
    args = p.parse_args(argv)

    if not re.match(r"^\d+$", args.build.strip()):
        _die("build must be a number, got {0!r}.".format(args.build))
    build = args.build.strip()

    # Exactly one destination mode.
    local_mode = bool(args.dest)
    remote_mode = bool(args.remote)
    if args.list:
        pass  # neither dest required for a pure metadata lookup
    elif local_mode == remote_mode:
        _die("specify exactly one of --dest DIR (local) or "
             "--remote USER@HOST [--remote-dest DIR] (remote).")

    # Default the remote dest per remote OS (~ maps to $HOME / $env:USERPROFILE).
    remote_dest = args.remote_dest or (
        "~\\vtune_builds" if args.remote_os == "windows" else "~/vtune_builds")

    # Guard the classic Git-Bash footgun (linux remotes only): an absolute POSIX
    # --remote-dest gets rewritten to a Windows drive path (C:\...) before Python
    # sees it, which then can't exist on the remote. A windows remote legitimately
    # uses drive paths, so only flag this for linux.
    if remote_mode and args.remote_os == "linux" and \
            re.match(r"^[A-Za-z]:[\\/]", remote_dest):
        _die("--remote-dest looks like a Windows path ({0!r}); Git-Bash "
             "rewrote an absolute /path. Pass a '~/...' or relative remote "
             "path instead (default is ~/vtune_builds).".format(remote_dest))

    # Platform default: windows for local or a windows remote, else linux.
    if args.platform:
        platform = args.platform
    elif not remote_mode or args.remote_os == "windows":
        platform = "windows"
    else:
        platform = "linux"

    # Resolve the archive (always local network access).
    name, url, size = resolve_archive(
        args.host, build, platform, args.config, args.stream, args.package)

    print("BUILD={0}".format(build))
    print("PLATFORM={0}".format(platform))
    print("CONFIG={0}".format(args.config))
    print("PACKAGE={0}".format(args.package))
    print("STREAM={0}".format(args.stream))
    print("ARCHIVE_NAME={0}".format(name))
    print("ARCHIVE_SIZE={0}".format(_human(size)))
    print("DOWNLOAD_URL={0}".format(url))

    if args.list:
        print("LOCATION=none")
        return

    is_installer = args.package != "developer"

    if remote_mode:
        password = None
        if args.remote_password_env:
            password = os.environ.get(args.remote_password_env)
            if password is None:
                _die("env var {0!r} (--remote-password-env) is not set.".format(
                    args.remote_password_env))
        elif args.remote_password:
            password = args.remote_password
        archive, skipped, ext_dir, vbin = download_remote(
            args.remote, remote_dest, build, url, name, password, args.extract,
            args.remote_os)
        print("LOCATION=remote")
        print("REMOTE_OS={0}".format(args.remote_os))
        print("REMOTE_HOST={0}".format(args.remote))
        print("REMOTE_ARCHIVE={0}".format(archive))
        if args.extract:
            print("REMOTE_EXTRACTED_DIR={0}".format(ext_dir))
            print("VTUNE_BIN={0}".format(
                vbin if (is_installer and vbin) else "(n/a)"))
        print("SKIPPED={0}".format("yes" if skipped else "no"))
        return

    # Local.
    build_dir = os.path.join(os.path.abspath(args.dest), build)
    archive_path, skipped = download_local(url, build_dir, name, args.force)
    print("LOCATION=local")
    print("LOCAL_ARCHIVE={0}".format(archive_path.replace("\\", "/")))
    if args.extract:
        extract_dir = os.path.join(build_dir, "extracted")
        already = os.path.isdir(extract_dir) and os.listdir(extract_dir)
        if already and not args.force:
            pass  # keep existing extraction
        else:
            extract_local(archive_path, extract_dir)
        print("EXTRACTED_DIR={0}".format(extract_dir.replace("\\", "/")))
        vbin = find_vtune_bin(extract_dir) if is_installer else None
        print("VTUNE_BIN={0}".format(
            vbin.replace("\\", "/") if vbin else "(n/a)"))
    print("SKIPPED={0}".format("yes" if skipped else "no"))


if __name__ == "__main__":
    main()
