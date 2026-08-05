---
name: test-history
description: >-
  Look up an Intel VTune/Analyzers test's run history by test name — for each run
  it returns the date, OS, platform, pass/fail status, the build number it ran on,
  and the matched Jira ticket (when a failure is tied to one). Data comes from the
  Triage Service `TestReportRemastered` report on pbirs01, via Kerberos SSO (no
  token). Use when the user gives a test name and asks for its history, how long
  it has been failing / flaky, when it last passed, or which builds/platforms it
  fails on.
argument-hint: <test-name> [--days N]
allowed-tools:
  - Read
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" *)
---

# test-history helper

A stdlib-only Python CLI (`test_history.py`) that returns a VTune/Analyzers
test's **run history** as **trimmed Markdown** (a per-platform summary plus the
individual runs), to keep agent context small.

The data does **not** come from TRAQs directly — TRAQs is a write-only
submission system with no read API. It comes from the downstream **Triage
Service** SSRS report **`TestReportRemastered`**, queried through the SQL Server
Reporting Services URL-access endpoint and rendered as XML. This skill is
**read-only** — it renders one report; it never writes to any system.

## Prerequisites

- **No token.** The report host authenticates with **Kerberos / Negotiate SSO**
  using your current Windows login (`curl --negotiate -u :`). You must be **on
  the Intel corp network** with a valid interactive login. If you are off-network
  or your Kerberos ticket is stale, the fetch returns an HTML 401 page and the
  script exits with a clear message.
- **`curl`** must be on `PATH` (Windows ships `curl.exe` with SSPI Kerberos;
  git-bash `curl` with SPNEGO works too). Override with the `CURL_BIN` env var.
- Python 3 (any 3.x). No third-party packages, no `jq`, no `pip` needed.
- Base URL defaults to `https://pbirs01.intel.com`; override with the
  `TEST_HISTORY_BASE_URL` env var if ever needed. `pbirs01.intel.com` is an
  `.intel.com` host reached directly (no proxy).

## Invocation

Call the script by its path inside this skill directory:

```bash
python "<skill-dir>/test_history.py" history <test-name> [options]
```

When invoked as the installed `/test-history` skill, `<skill-dir>` is the
directory containing this `SKILL.md` — i.e.
`${CLAUDE_PLUGIN_ROOT}/skills/test-history`. The sibling `rootcause` skill calls
it by exactly that path.

## Command cookbook (intent → command)

| The user wants… | Run |
|-----------------|-----|
| Test history, last 30 days (default) | `test_history.py history pshe_cli_dot_net_managed2native_ijw_lh_vtss` |
| A different look-back window | `test_history.py history <test> --days 90` |
| An explicit date range | `test_history.py history <test> --start 06/10/2026 --end 07/22/2026` |
| Just the per-platform summary (wide ranges) | `test_history.py history <test> --days 180 --summary` |
| Narrow + speed up the query by suite | `test_history.py history <test> --suite ps_dq_cli_64` |
| A different product branch / testing type | `test_history.py history <test> --branch PiersolHE_master --type "Integration Testing"` |
| Machine-readable records | `test_history.py history <test> --json` |
| Just see the report URL (don't fetch) | `test_history.py history <test> --url` |

The default output is a per-platform summary followed by every run (newest
first). Use `--json` for the raw records (one flat object per run) — use it
sparingly; the Markdown output is the default for context economy. Wide ranges
(e.g. `--days 180`) return 1000+ runs and can take a couple of minutes — prefer
`--summary` and/or `--suite` for those.

## Understanding the output

Each run's **Status** maps to a verdict:

- `Passed` / `Passed with Warnings` → **PASS**
- `Not Applicable` → **NA** (the test was skipped as not-applicable for that
  config — this is normal, e.g. a Windows test on a Linux box)
- anything else → **FAIL**. The failure is shown either as a matched **Jira key**
  (e.g. `VASP-34360`) or as the raw failure reason (e.g.
  `Unexpected exit code: 1`, `Function not found`) when no Jira is matched.

The per-platform summary shows, for each `(platform, OS)` bucket: run counts as
`pass/fail/na`, the **last-passed build**, the build the current failing streak
**started on** (`fail-since`), and any matched Jira keys.

**Caveat — the report's embedded Jira open/closed state is unreliable.** The
report emits a state token next to the key (e.g. `VASP-34360 : None`), but it is
often wrong (VASP-34360 is actually **Open** yet the report says `None`). This
skill surfaces the **key** as-is and does not trust that state. If you need the
authoritative open/closed status or the ticket's real summary, cross-check the
key with the `jira` skill (`jira.py view <KEY>`).

## SAFETY RULES — read before acting

1. This skill is **read-only** — it renders one report. There is no write command
   by design; don't hand-roll one.
2. There is **no secret to leak** here (Kerberos SSO, no token). Still, never
   echo credentials or write them into files you don't control.
3. This skill ships in a **git repo that has a public remote**. Never commit
   secrets into it.
4. Prefer the trimmed Markdown output over `--json`; reach for `--json` only when
   a caller needs to parse the records.

## Output & exit codes

- Exit `0` on success, `2` on a usage/auth error (bad args, e.g. only one of
  `--start`/`--end`), `1` on a fetch failure (curl error, empty response, or an
  HTML 401 auth page — the URL and any detail are printed to stderr).
- Dates are normalized to `YYYY-MM-DD HH:MM:SS`; build numbers, OS, platform, and
  status are echoed from the report.
