#!/usr/bin/env python
"""Intel on-prem Confluence CLI helper (stdlib only).

A thin wrapper over the Confluence REST API that emits trimmed Markdown instead
of raw JSON, so an AI agent can read a wiki page, see and download its attached
documents, and enumerate the links inside it without flooding its context.
Authentication uses a Personal Access Token read from the environment.

  Auth:     CONFLUENCE_TOKEN    (required) -- Bearer PAT, never stored on disk
  Base URL: CONFLUENCE_BASE_URL (optional) -- defaults to the Intel on-prem wiki

Usage:
  confluence.py view        PAGE [--json]
  confluence.py attachments PAGE [--json]
  confluence.py download    PAGE [--name FILE | --all] [--dir DIR]
  confluence.py links       PAGE [--json]
  confluence.py search      "CQL" [--limit N] [--json]
  confluence.py raw         PAGE [--expand E]

PAGE is anything that identifies a page: a full wiki URL (viewpage.action,
/display/SPACE/Title, /spaces/SPACE/pages/NNN/..., or a /x/<tiny> short link),
a bare numeric pageId, or the shorthand "SPACE/Title".

All read commands accept --json to emit the raw API JSON. This skill is
read-only -- it never edits or creates Confluence content. Exit code is 0 on
success, 2 on usage/auth error, 1 on API error.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEFAULT_BASE_URL = "https://wiki.ith.intel.com"
TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Low-level API plumbing
# --------------------------------------------------------------------------- #
def _token():
    tok = os.environ.get("CONFLUENCE_TOKEN")
    if not tok:
        sys.exit("ERROR: CONFLUENCE_TOKEN environment variable is not set. "
                 "Export your Confluence Personal Access Token first.")
    return tok


def _base_url():
    return os.environ.get("CONFLUENCE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _api(method, path, body=None, base=None, prefix="/rest/api"):
    """Call the Confluence REST API. `path` is appended to `<base><prefix>`.

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
        sys.exit("ERROR: Confluence API {0} {1} -> HTTP {2} {3}\n{4}".format(
            method, url, e.code, e.reason, detail[:2000]))
    except urllib.error.URLError as e:
        sys.exit("ERROR: cannot reach Confluence at {0}: {1}".format(url, e.reason))


def _get_raw_url(url):
    """GET an absolute or base-relative URL (e.g. an attachment `download`
    link). Returns (bytes, final_url). Follows redirects (short /x/ links)."""
    if url.startswith("/"):
        url = _base_url() + url
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _token()})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(), resp.geturl()
    except urllib.error.HTTPError as e:
        sys.exit("ERROR: fetch {0} -> HTTP {1} {2}".format(url, e.code, e.reason))
    except urllib.error.URLError as e:
        sys.exit("ERROR: cannot reach {0}: {1}".format(url, e.reason))


# --------------------------------------------------------------------------- #
# Page identifier resolution
# --------------------------------------------------------------------------- #
def _content_by_space_title(space, title):
    """Resolve a (spaceKey, title) pair to a content id. Exits if not found."""
    q = "/content?spaceKey={0}&title={1}&expand=version&limit=1".format(
        urllib.parse.quote(space), urllib.parse.quote(title))
    data = _api("GET", q)
    results = data.get("results") or []
    if not results:
        sys.exit("ERROR: no page titled {0!r} in space {1!r}. "
                 "Check the title (it must match exactly) and space key.".format(title, space))
    return results[0].get("id")


def _resolve_page(arg):
    """Return a numeric Confluence content id from many identifier forms.

    Accepts: a bare numeric id; the shorthand 'SPACE/Title'; or a full wiki URL
    in any of the shapes Confluence hands out --
      .../pages/viewpage.action?pageId=N
      .../pages/viewpage.action?spaceKey=S&title=T
      .../display/S/Title
      .../spaces/S/pages/N/Title
      .../x/<tiny>            (short link -- fetched and followed to the real URL)
    """
    arg = (arg or "").strip()
    if not arg:
        sys.exit("ERROR: no page identifier given.")

    # Bare numeric id.
    if arg.isdigit():
        return arg

    if "://" not in arg and not arg.startswith("/"):
        # Not a URL. Treat as "SPACE/Title" shorthand (space keys have no slash).
        if "/" in arg:
            space, title = arg.split("/", 1)
            if space and title:
                return _content_by_space_title(space, title.replace("+", " "))
        sys.exit("ERROR: cannot interpret {0!r} as a page. Use a pageId, a wiki "
                 "URL, or 'SPACE/Title'.".format(arg))

    return _resolve_url(arg)


def _resolve_url(url):
    """Resolve a full wiki URL to a content id."""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or ""
    qs = urllib.parse.parse_qs(parsed.query or "")

    # viewpage.action?pageId=N
    if "pageId" in qs and qs["pageId"]:
        return qs["pageId"][0]

    # viewpage.action?spaceKey=S&title=T  (title may be '+'-encoded)
    if "spaceKey" in qs and "title" in qs:
        title = urllib.parse.unquote_plus(qs["title"][0])
        return _content_by_space_title(qs["spaceKey"][0], title)

    # /spaces/S/pages/NNN/Title  or  /pages/NNN/...
    m = re.search(r"/pages/(\d+)", path)
    if m:
        return m.group(1)

    # /display/SPACE/Page+Title
    m = re.search(r"/display/([^/]+)/(.+)$", path)
    if m:
        space = urllib.parse.unquote(m.group(1))
        title = urllib.parse.unquote_plus(m.group(2)).rstrip("/")
        return _content_by_space_title(space, title)

    # /x/<tiny>  short link -- follow the redirect and re-resolve the target.
    if re.search(r"/x/[^/]+$", path):
        _, final_url = _get_raw_url(url)
        if final_url and final_url != url:
            return _resolve_url(final_url)
        sys.exit("ERROR: short link {0} did not redirect to a resolvable page.".format(url))

    sys.exit("ERROR: could not extract a page id from URL {0}. Pass the pageId "
             "or a 'SPACE/Title' instead.".format(url))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{0:.0f}{1}".format(n, unit) if unit == "B" else "{0:.1f}{1}".format(n, unit)
        n /= 1024.0


def _emit_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _page_url(page):
    """Human browse URL for a content object (from _links, best effort)."""
    links = page.get("_links") or {}
    base = links.get("base") or _base_url()
    webui = links.get("webui") or ""
    if webui:
        return base + webui
    return "{0}/pages/viewpage.action?pageId={1}".format(_base_url(), page.get("id", "?"))


class _TextRenderer(HTMLParser):
    """Render Confluence 'view' HTML into compact, readable Markdown-ish text.

    Not a full HTML->MD converter -- just enough structure (headings, lists,
    paragraphs, links, tables-as-lines) to read a page in a terminal without the
    tag noise. Unknown tags are dropped; their text content is kept.
    """
    _BLOCK = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "li", "ul", "ol", "table", "thead", "tbody", "section", "hr"}
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("p", "div", "tr", "ul", "ol", "table", "section"):
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n---\n")
        elif tag == "td" or tag == "th":
            self.parts.append(" | ")
        elif tag == "a":
            for k, v in attrs:
                if k == "href":
                    self._href = v
                    break

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._href:
            self.parts.append("({0})".format(self._href))
            self._href = None
        elif tag in ("p", "div", "tr", "table", "section",
                     "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._href is not None:
            self.parts.append("[{0}]".format(data.strip()))
            return
        self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        # Collapse runs of blank lines and trailing spaces.
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(html_value):
    if not html_value:
        return ""
    r = _TextRenderer()
    try:
        r.feed(html_value)
        r.close()
    except Exception:
        # On any parser hiccup, fall back to a crude tag strip.
        return html.unescape(re.sub(r"<[^>]+>", " ", html_value)).strip()
    return r.text()


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_view(args):
    pid = _resolve_page(args.page)
    page = _api("GET", "/content/{0}?expand=space,version,ancestors,body.view,"
                       "children.attachment".format(pid))
    if args.json:
        _emit_json(page)
        return
    space = (page.get("space") or {}).get("key") or "-"
    ver = (page.get("version") or {}).get("number") or "-"
    ancestors = [a.get("title") for a in (page.get("ancestors") or []) if a.get("title")]
    print("# {0}".format(page.get("title") or "(untitled)"))
    print()
    print("- Space:     {0}".format(space))
    print("- Type:      {0}".format(page.get("type") or "-"))
    print("- Version:   {0}".format(ver))
    if ancestors:
        print("- Breadcrumb: {0}".format(" > ".join(ancestors)))
    print("- Page id:   {0}".format(page.get("id") or pid))
    print("- URL:       {0}".format(_page_url(page)))
    att = (((page.get("children") or {}).get("attachment") or {}).get("size"))
    if att is not None:
        print("- Attachments: {0}  (list: `attachments {1}`)".format(att, pid))
    print()
    print("## Content")
    body = (((page.get("body") or {}).get("view") or {}).get("value")) or ""
    text = _html_to_text(body)
    print(text if text else "_(empty page)_")


def cmd_attachments(args):
    pid = _resolve_page(args.page)
    data = _api("GET", "/content/{0}/child/attachment?limit=200".format(pid))
    results = data.get("results") or []
    if args.json:
        _emit_json(results)
        return
    print("# Attachments on page {0}  ({1})".format(pid, len(results)))
    if not results:
        print("\n_(no attachments)_")
        return
    print()
    print("| # | filename | size | type | id |")
    print("|---|----------|------|------|----|")
    for i, a in enumerate(results, 1):
        ext = a.get("extensions") or {}
        print("| {0} | {1} | {2} | {3} | {4} |".format(
            i, a.get("title", "?"), _human_size(ext.get("fileSize")),
            ext.get("mediaType", "?"), a.get("id", "?")))


def cmd_download(args):
    pid = _resolve_page(args.page)
    data = _api("GET", "/content/{0}/child/attachment?limit=200".format(pid))
    results = data.get("results") or []
    if not results:
        sys.exit("No attachments on page {0}.".format(pid))
    if not args.all and not args.name:
        sys.exit("ERROR: specify --name FILENAME or --all")
    targets = results if args.all else [a for a in results if a.get("title") == args.name]
    if not targets:
        sys.exit("ERROR: no attachment named {0!r} on page {1}. Use "
                 "'attachments {1}' to list.".format(args.name, pid))
    out_dir = args.dir or "."
    os.makedirs(out_dir, exist_ok=True)
    for a in targets:
        link = (a.get("_links") or {}).get("download")
        if not link:
            print("SKIP {0}: no download link".format(a.get("title", "?")))
            continue
        content, _ = _get_raw_url(link)
        dest = os.path.join(out_dir, a.get("title", "attachment"))
        with open(dest, "wb") as fh:
            fh.write(content)
        print("Saved {0} ({1})".format(dest, _human_size(len(content))))


def _extract_links(storage):
    """Pull links out of Confluence 'storage' XHTML.

    Returns three lists: external URLs, internal page refs (space, title), and
    attachment refs (filename). Storage macros encode internal links as
    <ri:page ri:content-title=.. ri:space-key=..> and attachments as
    <ri:attachment ri:filename=..>, while plain <a href> carries external URLs.
    """
    external = []
    for m in re.finditer(r'href="([^"]+)"', storage or ""):
        href = html.unescape(m.group(1))
        if href.startswith("http://") or href.startswith("https://"):
            external.append(href)

    pages = []
    for m in re.finditer(r"<ri:page\b([^>]*)/?>", storage or ""):
        attrs = m.group(1)
        t = re.search(r'ri:content-title="([^"]*)"', attrs)
        s = re.search(r'ri:space-key="([^"]*)"', attrs)
        if t:
            pages.append((html.unescape(s.group(1)) if s else None,
                          html.unescape(t.group(1))))

    attachments = []
    for m in re.finditer(r'ri:filename="([^"]*)"', storage or ""):
        attachments.append(html.unescape(m.group(1)))

    # De-dup while preserving order.
    def _uniq(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return _uniq(external), _uniq(pages), _uniq(attachments)


def cmd_links(args):
    pid = _resolve_page(args.page)
    page = _api("GET", "/content/{0}?expand=body.storage".format(pid))
    storage = (((page.get("body") or {}).get("storage") or {}).get("value")) or ""
    external, pages, attachments = _extract_links(storage)
    if args.json:
        _emit_json({"external": external,
                    "pages": [{"space": s, "title": t} for s, t in pages],
                    "attachments": attachments})
        return
    print("# Links on page {0}".format(pid))

    print("\n## External URLs  ({0})".format(len(external)))
    if external:
        for u in external:
            print("- {0}".format(u))
    else:
        print("_(none)_")

    print("\n## Confluence pages  ({0})".format(len(pages)))
    if pages:
        print("\n| space | title | open with |")
        print("|-------|-------|-----------|")
        for s, t in pages:
            ident = "{0}/{1}".format(s, t) if s else t
            print("| {0} | {1} | `view \"{2}\"` |".format(
                s or "-", t, ident))
    else:
        print("_(none)_")

    print("\n## Attachment references  ({0})".format(len(attachments)))
    if attachments:
        for f in attachments:
            print("- {0}  (download: `download {1} --name \"{0}\"`)".format(f, pid))
    else:
        print("_(none)_")


def cmd_search(args):
    q = "/content/search?cql={0}&limit={1}&expand=space".format(
        urllib.parse.quote(args.cql), args.limit)
    data = _api("GET", q)
    if args.json:
        _emit_json(data)
        return
    results = data.get("results") or []
    print("# Search: {0}".format(args.cql))
    print("_{0} of {1} result(s)_\n".format(len(results), data.get("size", "?")))
    if not results:
        print("_(no results)_")
        return
    print("| id | space | type | title |")
    print("|----|-------|------|-------|")
    for r in results:
        space = (r.get("space") or {}).get("key") or "-"
        print("| {0} | {1} | {2} | {3} |".format(
            r.get("id", "?"), space, r.get("type", "-"),
            (r.get("title") or "").replace("|", "\\|")))


def cmd_raw(args):
    pid = _resolve_page(args.page)
    path = "/content/{0}".format(pid)
    if args.expand:
        path += "?expand=" + args.expand
    _emit_json(_api("GET", path))


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="confluence.py", description="Intel on-prem Confluence CLI (stdlib only).")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit raw API JSON")

    sp = sub.add_parser("view", help="render a page (header + readable body)")
    sp.add_argument("page", help="page URL, pageId, or SPACE/Title")
    add_json(sp)
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("attachments", help="list a page's attachments")
    sp.add_argument("page", help="page URL, pageId, or SPACE/Title")
    add_json(sp)
    sp.set_defaults(func=cmd_attachments)

    sp = sub.add_parser("download", help="download attachment(s) to a directory")
    sp.add_argument("page", help="page URL, pageId, or SPACE/Title")
    sp.add_argument("--name", help="filename to download")
    sp.add_argument("--all", action="store_true", help="download all attachments")
    sp.add_argument("--dir", help="output directory (default: cwd)")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("links", help="list links found inside a page")
    sp.add_argument("page", help="page URL, pageId, or SPACE/Title")
    add_json(sp)
    sp.set_defaults(func=cmd_links)

    sp = sub.add_parser("search", help="CQL search")
    sp.add_argument("cql", help="a CQL query, e.g. 'title ~ \"TRAQs\"'")
    sp.add_argument("--limit", type=int, default=25)
    add_json(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("raw", help="dump raw content JSON (escape hatch)")
    sp.add_argument("page", help="page URL, pageId, or SPACE/Title")
    sp.add_argument("--expand", help="comma-separated expand list "
                                     "(e.g. body.storage,version,ancestors)")
    sp.set_defaults(func=cmd_raw)

    return p


def main(argv=None):
    # Confluence returns UTF-8 (accented names, middots, non-breaking spaces);
    # force UTF-8 stdout so a cp1252 Windows console doesn't mangle them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
