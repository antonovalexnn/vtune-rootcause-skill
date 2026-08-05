# vtune-rootcause-skill

A Claude Code **plugin** that packages the `/rootcause` skill — an autonomous
Intel VTune (VASP) Jira root-cause investigator — together with the four helper
skills it drives:

| Skill | What it does |
|-------|--------------|
| **`rootcause`** | Given a Jira ticket key, reads the ticket + comments + attachments, downloads everything into a per-ticket artifact folder, runs a multi-agent workflow to map the failure to a `vcs/<comp>` component and form a root-cause hypothesis, then optionally builds/tests, fixes, and opens a Copilot-reviewed draft PR — all within a staged permission model. |
| **`jira`** | Read / search / download / (confirm-first) comment on on-prem Jira (`jira.devtools.intel.com`). |
| **`confluence`** | Read on-prem Confluence (`wiki.ith.intel.com`) pages, attachments, and links (read-only). |
| **`test-history`** | Look up a VTune/Analyzers test's run history (date / OS / platform / pass-fail / build / matched Jira) from the Triage Service report. |
| **`vtune-artifactory-build`** | Download a VTune daily build from Artifactory by build number, locally or onto a remote box. |

All five ship in **one** plugin so `rootcause` can call the others by a portable
`${CLAUDE_PLUGIN_ROOT}` path — no username-specific paths, no symlinks, no
`settings.json` edits.

## Install (teammates)

In Claude Code:

```
/plugin marketplace add antonovalexnn/vtune-rootcause-skill
/plugin install vtune-rootcause@vtune-rootcause-skill
```

That's it — `/rootcause`, `/jira`, `/confluence`, `/test-history`, and
`/vtune-artifactory-build` become available, with their tool permissions applied
automatically from each skill's `allowed-tools`.

To update later: `/plugin marketplace update vtune-rootcause-skill`.

## Usage

Open Claude Code **with your VTune build checkout as the workspace root** (this is
what `${CLAUDE_PROJECT_DIR}` resolves to — where artifacts are written and where the
repo-aware git/build commands run), then:

```
/rootcause VASP-XXXXX
```

The helper skills also work standalone, e.g. `/jira view VASP-XXXXX`,
`/test-history history <test_name>`.

## Prerequisites

**Required for the core investigation**
- **Python 3** on `PATH` (the helpers are stdlib-only — no `pip` install).
- **`JIRA_TOKEN`** — a Jira Personal Access Token, exported in the environment. This
  is a **hard blocker**: `rootcause` and `jira` cannot read tickets without it. The
  token is read at runtime and never written to disk.

**Recommended (each is best-effort — its absence is noted, not fatal)**
- **`CONFLUENCE_TOKEN`** — a Confluence PAT, for reading wiki pages linked from a
  ticket and the Analyzers Support Matrix.
- **Intel corp network + a valid Windows login** — `test-history` authenticates via
  **Kerberos SSO** (no token). Off-network it degrades gracefully.
- **`curl`** on `PATH` — used by `test-history` and `vtune-artifactory-build`.

**For the optional build / reproduce / PR phases**
- A local **VTune build checkout** as the workspace root, for building components and
  writing artifacts under `jira_artifacts/`.
- **`git`** and the **GitHub CLI `gh`** (authenticated) — for the fix → draft-PR →
  Copilot-review loop. The repo must have the `copilot-pull-request-reviewer[bot]`
  reviewer available.

### Environment overrides

| Var | Purpose | Default |
|-----|---------|---------|
| `VTUNE_BUILD_DIR` | Force the VTune build repo root the helpers use | falls back to `CLAUDE_PROJECT_DIR`, then the current working directory |
| `ROOTCAUSE_UID` | The `uid_<uid>_…` prefix for fix branches | derived from your local `git config user.email` / `user.name` |
| `JIRA_BASE_URL` / `CONFLUENCE_BASE_URL` / `TEST_HISTORY_BASE_URL` | Override the on-prem service hosts | the Intel on-prem defaults |

### Machine-specific residuals (Windows / Intel dev baseline)

A few commands in `rootcause` assume the author's Windows/Intel toolchain layout.
They only affect the **optional** Phase-C reproduce / artifactory-extract steps — the
Jira → analysis → report core works without them. Adjust for your box if needed:

- **scons build** uses `python "C:/Python38/Scripts/scons.py" …` (VTune's build needs
  Python 3.8). If your scons/Python 3.8 lives elsewhere, the build command and its
  `allowed-tools` grant differ — Claude will surface a permission prompt you can
  approve, or edit the grant.
- **`vtune-artifactory-build`** extracts the developer package with 7-Zip at
  `C:\Program Files\7-Zip\7z.exe` (`WIN_7ZIP` in `vab_fetch.py`). Only matters when
  extracting a developer package locally on Windows.
- **`gh`/`gh api`** may need the corporate proxy (`http://proxy-iind.intel.com:912`);
  `rootcause` documents this inline.

## Layout

```
.claude-plugin/marketplace.json           # marketplace catalog
plugins/vtune-rootcause/
├── .claude-plugin/plugin.json            # the plugin manifest
└── skills/
    ├── rootcause/     SKILL.md + rc_bootstrap.py, rc_bt.py, rc_pr_review.py
    ├── jira/          SKILL.md + jira.py
    ├── confluence/    SKILL.md + confluence.py
    ├── test-history/  SKILL.md + test_history.py
    └── vtune-artifactory-build/  SKILL.md + vab_fetch.py
```

## Local development / dogfooding

Add the marketplace from a local checkout instead of GitHub, then install:

```
/plugin marketplace add C:/Users/<you>/workspace/vtune-rootcause-skill
/plugin install vtune-rootcause@vtune-rootcause-skill
```

If you also have the skills installed another way (e.g. `~/.claude/skills`
symlinks), remove those first to avoid a name clash while testing the plugin copy.
