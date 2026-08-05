#!/usr/bin/env python
"""Intel VTune/Analyzers test-history CLI helper (stdlib only).

Given a test name, print that test's run history -- date, OS, platform,
pass/fail status, the build number it ran on, and the matched Jira ticket (if
the failing run was tied to one) -- so an AI agent can see how long a test has
been failing / flaky / when it last passed without flooding its context.

The data does NOT come from TRAQs directly (TRAQs is a write-only submission
system with no read API). It comes from the downstream Triage Service SSRS
report `TestReportRemastered`, queried through the SQL Server Reporting Services
URL-access endpoint and rendered as XML.

  Auth:     Kerberos / Negotiate SSO via the system `curl` -- NO token needed.
            Uses your current Windows login; you must be on the corp network.
  Base URL: TEST_HISTORY_BASE_URL (optional) -- defaults to the pbirs01 host.

Usage:
  test_history.py history TEST [--days N | --start MM/DD/YYYY --end MM/DD/YYYY]
                               [--branch B] [--type T] [--suite S]
                               [--summary] [--json] [--url]

TEST is the test-case name, e.g. pshe_cli_dot_net_managed2native_ijw_lh_vtss.
`history` accepts --json to emit the raw parsed records. This tool is
read-only. Exit code is 0 on success, 2 on usage/auth error, 1 on fetch error.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET

DEFAULT_BASE_URL = "https://pbirs01.intel.com"
REPORT_PATH = "/Triage Service/TestReportRemastered"
DEFAULT_BRANCH = "PiersolHE_master"     # VTune master (PiersolHE = VTune)
DEFAULT_TYPE = "Integration Testing"
DEFAULT_DAYS = 30
TIMEOUT = 300                            # the report can take minutes for wide ranges

JIRA_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]+-\d+)\b")
IP_RE = re.compile(r"\(([\d.]+)\)")


# --------------------------------------------------------------------------- #
# Low-level plumbing
# --------------------------------------------------------------------------- #
def _die(msg, code=2):
    sys.stderr.write("ERROR: " + msg + "\n")
    raise SystemExit(code)


def _base_url():
    return os.environ.get("TEST_HISTORY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _curl_bin():
    return os.environ.get("CURL_BIN", "curl")


def _build_url(test, start, end, branch, ttype, suite):
    """Build the SSRS URL-access URL that renders the report as XML.

    The report path is percent-encoded as the first query component; the rest
    are standard query params (rs:Command/rs:Format plus the *Str filters). The
    report supplies its own defaults for the numeric parameters, so only the
    string filters need to be passed.
    """
    params = [
        ("rs:Command", "Render"),
        ("rs:Format", "XML"),
        ("StartDate", start),
        ("EndDate", end),
        ("ProductBranchStr", branch),
        ("TestingTypeStr", ttype),
        ("TestCaseNameStr", test),
    ]
    if suite:
        params.append(("TestSuiteStr", suite))
    return "{0}/ReportServer?{1}&{2}".format(
        _base_url(),
        urllib.parse.quote(REPORT_PATH, safe=""),
        urllib.parse.urlencode(params),
    )


def _fetch(url):
    """GET the report over Kerberos SSO via curl. Returns the XML bytes."""
    cmd = [_curl_bin(), "-s", "--noproxy", ".intel.com",
           "--negotiate", "-u", ":", "-m", str(TIMEOUT), url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        _die("`{0}` not found. Install curl or set CURL_BIN to its path."
             .format(_curl_bin()), 1)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        _die("curl failed (rc={0}) fetching the report. {1}\nURL: {2}"
             .format(proc.returncode, detail or "(no stderr)", url), 1)
    body = proc.stdout
    if not body.strip():
        _die("empty response from the report server. Are you on the corp "
             "network and logged in (Kerberos)?\nURL: {0}".format(url), 1)
    # An auth failure comes back as an HTML 401 page, not report XML.
    head = body[:400].lstrip().lower()
    if head.startswith(b"<!doctype html") or b"<html" in head:
        _die("got an HTML page instead of report XML -- likely an "
             "authentication/authorization failure (401). Check your corp "
             "login / Kerberos ticket.\nURL: {0}".format(url), 1)
    return body


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _classify(status):
    """Map a raw Status string to (verdict, jira_key, jira_note)."""
    if not status:
        return "FAIL", None, ""
    s = status.strip()
    low = s.lower()
    if low.startswith("passed"):        # "Passed", "Passed with Warnings"
        return "PASS", None, ""
    if low == "not applicable":
        return "NA", None, ""
    m = JIRA_KEY_RE.match(s)
    if m:
        # "KEY : state : summary : [] : [...]"  -- keep the human-readable tail,
        # trimming the trailing python-list noise the report appends.
        parts = [p.strip() for p in s.split(" : ")]
        note = " : ".join(parts[1:3]) if len(parts) > 1 else ""
        return "FAIL", m.group(1), note
    return "FAIL", None, s               # a plain failure reason, no Jira match


def _fmt_date(raw):
    """RunDate is 'DD-MM-YYYY HH:MM:SS.mmm' -> 'YYYY-MM-DD HH:MM:SS' (raw on fail)."""
    try:
        dt = datetime.datetime.strptime(raw.strip(), "%d-%m-%Y %H:%M:%S.%f")
        return dt.strftime("%Y-%m-%d %H:%M:%S"), dt
    except ValueError:
        return raw.strip(), None


def _parse(xml_bytes):
    """Parse report XML into a list of run-record dicts, newest first."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        _die("could not parse report XML: {0}".format(e), 1)
    # Strip the default namespace so .iter('R')/.iter('C') match.
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.rsplit("}", 1)[-1]

    records = []
    for R in root.iter("R"):
        lines = (R.get("R") or "").replace("\r", "").split("\n")
        lines = [ln.strip() for ln in lines]
        l0 = lines[0] if len(lines) > 0 else ""
        l1 = lines[1] if len(lines) > 1 else ""
        l2 = lines[2] if len(lines) > 2 else ""
        tags = lines[3] if len(lines) > 3 else ""

        date_raw, _, build = l0.partition(" / ")
        date_disp, dt = _fmt_date(date_raw)
        os_type, _, os_name = l1.partition(" / ")
        platform, _, hostpart = l2.partition(" / ")
        ip_m = IP_RE.search(hostpart)
        ip = ip_m.group(1) if ip_m else ""
        host = IP_RE.sub("", hostpart).strip()

        for C in R.iter("C"):
            cval = C.get("C") or ""
            suite, _, test = cval.partition(" / ")
            verdict, jira_key, jira_note = _classify(C.get("Status"))
            records.append({
                "date": date_disp,
                "_dt": dt,
                "build": build.strip(),
                "os_type": os_type.strip(),
                "os_name": os_name.strip(),
                "platform": platform.strip(),
                "host": host,
                "ip": ip,
                "tags": tags,
                "suite": suite.strip(),
                "test": test.strip(),
                "status": (C.get("Status") or "").strip(),
                "verdict": verdict,
                "jira": jira_key or "",
                "jira_note": jira_note,
            })

    # The report already returns newest-first; enforce it for records we could
    # timestamp, keeping un-parseable ones in their original relative order.
    records.sort(key=lambda r: (r["_dt"] is not None, r["_dt"]), reverse=True)
    for r in records:
        r.pop("_dt", None)
    return records


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def _os_disp(r):
    t, n = r["os_type"], r["os_name"]
    if not n:
        return t
    if n.lower().startswith(t.lower()):
        return n
    return (t + " " + n).strip()


def _table(rows, headers):
    """Render a list of string-tuples as a fixed-width text table."""
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    fmt = "  ".join("{{:<{0}}}".format(w) for w in widths)
    out = [fmt.format(*headers).rstrip()]
    out.append(fmt.format(*["-" * w for w in widths]).rstrip())
    for row in rows:
        out.append(fmt.format(*row).rstrip())
    return "\n".join(out)


def _group_stats(runs):
    """For runs (newest-first) in one bucket: last-pass and current-fail-start."""
    last_pass = next((r for r in runs if r["verdict"] == "PASS"), None)
    lead = []
    for r in runs:
        if r["verdict"] == "PASS":
            break
        lead.append(r)
    fails_in_lead = [r for r in lead if r["verdict"] == "FAIL"]
    first_fail = fails_in_lead[-1] if fails_in_lead else None   # oldest in the streak
    return last_pass, first_fail


def _print_summary(test, start, end, records):
    npass = sum(1 for r in records if r["verdict"] == "PASS")
    nfail = sum(1 for r in records if r["verdict"] == "FAIL")
    nna = sum(1 for r in records if r["verdict"] == "NA")
    print("# test-history: {0}".format(test))
    print("range: {0} .. {1}   ({2} runs: {3} passed, {4} failed, {5} n/a)"
          .format(start, end, len(records), npass, nfail, nna))
    jkeys = sorted({r["jira"] for r in records if r["jira"]})
    if jkeys:
        print("matched Jira: {0}".format(", ".join(jkeys)))
    print()

    # Bucket by (platform, OS).
    buckets = {}
    for r in records:
        buckets.setdefault((r["platform"], _os_disp(r)), []).append(r)

    rows = []
    for (plat, osd), runs in sorted(buckets.items()):
        p = sum(1 for r in runs if r["verdict"] == "PASS")
        f = sum(1 for r in runs if r["verdict"] == "FAIL")
        na = sum(1 for r in runs if r["verdict"] == "NA")
        last_pass, first_fail = _group_stats(runs)
        lp = last_pass["build"] if last_pass else "-"
        ff = first_fail["build"] if first_fail else "-"
        jk = ",".join(sorted({r["jira"] for r in runs if r["jira"]})) or "-"
        rows.append([plat or "-", osd or "-", str(len(runs)),
                     "{0}/{1}/{2}".format(p, f, na), lp, ff, jk])
    print("## By platform  (pass/fail/na · last-pass build · failing-since build)")
    print(_table(rows, ["platform", "os", "runs", "p/f/na",
                         "last-pass", "fail-since", "jira"]))


def _print_runs(records):
    print()
    print("## Runs (newest first)")
    rows = []
    for r in records:
        rows.append([r["date"], r["build"] or "-", _os_disp(r) or "-",
                     r["platform"] or "-", r["verdict"],
                     r["jira"] or (r["jira_note"][:38] if r["jira_note"] else "-")])
    print(_table(rows, ["date", "build", "os", "platform", "status", "jira/reason"]))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _resolve_range(args):
    if args.start or args.end:
        if not (args.start and args.end):
            _die("pass BOTH --start and --end (MM/DD/YYYY), or use --days.")
        return args.start, args.end
    today = datetime.date.today()
    start = today - datetime.timedelta(days=args.days)
    end = today + datetime.timedelta(days=1)          # inclusive of today's runs
    return start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")


def cmd_history(args):
    start, end = _resolve_range(args)
    url = _build_url(args.test, start, end, args.branch, args.type, args.suite)
    if args.url:
        print(url)
        return
    records = _parse(_fetch(url))

    if args.json:
        import json
        print(json.dumps(records, indent=2))
        return

    if not records:
        print("# test-history: {0}".format(args.test))
        print("range: {0} .. {1}   (no runs found)".format(start, end))
        print("\nNo matching runs. Check the test name spelling, widen --days, "
              "or verify --branch/--type/--suite.")
        return

    _print_summary(args.test, start, end, records)
    if not args.summary:
        _print_runs(records)


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="test_history.py",
        description="Intel VTune/Analyzers test-history CLI (stdlib only).")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    sp = sub.add_parser("history", help="show a test's run history")
    sp.add_argument("test", help="test-case name (e.g. pshe_cli_...)")
    sp.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="look back this many days (default {0})".format(DEFAULT_DAYS))
    sp.add_argument("--start", help="explicit start date MM/DD/YYYY (with --end)")
    sp.add_argument("--end", help="explicit end date MM/DD/YYYY (with --start)")
    sp.add_argument("--branch", default=DEFAULT_BRANCH,
                    help="ProductBranchStr (default {0})".format(DEFAULT_BRANCH))
    sp.add_argument("--type", default=DEFAULT_TYPE,
                    help="TestingTypeStr (default '{0}')".format(DEFAULT_TYPE))
    sp.add_argument("--suite", help="TestSuiteStr filter (narrows + speeds the query)")
    sp.add_argument("--summary", action="store_true",
                    help="print only the per-platform summary, not every run")
    sp.add_argument("--json", action="store_true", help="emit the raw parsed records")
    sp.add_argument("--url", action="store_true",
                    help="print the report URL and exit (do not fetch)")
    sp.set_defaults(func=cmd_history)

    return p


def main(argv=None):
    # The report carries accented host names and non-ASCII text; force UTF-8 so a
    # cp1252 Windows console does not mangle it into U+FFFD.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
