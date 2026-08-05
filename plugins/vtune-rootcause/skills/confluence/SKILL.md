---
name: confluence
description: >-
  Read Intel on-prem Confluence (wiki.ith.intel.com) on the user's behalf — open
  and render a page by URL/id, list and download its attached documents to a path,
  and enumerate the links inside a page so they can be followed. Uses the
  CONFLUENCE_TOKEN env var (Bearer PAT). Use when the user gives a
  wiki.ith.intel.com link or asks to read a Confluence/wiki page, see or download
  its attachments, or follow the links inside an article.
argument-hint: <page-url | pageId | SPACE/Title>
allowed-tools:
  - Read
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" *)
---

# Confluence helper

A stdlib-only Python CLI (`confluence.py`) that talks to Intel's on-prem
Confluence REST API and prints **trimmed Markdown** instead of raw JSON, to keep
agent context small. It is **read-only** — it opens pages, lists/downloads
attachments, and enumerates the links inside a page; it never edits or creates
Confluence content.

## Prerequisites

- **`CONFLUENCE_TOKEN`** must be set in the environment — a Confluence Personal
  Access Token, sent as a `Bearer` token. The script reads it from the env at
  runtime and **never** writes it to disk. If it is unset, the script exits with
  a clear message; tell the user to export it.
- Python 3 (any 3.x). No third-party packages, no `jq`, no `pip` needed.
- Base URL defaults to `https://wiki.ith.intel.com`; override with the
  `CONFLUENCE_BASE_URL` env var if ever needed.
- `wiki.ith.intel.com` is an `.intel.com` host, so it is reached directly (it is
  in `no_proxy`) — no proxy handling is required.

## Invocation

Call the script by its path inside this skill directory:

```bash
python "<skill-dir>/confluence.py" <subcommand> ...
```

When invoked as the installed `/confluence` skill, `<skill-dir>` is the directory
containing this `SKILL.md` — i.e. `${CLAUDE_PLUGIN_ROOT}/skills/confluence`. The
sibling `rootcause` skill calls it by exactly that path.

## Command cookbook (intent → command)

| The user wants… | Run |
|-----------------|-----|
| **Open & read a page** (header + readable body) | `confluence.py view "<url>"` |
| Read a page by id or SPACE/Title | `confluence.py view 2822187769` · `confluence.py view "DevSWAnalyzers/TRAQs Architecture and Implementation"` |
| **See which documents are attached** | `confluence.py attachments "<url>"` |
| **Download one attached document** | `confluence.py download "<url>" --name "file.vsdx" --dir ./dl` |
| **Download all attached documents** to a path | `confluence.py download "<url>" --all --dir ./dl` |
| **See the links inside the article** (and how to follow them) | `confluence.py links "<url>"` |
| Follow an internal page link found by `links` | `confluence.py view "SPACE/Title"` (as printed in the "open with" column) |
| Find pages (CQL) | `confluence.py search 'title ~ "TRAQs"'` |
| A field the wrappers don't show | `confluence.py raw "<url>" --expand body.storage,version,ancestors` |

Read commands accept `--json` to emit raw API JSON — use sparingly; the Markdown
output is the default for a reason (context economy).

## Accepted page identifiers

Any subcommand's `PAGE` argument accepts any of these — the script resolves them
all to a numeric page id:

- A **bare numeric pageId**: `2822187769`
- **`SPACE/Title`** shorthand: `"DevSWAnalyzers/TRAQs Architecture and Implementation"`
- A full **wiki URL** in any shape Confluence hands out:
  - `.../pages/viewpage.action?pageId=N`
  - `.../pages/viewpage.action?spaceKey=S&title=T` (title may be `+`-encoded)
  - `.../display/SPACE/Page+Title`
  - `.../spaces/SPACE/pages/N/Title`
  - `.../x/<tiny>` short link (fetched and followed to the real page)

Wrap the argument in quotes — wiki URLs and titles contain `&`, `+`, and spaces.

## Following links inside an article

`confluence.py links "<url>"` splits what it finds in the page body into three
groups:

- **External URLs** — `http(s)://…` links (GitHub, other intranet services, …).
  Follow these with `WebFetch` or `curl` as appropriate, not this skill.
- **Confluence pages** — internal links to other wiki pages, printed with an
  "open with" hint like `` `view "SPACE/Title"` ``. Follow one by running that
  `confluence.py view` command (it re-uses the same resolver).
- **Attachment references** — documents referenced in the body; download one with
  the printed `` `download <id> --name "..."` `` command.

## SAFETY RULES — read before acting

1. This skill is **read-only** — there is no page-edit/create/comment command by
   design. Don't hand-roll one; if the user needs to write to Confluence, say so.
2. **Never** echo, log, or write `CONFLUENCE_TOKEN` (or any secret) into files,
   commits, or terminal output you don't control. The script already keeps it
   env-only — don't undermine that.
3. This skill ships in a **git repo that has a public remote**. Never commit a
   token or other secret into it. `confluence.py` reads the token from the
   environment by design — keep it that way.
4. Prefer the trimmed Markdown commands over `raw` / `--json`. Reach for `raw`
   only when a needed field genuinely isn't surfaced by the wrappers.
5. When downloading attachments, write them to an explicit `--dir` the user (or
   the calling skill) chose — don't scatter files into the cwd unintentionally.

## Output & exit codes

- Exit `0` on success, `2` on a usage/auth error (bad args, missing token), `1`
  on an API failure (the HTTP status and response body are printed to stderr).
- `view` renders the page's rendered HTML as compact Markdown-ish text (headings,
  lists, links); it is a readability aid, not a byte-exact reproduction. Use
  `raw --expand body.storage` if you need the exact source.
