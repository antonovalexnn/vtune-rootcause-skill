---
name: jira
description: Read, search, download from, comment on, and update Intel on-prem Jira (jira.devtools.intel.com) on the user's behalf — post comments, change status (transition), reassign, and move sprints. Use when the user references a VASP-/Jira ticket key, asks to read a ticket's description/comments/attachments, search Jira, post a comment, or change a ticket's status/assignee/sprint.
allowed-tools:
  - Read
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" view *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comments *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" attachments *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" search *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" raw *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" download *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comment * --dry-run)
---

<!-- Read-only subcommands (view/comments/attachments/search/raw/download) and a
     comment --dry-run preview are granted above so they run without a prompt. The
     write subcommands — `comment` (real post), `assign`, `transition`, `sprint` —
     are deliberately NOT granted: they must stay confirm-first (see SAFETY RULES).
     Preview a comment with `--dry-run`, then post only on explicit user approval. -->


# Jira helper

A stdlib-only Python CLI (`jira.py`) that talks to Intel's on-prem Jira REST API
and prints **trimmed Markdown** instead of raw JSON, to keep agent context small.

## Prerequisites

- **`JIRA_TOKEN`** must be set in the environment — a Jira Personal Access Token,
  sent as a `Bearer` token. The script reads it from the env at runtime and
  **never** writes it to disk. If it is unset, the script exits with a clear
  message; tell the user to export it.
- Python 3 (any 3.x). No third-party packages, no `jq`, no `pip` needed.
- Base URL defaults to `https://jira.devtools.intel.com`; override with the
  `JIRA_BASE_URL` env var if ever needed.

## Invocation

Call the script by its path inside this skill directory:

```bash
python "<skill-dir>/jira.py" <subcommand> ...
```

When invoked as the installed `/jira` skill, `<skill-dir>` is the directory
containing this `SKILL.md` — i.e. `${CLAUDE_PLUGIN_ROOT}/skills/jira`. The sibling
`rootcause` skill calls it by exactly that path.

## Command cookbook (intent → command)

| The user wants… | Run |
|-----------------|-----|
| Read a ticket (summary + description + meta) | `jira.py view VASP-33648` |
| Read the comment thread | `jira.py comments VASP-33648` |
| Read more/older comments | `jira.py comments VASP-33648 --limit 50` |
| List attachments | `jira.py attachments VASP-33648` |
| Download one attachment | `jira.py download VASP-33648 --name file.zip --dir ./dl` |
| Download all attachments | `jira.py download VASP-33648 --all --dir ./dl` |
| Find tickets (JQL) | `jira.py search "assignee = currentUser() AND status = 'In Progress'"` |
| **Post a comment** | `jira.py comment VASP-33648 -m "text"` — see SAFETY RULES first |
| Preview a comment without posting | `jira.py comment VASP-33648 -m "text" --dry-run` |
| Comment with long/multi-line text | write it to a file, then `jira.py comment VASP-33648 -f body.txt` |
| **Change status** — list options first | `jira.py transition VASP-33648 --list` |
| **Change status** — by target name | `jira.py transition VASP-33648 --to "In Progress"` (substring, case-insensitive; `--id N` for an exact transition) |
| **Reassign** — find the login first | `jira.py assign VASP-33648 --search <name>` (lists assignable users; mutates nothing) |
| **Reassign** — set / clear | `jira.py assign VASP-33648 --to <login>` (or `--to -1` to unassign) |
| **Move to a sprint** — list first | `jira.py sprint VASP-33648 --list [--board N] [--state active,future]` |
| **Move to a sprint** — by name / id | `jira.py sprint VASP-33648 --to "AE Agile Team Core Sprint 50"` (board inferred from the issue; or `--id N`) |
| A field the wrappers don't show | `jira.py raw VASP-33648 --fields status,fixVersions` |

Read commands accept `--json` to emit raw API JSON — use sparingly; the Markdown
output is the default for a reason (context economy).

**All write commands (`comment`, `transition`, `assign`, `sprint`) accept
`--dry-run`** — it prints the exact payload and changes nothing. Use it to preview.
The `--list` / `--search` modes are always read-only.

**Resolution & inference notes:**
- `transition --to` matches on the target-status *or* transition name as a
  case-insensitive substring; ambiguous matches error and ask for `--id`. The set
  of available transitions depends on the current status, so run `--list` if unsure.
- `assign --to` takes a **login** (e.g. `jdoe`), not a display name — use
  `--search NAME` first to find it.
- `sprint` infers the board from the issue's Sprint custom field (the field id is
  not fixed across instances, so it is discovered, not hard-coded). If inference
  fails, pass `--board N` (find it via `sprint … --list --board N`). Name matching
  is a case-insensitive substring; `--state active,future` narrows the search and
  avoids matching closed sprints with similar names.

**Not supported — pinning a comment.** On this Jira DC instance the pin endpoint
(`rest/internal/*/issue/{id}/comment/{id}/pin`) rejects the Bearer PAT and redirects
to `login.jsp?permissionViolation=true`; it needs an interactive browser session.
Every other write here works with the PAT — pin manually in the web UI if asked.

## SAFETY RULES — read before acting

1. **`comment`, `transition`, `assign`, and `sprint` are writes performed on the
   user's behalf against a shared ticket.** Before running any of them without
   `--dry-run`, confirm the intended change with the user (for `comment`, show the
   **exact final text**). Never auto-post or auto-mutate. When in doubt, run
   `--dry-run` (or the `--list`/`--search` read-only mode) first and show the
   result. Verify the ticket **key** and that you are changing the right ticket.
2. **Never** echo, log, or write `JIRA_TOKEN` (or any secret) into files,
   commits, comment bodies, or terminal output you don't control. The script
   already keeps it env-only — don't undermine that.
3. This skill ships in a **git repo that has a public remote**. Never commit a
   token or other secret into it. `jira.py` reads the token from the environment
   by design — keep it that way.
4. Prefer the trimmed Markdown commands over `raw` / `--json`. Reach for `raw`
   only when a needed field genuinely isn't surfaced by the wrappers.
5. Verify the ticket **key** before a write. Posting to the wrong ticket is hard
   to undo cleanly.

## Writing comments: Jira wiki markup (NOT GitHub Markdown)

Jira renders its own wiki syntax. When composing a comment body, use:

- Mention a user: `[~username]`  (e.g. `[~lcrisant]`)
- Bold / italic: `*bold*`, `_italic_`
- Headings: `h2. Title`  (also seen: `+underlined+`)
- Bulleted list: lines starting with `* `   Numbered list: lines starting with `# `
- Inline code: `{{code}}`   Code block: `{code:python}...{code}` or `{code}...{code}`
- Preformatted, no styling: `{noformat}...{noformat}`
- Quote: `{quote}...{quote}`
- Link: `[text|https://url]`

Do **not** assume GitHub-flavored Markdown will render — it won't.

## Output & exit codes

- Exit `0` on success, non-zero on any error — missing token, bad args, or an
  API failure (the HTTP status and response body are printed to stderr).
- All timestamps are echoed verbatim from Jira (ISO-8601 with offset).
