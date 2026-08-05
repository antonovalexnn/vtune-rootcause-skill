#!/usr/bin/env python
"""Intel on-prem Jira CLI helper (stdlib only).

A thin wrapper over the Jira REST API (v2 + Agile) that emits trimmed Markdown
instead of raw JSON, so an AI agent can read/search and mutate tickets
(comment, change status, reassign, move sprint) without flooding its context.
Authentication uses a Personal Access Token read from the environment.

  Auth:     JIRA_TOKEN   (required) -- Bearer PAT, never stored on disk
  Base URL: JIRA_BASE_URL (optional) -- defaults to the Intel on-prem instance

Usage:
  jira.py view        KEY [--json]
  jira.py comments    KEY [--limit N] [--json]
  jira.py comment     KEY (-m TEXT | -f FILE | --stdin) [--dry-run]
  jira.py transition  KEY [--to STATUS|--id N] [--dry-run]     # change status (write!)
  jira.py assign      KEY [--to USER|--search [NAME]] [--dry-run]  # set assignee (write!)
  jira.py sprint      KEY [--to NAME|--id N|--list] [--board N] [--state S] [--dry-run]
  jira.py attachments KEY [--json]
  jira.py download    KEY [--name FILE | --all] [--dir DIR]
  jira.py search      "JQL" [--limit N] [--json]
  jira.py raw         KEY [--fields f1,f2,...]

All read commands accept --json to emit the raw API JSON. Every write command
supports --dry-run (print the payload, change nothing). Exit code is 0 on
success, 2 on usage/auth error, 1 on API error.

Note: pinning a comment is NOT supported here -- on this Jira DC instance the
pin endpoint (rest/internal/*/.../comment/{id}/pin) rejects the Bearer PAT and
requires an interactive browser session, so pin manually in the web UI.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://jira.devtools.intel.com"
TIMEOUT = 30
BROWSE_URL = "{base}/browse/{key}"


# --------------------------------------------------------------------------- #
# Low-level API plumbing
# --------------------------------------------------------------------------- #
def _token():
    tok = os.environ.get("JIRA_TOKEN")
    if not tok:
        sys.exit("ERROR: JIRA_TOKEN environment variable is not set. "
                 "Export your Jira Personal Access Token first.")
    return tok


def _base_url():
    return os.environ.get("JIRA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _api(method, path, body=None, base=None, prefix="/rest/api/2"):
    """Call the Jira REST API. `path` is appended to `<base><prefix>`.

    `prefix` defaults to the v2 platform API; pass "/rest/agile/1.0" for the
    Agile (greenhopper) API used by the sprint command.

    Returns the parsed JSON (or {} for empty 204 bodies). Exits non-zero with a
    readable message on HTTP / network errors.
    """
    base = base or _base_url()
    url = "{0}{1}{2}".format(base, prefix, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": "Bearer " + _token(),
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        sys.exit("ERROR: Jira API {0} {1} -> HTTP {2} {3}\n{4}".format(
            method, url, e.code, e.reason, detail[:2000]))
    except urllib.error.URLError as e:
        sys.exit("ERROR: cannot reach Jira at {0}: {1}".format(url, e.reason))


def _get_raw_url(url, base=None):
    """GET an absolute URL (e.g. an attachment `content` link). Returns bytes."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _token()})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        sys.exit("ERROR: download {0} -> HTTP {1} {2}".format(url, e.code, e.reason))
    except urllib.error.URLError as e:
        sys.exit("ERROR: cannot reach {0}: {1}".format(url, e.reason))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _name(obj, key="name", default="-"):
    """Safely pull a sub-field from a possibly-None nested object."""
    if not obj:
        return default
    return obj.get(key) or default


def _person(obj):
    if not obj:
        return "-"
    return "{0} ({1})".format(obj.get("displayName") or obj.get("name") or "?",
                              obj.get("name") or "?")


def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{0:.0f}{1}".format(n, unit) if unit == "B" else "{0:.1f}{1}".format(n, unit)
        n /= 1024.0


def _clean(text):
    """Normalize Jira's CRLF line endings for terminal display."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _emit_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
VIEW_FIELDS = ("summary,status,issuetype,priority,assignee,reporter,"
               "created,updated,description,labels,components")


def cmd_view(args):
    issue = _api("GET", "/issue/{0}?fields={1}".format(args.key, VIEW_FIELDS))
    if args.json:
        _emit_json(issue)
        return
    f = issue.get("fields", {})
    print("# {0}  ·  {1}  ·  {2}".format(
        issue.get("key", args.key),
        _name(f.get("priority")),
        _name(f.get("status"))))
    print("**{0}**".format(f.get("summary") or "(no summary)"))
    print()
    print("- Type:      {0}".format(_name(f.get("issuetype"))))
    print("- Assignee:  {0}".format(_person(f.get("assignee"))))
    print("- Reporter:  {0}".format(_person(f.get("reporter"))))
    print("- Created:   {0}".format(f.get("created") or "-"))
    print("- Updated:   {0}".format(f.get("updated") or "-"))
    comps = [c.get("name") for c in (f.get("components") or [])]
    if comps:
        print("- Components: {0}".format(", ".join(comps)))
    if f.get("labels"):
        print("- Labels:    {0}".format(", ".join(f["labels"])))
    print("- URL:       {0}".format(BROWSE_URL.format(base=_base_url(), key=issue.get("key", args.key))))
    print()
    print("## Description")
    desc = _clean(f.get("description"))
    print(desc if desc else "_(no description)_")


def cmd_comments(args):
    data = _api("GET", "/issue/{0}/comment?maxResults={1}".format(args.key, args.limit))
    if args.json:
        _emit_json(data)
        return
    comments = data.get("comments") or []
    total = data.get("total", len(comments))
    print("# Comments on {0}  ({1} total)".format(args.key, total))
    if not comments:
        print("\n_(no comments)_")
        return
    for i, c in enumerate(comments, 1):
        print("\n## {0}. {1} · {2}".format(i, _person(c.get("author")), c.get("created", "?")))
        if c.get("updated") and c.get("updated") != c.get("created"):
            print("_(edited {0})_".format(c["updated"]))
        print()
        print(_clean(c.get("body")))


def _resolve_comment_body(args):
    if args.message is not None:
        return args.message
    if args.stdin:
        return sys.stdin.read()
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            return fh.read()
    sys.exit("ERROR: provide comment text via -m TEXT, -f FILE, or --stdin")


def cmd_comment(args):
    body = _resolve_comment_body(args)
    if not body.strip():
        sys.exit("ERROR: refusing to post an empty comment.")
    payload = {"body": body}
    if args.dry_run:
        print("DRY RUN — would POST to /issue/{0}/comment:".format(args.key))
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    result = _api("POST", "/issue/{0}/comment".format(args.key), body=payload)
    cid = result.get("id", "?")
    print("Posted comment {0} on {1}".format(cid, args.key))
    print("URL: {0}?focusedCommentId={1}".format(
        BROWSE_URL.format(base=_base_url(), key=args.key), cid))


def cmd_transition(args):
    """Move an issue to another workflow status (write!)."""
    data = _api("GET", "/issue/{0}/transitions".format(args.key))
    transitions = data.get("transitions") or []
    if args.list or (not args.to and not args.id):
        print("# Available transitions for {0}".format(args.key))
        if not transitions:
            print("\n_(none -- you may lack permission or the issue is in a terminal state)_")
            return
        print("\n| id | transition | -> status |")
        print("|----|------------|-----------|")
        for t in transitions:
            print("| {0} | {1} | {2} |".format(
                t.get("id"), t.get("name"), _name(t.get("to"))))
        if not args.to and not args.id:
            return
    # Resolve the requested transition by explicit id or by target-status name.
    chosen = None
    if args.id:
        chosen = next((t for t in transitions if str(t.get("id")) == str(args.id)), None)
        if not chosen:
            sys.exit("ERROR: no transition with id {0} available on {1}. "
                     "Run 'transition {1} --list'.".format(args.id, args.key))
    else:
        want = args.to.strip().lower()
        matches = [t for t in transitions
                   if want in (_name(t.get("to")) or "").lower()
                   or want in (t.get("name") or "").lower()]
        if not matches:
            avail = ", ".join(_name(t.get("to")) for t in transitions) or "(none)"
            sys.exit("ERROR: no transition to {0!r} on {1}. Available targets: {2}".format(
                args.to, args.key, avail))
        if len(matches) > 1:
            opts = "; ".join("{0} -> {1}".format(t.get("id"), _name(t.get("to"))) for t in matches)
            sys.exit("ERROR: {0!r} is ambiguous ({1}). Re-run with --id.".format(args.to, opts))
        chosen = matches[0]
    payload = {"transition": {"id": str(chosen.get("id"))}}
    target = _name(chosen.get("to"))
    if args.dry_run:
        print("DRY RUN — would POST to /issue/{0}/transitions ({1} -> {2}):".format(
            args.key, chosen.get("name"), target))
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    _api("POST", "/issue/{0}/transitions".format(args.key), body=payload)
    print("Transitioned {0} -> {1} (via '{2}', id {3})".format(
        args.key, target, chosen.get("name"), chosen.get("id")))


def _assignable(key, query):
    """Return the list of assignable users matching `query` for an issue."""
    q = urllib.parse.quote(query or "")
    return _api("GET", "/user/assignable/search?issueKey={0}&username={1}&maxResults=20".format(
        key, q))


def cmd_assign(args):
    """Set (or clear) the assignee of an issue (write!)."""
    if args.search is not False:
        # --search [NAME]: list candidates, mutate nothing.
        users = _assignable(args.key, args.search or args.to or "")
        print("# Assignable users for {0}".format(args.key))
        if not users:
            print("\n_(no matches)_")
            return
        print("\n| name | displayName | email |")
        print("|------|-------------|-------|")
        for u in users:
            print("| {0} | {1} | {2} |".format(
                u.get("name"), u.get("displayName") or "-", u.get("emailAddress") or "-"))
        return
    if not args.to:
        sys.exit("ERROR: provide --to USER (login/username), --to -1 to unassign, "
                 "or --search [NAME] to list candidates.")
    # Jira DC: {"name": "<login>"} sets, {"name": null} / -1 unassigns.
    if args.to in ("-1", "null", ""):
        payload, human = {"name": None}, "(unassigned)"
    else:
        payload, human = {"name": args.to}, args.to
    if args.dry_run:
        print("DRY RUN — would PUT /issue/{0}/assignee:".format(args.key))
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    _api("PUT", "/issue/{0}/assignee".format(args.key), body=payload)
    print("Assigned {0} -> {1}".format(args.key, human))


def _iter_board_sprints(board_id, state=None):
    """Yield every sprint on a board, following pagination."""
    start = 0
    while True:
        path = "/board/{0}/sprint?maxResults=50&startAt={1}".format(board_id, start)
        if state:
            path += "&state=" + urllib.parse.quote(state)
        data = _api("GET", path, prefix="/rest/agile/1.0")
        values = data.get("values") or []
        for s in values:
            yield s
        if data.get("isLast") or not values:
            return
        start += len(values)


def _current_sprint_board(key):
    """Best-effort: read the issue's Sprint custom field to find its board id.

    Returns (board_id, [sprint_names]) or (None, []). The Sprint field id is not
    fixed across instances, so we scan custom fields for a greenhopper Sprint
    serialization and pull rapidViewId out of it.
    """
    issue = _api("GET", "/issue/{0}".format(key))
    fields = issue.get("fields", {}) or {}
    for val in fields.values():
        if isinstance(val, list) and val and isinstance(val[0], str) and "greenhopper" in val[0] and "Sprint" in val[0]:
            board = None
            names = []
            for entry in val:
                m = re.search(r"rapidViewId=(\d+)", entry)
                if m:
                    board = int(m.group(1))
                n = re.search(r"name=([^,]+)", entry)
                if n:
                    names.append(n.group(1))
            return board, names
    return None, []


def cmd_sprint(args):
    """Move an issue onto a sprint, or list a board's sprints (write on move!)."""
    board = args.board
    if not board:
        board, cur = _current_sprint_board(args.key)
        if board and not args.list:
            print("_(current sprint(s): {0}; board {1})_".format(", ".join(cur) or "none", board))
    if args.list or (not args.to and not args.id):
        if not board:
            sys.exit("ERROR: pass --board N (could not infer the board from {0}'s "
                     "Sprint field).".format(args.key))
        print("# Sprints on board {0}{1}".format(board, " (state=" + args.state + ")" if args.state else ""))
        print("\n| id | name | state |")
        print("|----|------|-------|")
        for s in _iter_board_sprints(board, args.state):
            print("| {0} | {1} | {2} |".format(s.get("id"), s.get("name"), s.get("state")))
        if not args.to and not args.id:
            return
    # Resolve the target sprint id.
    sprint_id = args.id
    sprint_name = None
    if not sprint_id:
        if not board:
            sys.exit("ERROR: pass --board N so the sprint name can be resolved (or use --id).")
        want = args.to.strip().lower()
        matches = [s for s in _iter_board_sprints(board, args.state)
                   if want in (s.get("name") or "").lower()]
        if not matches:
            sys.exit("ERROR: no sprint matching {0!r} on board {1}. Run 'sprint {2} --board {1} --list'.".format(
                args.to, board, args.key))
        if len(matches) > 1:
            opts = "; ".join("{0} -> {1} [{2}]".format(s.get("id"), s.get("name"), s.get("state")) for s in matches)
            sys.exit("ERROR: {0!r} is ambiguous ({1}). Re-run with --id.".format(args.to, opts))
        sprint_id, sprint_name = matches[0].get("id"), matches[0].get("name")
    payload = {"issues": [args.key]}
    if args.dry_run:
        print("DRY RUN — would POST /sprint/{0}/issue (add {1}):".format(sprint_id, args.key))
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    _api("POST", "/sprint/{0}/issue".format(sprint_id), body=payload, prefix="/rest/agile/1.0")
    print("Moved {0} -> sprint {1}{2}".format(
        args.key, sprint_id, " ({0})".format(sprint_name) if sprint_name else ""))


def cmd_attachments(args):
    issue = _api("GET", "/issue/{0}?fields=attachment".format(args.key))
    if args.json:
        _emit_json(issue.get("fields", {}).get("attachment") or [])
        return
    atts = issue.get("fields", {}).get("attachment") or []
    print("# Attachments on {0}  ({1})".format(args.key, len(atts)))
    if not atts:
        print("\n_(no attachments)_")
        return
    print()
    print("| # | filename | size | type | id |")
    print("|---|----------|------|------|----|")
    for i, a in enumerate(atts, 1):
        print("| {0} | {1} | {2} | {3} | {4} |".format(
            i, a.get("filename", "?"), _human_size(a.get("size")),
            a.get("mimeType", "?"), a.get("id", "?")))


def cmd_download(args):
    issue = _api("GET", "/issue/{0}?fields=attachment".format(args.key))
    atts = issue.get("fields", {}).get("attachment") or []
    if not atts:
        sys.exit("No attachments on {0}.".format(args.key))
    if not args.all and not args.name:
        sys.exit("ERROR: specify --name FILENAME or --all")
    targets = atts if args.all else [a for a in atts if a.get("filename") == args.name]
    if not targets:
        sys.exit("ERROR: no attachment named {0!r} on {1}. Use 'attachments {1}' to list.".format(
            args.name, args.key))
    out_dir = args.dir or "."
    os.makedirs(out_dir, exist_ok=True)
    for a in targets:
        content = _get_raw_url(a["content"])
        dest = os.path.join(out_dir, a["filename"])
        with open(dest, "wb") as fh:
            fh.write(content)
        print("Saved {0} ({1})".format(dest, _human_size(len(content))))


def cmd_search(args):
    payload = {
        "jql": args.jql,
        "maxResults": args.limit,
        "fields": ["summary", "status", "assignee"],
    }
    data = _api("POST", "/search", body=payload)
    if args.json:
        _emit_json(data)
        return
    issues = data.get("issues") or []
    print("# Search: {0}".format(args.jql))
    print("_{0} of {1} match(es)_\n".format(len(issues), data.get("total", "?")))
    if not issues:
        print("_(no results)_")
        return
    print("| key | status | assignee | summary |")
    print("|-----|--------|----------|---------|")
    for it in issues:
        f = it.get("fields", {})
        print("| {0} | {1} | {2} | {3} |".format(
            it.get("key", "?"), _name(f.get("status")),
            _name(f.get("assignee"), "displayName"),
            (f.get("summary") or "").replace("|", "\\|")))


def cmd_raw(args):
    path = "/issue/{0}".format(args.key)
    if args.fields:
        path += "?fields=" + args.fields
    _emit_json(_api("GET", path))


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="jira.py", description="Intel on-prem Jira CLI (stdlib only).")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit raw API JSON")

    sp = sub.add_parser("view", help="show issue header + description")
    sp.add_argument("key")
    add_json(sp)
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("comments", help="list comments")
    sp.add_argument("key")
    sp.add_argument("--limit", type=int, default=20)
    add_json(sp)
    sp.set_defaults(func=cmd_comments)

    sp = sub.add_parser("comment", help="post a comment (write!)")
    sp.add_argument("key")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("-m", "--message", help="comment text")
    g.add_argument("-f", "--file", help="read comment text from file")
    g.add_argument("--stdin", action="store_true", help="read comment text from stdin")
    sp.add_argument("--dry-run", action="store_true", help="print payload, post nothing")
    sp.set_defaults(func=cmd_comment)

    sp = sub.add_parser("transition", help="change workflow status (write!)")
    sp.add_argument("key")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--to", help="target status or transition name (substring, case-insensitive)")
    g.add_argument("--id", help="explicit transition id (from --list)")
    sp.add_argument("--list", action="store_true", help="list available transitions and exit")
    sp.add_argument("--dry-run", action="store_true", help="print payload, change nothing")
    sp.set_defaults(func=cmd_transition)

    sp = sub.add_parser("assign", help="set the assignee (write!)")
    sp.add_argument("key")
    sp.add_argument("--to", help="assignee login/username, or -1 to unassign")
    # nargs='?' + default False: bare --search lists all; --search NAME filters.
    sp.add_argument("--search", nargs="?", default=False, const="",
                    help="list assignable users (optionally filtered by NAME); mutates nothing")
    sp.add_argument("--dry-run", action="store_true", help="print payload, change nothing")
    sp.set_defaults(func=cmd_assign)

    sp = sub.add_parser("sprint", help="move issue onto a sprint (write!)")
    sp.add_argument("key")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--to", help="target sprint name (substring, case-insensitive)")
    g.add_argument("--id", help="explicit sprint id")
    sp.add_argument("--board", type=int, help="board id (inferred from the issue's Sprint field if omitted)")
    sp.add_argument("--state", help="filter listed sprints by state: active,future,closed")
    sp.add_argument("--list", action="store_true", help="list the board's sprints and exit")
    sp.add_argument("--dry-run", action="store_true", help="print payload, change nothing")
    sp.set_defaults(func=cmd_sprint)

    sp = sub.add_parser("attachments", help="list attachments")
    sp.add_argument("key")
    add_json(sp)
    sp.set_defaults(func=cmd_attachments)

    sp = sub.add_parser("download", help="download attachment(s)")
    sp.add_argument("key")
    sp.add_argument("--name", help="filename to download")
    sp.add_argument("--all", action="store_true", help="download all attachments")
    sp.add_argument("--dir", help="output directory (default: cwd)")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("search", help="JQL search")
    sp.add_argument("jql")
    sp.add_argument("--limit", type=int, default=30)
    add_json(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("raw", help="dump raw issue JSON (escape hatch)")
    sp.add_argument("key")
    sp.add_argument("--fields", help="comma-separated field list")
    sp.set_defaults(func=cmd_raw)

    return p


def main(argv=None):
    # Jira returns UTF-8 (middots, non-breaking spaces, accented names); force
    # UTF-8 stdout so a cp1252 Windows console doesn't mangle them into U+FFFD.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
