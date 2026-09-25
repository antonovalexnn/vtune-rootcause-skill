---
name: rootcause
description: >-
  Autonomously root-cause an Intel VTune (VASP) Jira ticket. Given a ticket key
  (e.g. VASP-32909; any project prefix), reads the ticket + comments, downloads
  all attachments into a per-ticket folder under vtune_build/jira_artifacts/,
  runs a multi-agent investigation workflow to map it to the owning vcs/ area and
  form a root-cause hypothesis, then optionally builds/tests and edits the
  component to fix it — acting without asking questions, within a staged
  permission model. Use when the user gives a ticket key and asks to
  investigate, root-cause, triage, debug, or fix it.
argument-hint: <TICKET-KEY>
effort: max
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - Workflow
  - Skill(caveman)
  - Skill(caveman:*)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" view *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comments *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" attachments *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" search *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" raw *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" download *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" view *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" attachments *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" download *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" links *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" search *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" raw *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_bootstrap.py" *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_bt.py" *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_pr_review.py" wait *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_pr_review.py" rerequest *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_pr_review.py" resolve *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/vtune-artifactory-build/vab_fetch.py" *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" *)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" fetch --tags*)
  - Bash(python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 run_utest::*)
  - Bash(python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 run_bench::*)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" log:*)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" show:*)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" diff:*)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" status:*)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" blame:*)
  - Bash(git grep:*)
  - Bash(ls:*)
  - Bash(du:*)
  - Bash(file:*)
  - Bash(unzip -l:*)
  - Bash(unzip -v:*)
disallowed-tools:
  - AskUserQuestion
---

# rootcause — autonomous Jira → VTune root-cause investigator

Given one Jira ticket key, investigate and root-cause the problem against the
VTune build repo — your **workspace root**, `${CLAUDE_PROJECT_DIR}` (open Claude
Code with your `vtune_build` checkout as the working directory; the `rc_*.py`
helpers also honour a `VTUNE_BUILD_DIR` env override) — acting **autonomously**
within a staged permission model. Reuse the bundled `jira` skill for all Jira I/O
and the `confluence` skill for any wiki page linked from the ticket — never
reimplement either.

## Autonomy contract

While this skill runs you act autonomously and **never ask the user questions**
mid-run (`AskUserQuestion` is disabled here on purpose). You operate strictly
within the **currently-granted** permission set. Anything outside it is **not
attempted and not silently prompted** — it is *deferred* and enumerated in the
mandatory closing report. This autonomy is scoped to this skill only; normal
chat behavior is unchanged.

The one exception is a hard failure that blocks all progress (e.g. `JIRA_TOKEN`
unset, or the repo is missing) — stop and report it plainly. Do not attempt
auth workarounds.

**The Copilot-review gate is not a violation of this contract.** In Phase D,
after Copilot reviews the PR you present your assessment and then *end the turn*
to await the user's evaluation. That is an ordinary end-of-turn stop, not a
mid-run `AskUserQuestion` — you are not blocking on an interactive prompt, you
have simply finished the autonomous work you were permitted to do and handed
control back. Do not use `AskUserQuestion` for it (still disabled); just stop
and wait for the user's next message.

## Communication style

Chat narration during this run — interim phase updates, the Phase C
**closing report**, and the prose framing the Phase D assessment table —
defaults to **caveman-lite**: drop filler, hedging, and pleasantries; keep
articles and full grammatical sentences; stay professional. Compress
phrasing, never content — every mandated item (each closing-report bullet,
the Phase D gate statement, a hard-failure stop) still appears, stated
plainly enough not to be misread. This never reaches anything persisted —
`NOTES.md`, `ROOTCAUSE.md`, `pr_review/ASSESSMENT.md` (the Phase D table
included, since it is saved verbatim), PR titles/bodies, Jira comments —
those stay full professional prose per their own conventions below.

**Prefer the real skill.** Before Phase A step 1, if `caveman` is listed
among this session's available skills, silently call `Skill(skill:
"caveman", args: "lite")` — no announcement, per its own no-self-reference
rule — then follow its loaded rules (the six levels, the persisted-text
exemption, Auto-Clarity) for the rest of the run instead of the default
above. Absent caveman, keep the default as-is; its absence is not a failure.

**The user's word overrides and never snaps back.** Strengthen with
`/caveman full|ultra|wenyan-*` or plain language ("be more terse"); turn off
with `/caveman off`, "stop caveman", or "normal mode" — honor the same
phrases against the default when caveman isn't installed.

## Input

One ticket key, any `PREFIX-NNNNN` (the prefix is usually `VASP` but may differ
— do not hard-code it). Normalize once:
- `KEY_UPPER` = the key upper-cased (for Jira API calls).
- `key_lower` = the key lower-cased (for the artifact folder name).

## Artifact folder — the single source of truth

Everything downloaded or written lives under:

```
${CLAUDE_PROJECT_DIR}/jira_artifacts/<key_lower>/
```

Nothing is written outside it — not the repo root, not `/tmp`. This directory is
git-ignored (the repo's `.gitignore` is a whitelist), so artifacts stay local.
The only other place you may write is component source under `vcs/<comp>/` (a
Fix — see the permission model). Layout the bootstrap creates:

```
jira_artifacts/<key_lower>/
├── attachments/     # everything downloaded from Jira
├── confluence/      # pages + docs pulled from any linked wiki page
├── test_history/    # the failing test's run history (test_history.py)
├── NOTES.md         # raw fetched material (you fill this in)
└── ROOTCAUSE.md     # the structured report (you fill this in)
# build/ and fix.diff are added later if you reproduce/fix
# pr_review/ (copilot_review.json + your assessment) is added in Phase D
```

## Reusing the jira skill (`jira.py`)

All Jira I/O goes through `jira.py`. `JIRA_TOKEN` must be set in the environment
(a Jira PAT); if a `jira.py` call fails with the token error, stop and report it.
Never echo/write the token anywhere.

| Intent | Command |
|--------|---------|
| Read a ticket | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" view KEY_UPPER` |
| Read comments | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comments KEY_UPPER --limit 30` |
| List attachments | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" attachments KEY_UPPER` |
| Download all attachments | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" download KEY_UPPER --all --dir "<ATTACHMENTS_DIR>"` |
| Extra fields | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" raw KEY_UPPER --fields status,fixVersions,resolution,issuelinks,labels,priority,components` |
| Related tickets | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" search "JQL"` |
| Preview a comment (never auto-post) | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comment KEY_UPPER -m "text" --dry-run` |

## Reusing the confluence skill (`confluence.py`)

VTune tickets often link an on-prem wiki page (`wiki.ith.intel.com`) — an
architecture doc, test spec, or design note that explains the failing feature.
All Confluence I/O goes through the sibling `confluence` skill's `confluence.py`
(read-only). `CONFLUENCE_TOKEN` must be set in the environment (a Confluence PAT);
if a call fails with the token error, note it and continue (a linked wiki page is
helpful context, **not** a hard blocker like `JIRA_TOKEN`). Never echo/write the
token anywhere. Save everything into `<ARTIFACT_DIR>/confluence` (use
`CONFLUENCE_DIR_ARG` from the bootstrap output).

| Intent | Command |
|--------|---------|
| Read a linked page | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" view "<wiki-url>"` |
| List its attached docs | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" attachments "<wiki-url>"` |
| Download the docs into the artifact folder | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" download "<wiki-url>" --all --dir "<CONFLUENCE_DIR_ARG>"` |
| See the links inside the page (to follow) | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" links "<wiki-url>"` |
| Find a page by topic | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" search 'title ~ "TRAQs"'` |

`view`/`attachments`/`links`/`search` also accept a bare pageId or `SPACE/Title`.
`links` splits results into external URLs, internal wiki pages (follow with
`view "SPACE/Title"`), and attachment references.

## Reusing the test-history skill (`test_history.py`)

Most VTune tickets — especially `[BT]` ones — name a failing test. The
`test-history` skill turns that test name into its run history from the Triage
Service report: for each run, the date, OS, platform, pass/fail, the **build
number**, and any **matched Jira key**. This is decisive triage signal: *how
long* the test has been failing, *which build* it began failing on, *which
platforms/OSes* fail vs pass, and whether *other* open Jiras already match the
same failure. It uses **Kerberos SSO — no token** (needs corp network + a valid
Windows login); if a call fails, note it and continue (test history is strong
context, not a hard blocker). Save output into `<ARTIFACT_DIR>/test_history`.

| Intent | Command |
|--------|---------|
| Test history, default 30 days | `python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" history <TESTNAME>` |
| Widen the window | `python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" history <TESTNAME> --days 90` |
| Per-platform summary only (wide ranges) | `python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" history <TESTNAME> --days 180 --summary` |
| Faster, narrowed to a suite | `python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" history <TESTNAME> --suite <SUITE>` |
| Machine-readable records | `python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" history <TESTNAME> --json` |

The report's embedded Jira open/closed state is **unreliable** — trust only the
key, then confirm real status with `jira.py view <KEY>`.

## Platform support matrix — is the failing result even in-support?

A recurring class of "failures" is not a product bug at all: the result was
**collected on a platform VTune no longer supports** (an OS that was dropped, or
a microarch that is out of the support matrix / never went public). Before
concluding "regression", establish whether the failing run's **OS** and
**microarch/CPU** are in VTune's support matrix. There are two sources — use the
wiki first, fall back to the repo.

**Primary — the Analyzers Support Matrix (Confluence, read-only, already
granted).** The single source of truth is one wiki page; read it with the
`confluence` skill (no new grant — `confluence.py view` is already allowed):

```
python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" view 2698724254
```

(pageId `2698724254`, space `DevSWAnalyzers`, "Analyzers Support Matrix" — a
bare pageId is a valid identifier, so this works even if the URL changes.) It
carries two tables, both with a `VTune` row:

- **Current OS Support Matrix** — per OS, one of **P1** (must be tested +
  automated), **P2** (manual testing), **P3** (reactive — bugfix only, no
  testing), **N/A**, or **DROPPED**. A result from a `DROPPED`/`N/A` OS is out of
  support; a `P3` OS is reactive-only (a low-priority "won't proactively test"
  signal, not a hard regression).
  - **⚠ Windows 11 is frequently mislabeled as "Windows 10" by the test infra.**
    The test harness derives the OS string from Python `platform.release()`, which
    on Windows 11 (build ≥ 22000) still returns `'10'` — so failure-signature XMLs,
    the test-history `## By platform` table, and ticket bodies routinely say
    **`Windows 10`** for a box that is actually running **Windows 11**. Do **not**
    treat a "Windows 10" label as EOL/out-of-support at face value. Cross-check the
    *real* OS before ruling: the VTune summary report's `Operating System:` line
    (in the attachment `check_stdout*.txt` / `cl_report_out*.txt`), the Jenkins
    `NODE_NAME` / `BUILD_DISPLAY_NAME` in `environ.txt`/`log.txt` (e.g.
    `...-win11-RPL`), or the `os_name`/OS field in the collected result. In the
    support matrix **Windows 10 is absent (dropped) but Windows 11 = P3
    (reactive)** — so the mislabel flips the verdict from "out-of-support" to
    "reactive-only", a materially different (and less dismissive) reading. When you
    detect this, record both the labeled and the real OS in the "Support matrix"
    section of NOTES.md (cf. VASP-33520, where the XML said `os_name="10"` but the
    report said `Microsoft Windows 11` on node `tst-jf116480-win11-RPL`).
- **Current HW Support Matrix** — per CPU codename abbreviation (`ICL`, `ICX`,
  `SPR`, `MTL`, `GNR`, `PTL`, `DMR`, `NVL`, …), one of **P** (Public), **NDA**,
  **I** (Internal), or **absent/blank**. A microarch that is `absent` (or only
  `Internal`/`NDA` when the failing test runs in the public suite) is not in the
  public support matrix.

Digest the `VTune` row of both tables into NOTES.md — don't paste the whole page.

**Backup — `vcs/perfconfigs4` (read-only, already granted via `Grep`/`git
grep`).** If Confluence is unreachable (`CONFLUENCE_TOKEN` unset, network), or to
cross-check, the repo encodes the *effective* supported platform set. Two anchors:

- `vcs/perfconfigs4/scripts/maps.py` → the **`_PLATFORM_FILE_NAME`** dict maps the
  wiki's HW abbreviations to internal **PMU codenames** (e.g. `'ICX': ['icelake_server']`,
  `'SPR': ['sapphirerapids_server']`, `'PTL': ['cgc']`, `'GNR': ['graniterapids_server']`).
  This is the bridge between a stack/CPU string in the ticket and the codename the
  configs use.
- `vcs/perfconfigs4/include/pmu_common.xsl` (and its `query_library/*.cfg`) →
  `exsl:ctx('PMU') = '<codename>'` switches enumerate the platforms the configs
  still handle. **A microarch that has been *removed* from these switches is
  intentionally de-supported** — dropping a platform's binding here is exactly how
  support is retired (cf. VASP-32463 "Drop old platforms", which legitimately
  broke a test pinned to `skylake`). If the failing result's microarch is absent
  from both the switch and `_PLATFORM_FILE_NAME`, treat it as out-of-support.
  Grep for the codename: `git grep -n "'PMU') = 'skylake'" vcs/perfconfigs4` — no
  hits ⇒ not supported.

**How to use the verdict.** If the failing result was collected on an
out-of-support OS or microarch, the ticket is very likely **not a product
regression** but an out-of-support / stale-test-infra issue — the fix belongs in
the test spec (skip/guard the platform), not the product. Record the finding
explicitly (see the Phase A step below) and let it re-rank the Phase B
hypotheses. If the platform *is* in support, note that too — it strengthens the
case that the failure is real. If the collection platform can't be pinned from
the evidence, say so rather than guessing.

## Workflow

### Phase A — Fetch (main loop)

1. Parse & normalize the key (`KEY_UPPER`, `key_lower`).
2. **Bootstrap** the artifact dir:
   `python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_bootstrap.py" KEY_UPPER`
   It prints `ARTIFACT_DIR`, `ATTACHMENTS_DIR`, `DOWNLOAD_DIR_ARG`,
   `CONFLUENCE_DIR`, `CONFLUENCE_DIR_ARG`, `NOTES`, `REPORT`. Use
   `DOWNLOAD_DIR_ARG` as the `--dir` for Jira downloads and `CONFLUENCE_DIR_ARG`
   for `confluence.py download`.
3. `jira.py view KEY_UPPER` — summary, type, priority, status, components,
   labels, assignee, reporter, description.
4. **Build-tracking (`[BT]`) shortcut — do this whenever the summary starts with
   `[BT]`.** These are auto-filed regression bugs: a test that began failing at a
   specific build. The single most decisive signal is *which components that
   build bumped*, so resolve it up front and carry it into Phase B.
   - Read the build + summary:
     `jira.py raw KEY_UPPER --fields customfield_14617,summary` —
     **`customfield_14617` is the "Found in build"** number (e.g. `630829`).
   - The stream is the second bracket token of the summary: `[BT] [<stream>] …`
     (e.g. `stable`). Pass it as the disambiguator.
   - Resolve build → bumped components:
     `python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_bt.py" <build> --stream <stream>`
     It prints `FOUND_TAG`, `BASELINE_TAG`, `FETCHED`, `BUMPED_COUNT` and a
     `| component | old | new |` table (git tag `#<build>+<stream>+…`, diffed
     against the immediate-predecessor build's `SConstruct component_versions`).
     It auto-runs `git fetch --tags` once if the build tag isn't local yet.
   - **The field can be wrong** (Jira hygiene). Also scan the description and
     comments for `build <N>` / `reproduced on build <N>` mentions. Lead with the
     field, but if a mention disagrees, note it and run `rc_bt.py` for that build
     too — treat both bumped-component sets as candidates.
   - If `rc_bt.py` can't resolve the tag or reports ambiguity, record the exact
     message and fall back to the normal mapping (Jira `components` + stack); do
     not block on it.
4b. **Test history — do this whenever a failing test name is identifiable**
   (`[BT]` summaries embed it as `[BT] [<stream>] <TESTNAME>: <reason>`;
   otherwise pull it from the description/comments). Using the `test-history`
   skill, resolve how long / on which builds / which platforms the test has been
   failing, and whether other open Jiras match the same failure:
   `python "${CLAUDE_PLUGIN_ROOT}/skills/test-history/test_history.py" history <TESTNAME> --days 90`
   Save the output with the `Write` tool to
   `<ARTIFACT_DIR>/test_history/<TESTNAME>_history.md`. This cross-checks the
   `[BT]` "Found in build" (the `fail-since` build should line up with the
   `rc_bt.py` bumped-component build) and surfaces sibling open tickets. It uses
   Kerberos SSO (no token); if it fails, record the message and continue — this
   is context, not a blocker. The report's embedded Jira state is unreliable, so
   confirm any surfaced key with `jira.py view <KEY>`.
   
   **IMPORTANT:** Parse the `## By platform` table to detect **platform/OS
   dependencies** — the output already groups by `(platform, os)` and reports
   `p/f/na` (pass/fail/not-applicable counts) + `last-pass` + `fail-since` for
   each. Look for patterns:
   - 100% pass on one OS, 100% fail on another (OS-specific issue);
   - 100% pass on one platform, 100% fail on another (platform/arch-specific);
   - mixed pass/fail (flaky);
   - fail-since build != last-pass build (regression at a specific build).
   When you find such a pattern, **record it explicitly** in the **"Test
   history"** section of NOTES.md (e.g., "PASS on all Linux runs (5/0/0), FAIL
   on all Windows runs (0/12/0) → Windows-specific issue" or "flaky on all
   platforms (3/5/0 mix)"). This is decisive triage signal — the Phase B
   workflow agents will use it to rank hypotheses (e.g., a Linux-only failure
   points to a Linux-only code path).
4c. **Support-matrix check — do this once you can identify the failing run's
   platform** (from the test-history `## By platform` table, the `[BT]` stream,
   the attachment/dump evidence, or the ticket's environment field). Establish
   whether the **OS** and the **microarch/CPU** the failing result was collected
   on are in VTune's support matrix, per the
   [Platform support matrix](#platform-support-matrix--is-the-failing-result-even-in-support)
   section above:
   - read the wiki page — `confluence.py view 2698724254` — and pull the `VTune`
     row of the OS table (P1/P2/P3/DROPPED/N/A) and the HW table
     (P/NDA/I/absent) for that OS + microarch;
   - if Confluence is unreachable, fall back to `vcs/perfconfigs4`
     (`scripts/maps.py` `_PLATFORM_FILE_NAME` for the abbrev→codename, then
     `git grep -n "'PMU') = '<codename>'" vcs/perfconfigs4` — no hits ⇒ dropped).
   Record the verdict in the new **"Support matrix"** section of NOTES.md: the
   failing platform's OS+microarch and their tags, and a one-line classification —
   `in support (Pn)`, `reactive-only (P3)`, or **`OUT OF SUPPORT (dropped/absent)`**.
   An out-of-support platform is a strong prior that the ticket is **not a product
   regression** but an out-of-support / stale-test-infra issue (fix in the test
   spec, not the product) — carry it into Phase B. This is best-effort: if neither
   source resolves and the platform can't be pinned, record "unknown" and continue.
5. `jira.py comments KEY_UPPER --limit 30` (widen only if the ticket is
   comment-heavy and the tail looks relevant).
6. `jira.py attachments KEY_UPPER`, then
   `jira.py download KEY_UPPER --all --dir "<DOWNLOAD_DIR_ARG>"`. Record the
   *inventory* (filename / size / type) in NOTES.md, but **do not read the
   attachment contents in the main loop** — that's the Stage-1 artifact-reader
   agent's job. Reading large dumps/logs into the main-loop context here would
   re-bill them through Phases B/C/D.
6b. **Linked wiki pages.** Scan the description and comments for
   `wiki.ith.intel.com` URLs. For each one, using the `confluence` skill:
   - `confluence.py view "<wiki-url>"` — read the page;
   - `confluence.py attachments "<wiki-url>"`, then
     `confluence.py download "<wiki-url>" --all --dir "<CONFLUENCE_DIR_ARG>"` —
     pull its docs into `<ARTIFACT_DIR>/confluence`;
   - `confluence.py links "<wiki-url>"` — note further links worth following.
   This is best-effort: if `CONFLUENCE_TOKEN` is unset or a fetch fails, record it
   and move on — a wiki page is context, not a hard blocker.
7. Write **NOTES.md**: trimmed ticket text, comment digest, attachment
   inventory, and any extracted stack traces / repro steps / affected version.
   For a `[BT]` ticket, fill the **"Build-tracking (BT)"** section with the found
   build, resolved tag, baseline tag, and the bumped-component table. Fill the
   **"Linked Confluence pages"** section with each fetched page (title, URL, a
   short digest, and any docs saved under `confluence/`). If step 4b ran, add a
   **"Test history"** digest — the `fail-since` build, the pass/fail split across
   platforms/OSes, and any sibling open Jira keys (saved under `test_history/`).
   If step 4c ran, fill the **"Support matrix"** section — the failing run's
   OS+microarch, their support tags, and the `in support (Pn)` / `reactive-only
   (P3)` / `OUT OF SUPPORT` classification. Never include the token or any secret.

   **Digest, don't hoard.** NOTES.md is the durable context; once material is
   digested into it, **rely on NOTES.md and stop re-reading the raw sources** in
   Phases B/C/D. In particular, a full `confluence.py view` dump can be large —
   capture a short digest into the **"Linked Confluence pages"** section rather
   than pasting the whole page into the main-loop context; re-read the raw page
   only if a specific detail is later needed.

### Phase B — Deep analysis (an ultracode-style Workflow)

8. Call the **`Workflow`** tool with a script that fans out, adversarially
   verifies, then synthesizes. This is the intended way to run the deep,
   multi-agent investigation autonomously. Hand the workflow: `KEY_UPPER`, the
   `ARTIFACT_DIR`, the **path** to `NOTES.md`, the attachment inventory, and the
   repo facts below.

   **Token discipline — this is the skill's single biggest cost, so spend
   deliberately.** Three rules govern every `agent()` call; they cut the bill
   substantially on typical tickets with no loss of triage quality:

   - **Pass NOTES.md by path, never by content.** Hand agents the *path* to
     `NOTES.md` (and the `ARTIFACT_DIR`); each agent `Read`s only the sections it
     needs. Inlining the full NOTES.md (trimmed ticket + comment digest + test
     history + Confluence digest) into every agent prompt replicates it N times —
     don't.
   - **Effort-routing is the primary lever.** Default every finder to
     `opts.effort: 'medium'`. Escalate to `'high'`/`'xhigh'` **only** for a
     `cross_component_regression` class (see Stage 0) and for the synthesis
     stage. The Stage-0 triage-gate itself runs at `effort: 'low'`. Do not put
     `'xhigh'` on every agent — that (plus the old fixed 15-agent fan-out) was
     the original bill.
   - **Do NOT override the model — omit `opts.model`.** Agents inherit the
     session model (Opus 4.8), which is what we want. *Do not set
     `opts.model: 'sonnet'`* on workflow agents: in this environment a model
     override collides with extended-thinking and the agent dies with a `400 …
     Expected 'thinking' … but found 'text'` error. The savings come from the
     triage-gate, the adaptive (smaller) fan-out, `effort: 'medium'`, and passing
     NOTES.md by path — not from downgrading the model.

   Suggested shape:

   - **Stage 0 — triage-gate (one agent, `effort: 'low'`, no model override).**
     Before any fan-out, spend one cheap agent to *size the investigation*. It
     `Read`s the NOTES.md digest (and, for a `[BT]` ticket, notes the
     `BUMPED_COUNT` from the `rc_bt.py` table already in NOTES.md) and returns a
     small structured result (use `schema`): `ticket_class` ∈
     `{single_component_crash, flaky_threshold, cross_component_regression}`, the
     recommended finder set, and a boolean `needs_adversarial`. **Everything
     below scales from this verdict** — do not launch the full fan-out
     unconditionally. In particular:
     - **`[BT]` with `BUMPED_COUNT` ≤ 2 → short-circuit.** The regressing
       component is almost certainly one of those two. Run just the **component
       mapper** (seeded with the bumped list) + **one** verifier + **synthesis**
       (2-3 agents total, not ~15). Skip the broad finder fan-out entirely.
     - **`single_component_crash`** → ~4 agents: artifact reader + component
       mapper + history digger + synthesis.
     - **`cross_component_regression`** → the full fan-out below, with the
       adversarial panel (Stage 2) and escalated effort.
   - **Stage 1 — fan-out finders (parallel), all read-only.** Launch **only the
     finders the Stage-0 verdict called for** (not all five every time). Each
     agent is blind to the others and attacks the mapping from a different angle:
     - artifact/crash-signature reader — `unzip -l`/`file`/Read the downloaded
       files → module/symbol names, versions, exact repro steps;
     - component mapper — Jira `components` + stack module/file names +
       `fixVersions`/labels → **ranked** `vcs/<comp>` candidates, using each
       candidate's `CODEOWNERS`. **For `[BT]` tickets, the bumped-component list
       from `rc_bt.py` (in NOTES.md) is a strong ranked prior: the regression is
       almost certainly one of those components or a transitive dependent of one.
       Start the ranking there** and only look wider if none of them fit the
       signature;
     - **test-history dependency analyzer** — when NOTES.md has a **"Test
       history"** section with platform/OS dependency patterns (pulled from the
       `test_history.py` output's `## By platform` table), use it to narrow the
       search space: if the test passes 100% on Linux but fails 100% on Windows,
       hypotheses that touch only cross-platform code are unlikely; look for
       Windows-specific code paths / `#ifdef _WIN32` / OS-layer interactions.
       Similarly, if flaky across all platforms (mixed p/f), suspect timing /
       sampling threshold issues rather than OS/arch-specific bugs. The pattern
       is a decisive prior — use it to rank hypotheses;
     - **support-matrix analyzer** — read the **"Support matrix"** section of
       NOTES.md (filled in Phase A step 4c). If the failing result was collected on
       an **out-of-support** OS (DROPPED/N/A) or microarch (absent from the
       Analyzers Support Matrix / removed from `perfconfigs4` `pmu_common.xsl`),
       raise the hypothesis that this is **not a product regression** but an
       out-of-support / stale-test-infra issue — the fix belongs in the test spec
       (skip/guard the platform, cf. VASP-32463), not the product. Cross-check the
       ticket's microarch against `vcs/perfconfigs4/scripts/maps.py`
       `_PLATFORM_FILE_NAME` and `git grep "'PMU') = '<codename>'" vcs/perfconfigs4`.
       If the platform *is* in support, downgrade this hypothesis and note that the
       failure is on supported HW (strengthening a real-bug reading);
     - history digger — `git -C vcs/<comp> log/blame/diff` for recent changes
       near the suspected fault;
     - log/failure correlator — root-level `stacktrace.dumps`, `failures.csv`,
       and `logs/` (Grep them directly; if the checkout ships a
       `logs/_analyze.py` summarizer, you may run it).
   - **Stage 2 — adversarial verification (parallel, *conditional*).** Spawn the
     refutation panel **only when** Stage 0 set `needs_adversarial` **or** the
     top hypotheses conflict / synthesis confidence is low. For a clean
     single-component crash or a `[BT]` match to a bumped component, **skip it** —
     one verifier (or none) suffices; the mapping is already well-evidenced.
     When the panel *is* warranted: for each top hypothesis, spawn independent
     verifiers (`effort: 'medium'`, no model override) prompted to **refute** it
     (Read/Grep the suspected `include/ src/ utest/`). A hypothesis survives only
     if it is not refuted by a majority.
   - **Stage 3 — synthesis (single agent).** Return a structured plan:
     root-cause hypothesis (+confidence), evidence chain, surviving
     alternatives, a concrete minimal fix proposal (files + change sketch),
     the exact component to rebuild/retest, and the **reproduction platform** —
     the specific OS (name + version, e.g. `Ubuntu 24.04 / kernel 6.8`,
     `Windows 11`) **and** microarchitecture/platform (e.g. `icelake_server`,
     `panther lake`, `aarch64`, or a concrete CPU like `Xeon 6330`) needed to
     reproduce the failure. Derive it from the **failing `(platform, os)` rows**
     of the test-history "By platform" table in NOTES.md (those combos are
     exactly where the bug reproduces) plus the crash/attachment evidence and,
     for `[BT]`, the stream. State it as a concrete target a box can be **booked
     against**, and give a short reason (which evidence pins it). If the evidence
     shows no OS/arch dependency (e.g. flaky across all platforms, or a pure
     logic bug), say so explicitly: `platform-agnostic — reproduces on any
     supported target`. If the signal is weak/absent (no test history, no
     platform in dumps), return your best guess with `confidence: low` rather
     than omitting it. Also return a **support-matrix verdict** — whether the
     failing run's OS+microarch are in support (`in support (Pn)` /
     `reactive-only (P3)` / `OUT OF SUPPORT (dropped/absent)` / `unknown`), from
     the "Support matrix" section of NOTES.md. When it is out-of-support, make the
     "not a product regression → fix in test spec" reading the leading hypothesis
     unless other evidence overrides it.

   **Scale by the Stage-0 class** — the whole point of the triage-gate is that a
   clear single-component crash must *not* cost the same as a vague
   cross-component regression:

   | Stage-0 class | Finders (Stage 1) | Adversarial (Stage 2) | Effort (model: inherit Opus) |
   |---|---|---|---|
   | `[BT]`, `BUMPED_COUNT` ≤ 2 | mapper only (seeded) | 1 verifier | medium; synth high |
   | `single_component_crash` | reader + mapper + history digger | skip unless conflict | medium; synth high |
   | `flaky_threshold` | reader + test-history analyzer + support-matrix analyzer + correlator | skip unless conflict | medium; synth high |
   | `cross_component_regression` | all 6 finders | full panel (majority-of-3) | high-xhigh; synth xhigh |

   The **support-matrix analyzer** is cheap (`effort: 'low'`, reads NOTES.md +
   greps `perfconfigs4`) — fold it in whenever NOTES.md's "Support matrix" section
   flags anything but a clean `in support` verdict, regardless of class; an
   out-of-support platform reframes the whole investigation.

   The workflow is **read-only**: no edits, no builds inside it. It returns one
   consolidated plan (no approval gate), preserving autonomy.

   Repo facts to pass to the workflow:
   - Repo root `${CLAUDE_PROJECT_DIR}` (the VTune build checkout); ~155 components
     under `vcs/<comp>/` (each with `include/ src/ utest/ *.parts CODEOWNERS`).
   - Per-component build logs in `logs/`; `logs/all.log` (+ a `logs/_analyze.py`
     summarizer if the checkout has one).
   - Crash dumps in root `stacktrace.dumps`; failures in root `failures.csv`.
   - Build/test commands (Phase C, main loop only):
     `python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 <target>`
     where `<target>` is `<comp>::` (build), `run_utest::<comp>::` (build+test),
     or `run_bench::<comp>::`.

### Phase C — Act autonomously (main loop, within the granted set)

9. Record the returned plan into **ROOTCAUSE.md** (hypothesis, evidence,
   alternatives, proposed fix).
10. **(Optional) Reproduce** — build/test the affected component to confirm:
    `python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 run_utest::<comp>::`
    (run from the repo root). Capture the log into `<ARTIFACT_DIR>/build/`.
    Do **not** run `... all` — propose it instead (see permission model).

    **Downloading a prebuilt VTune (optional).** Instead of (or alongside)
    building locally, you may pull an already-built VTune from Artifactory with
    the `vtune-artifactory-build` skill's helper — useful to reproduce on the
    exact **"Found in build"** from the `[BT]` flow:
    `python "${CLAUDE_PLUGIN_ROOT}/skills/vtune-artifactory-build/vab_fetch.py" <build> --dest "<ARTIFACT_DIR>/build"`
    (defaults to the public package, Windows, extracted; reads `VTUNE_BIN` from
    the output). When the user gives you a remote box, add
    `--remote <user@host> [--remote-password-env VAR]` to fetch the build
    directly onto it — append `--remote-os windows` for a Windows remote (a
    Linux remote is the default). Add `--package developer` for the full
    developer package. This is granted (see permission model).

    Package choice matters for what you can do with it:
    - **`--package developer`** ships the **entire test infrastructure**
      (perfdqtests, testspec, collateral — the harness the `[BT]` failures run
      under), so it's the one to pull when you want to *reproduce a failing
      test* rather than just run the profiler. Caveat: driving that
      infrastructure needs **Python 3.8** (`C:/Python38`, same interpreter used
      for scons here) — newer Pythons won't run it.
    - **SEP driver / Windows:** in the Windows build the **SEP driver is
      unsigned**, so hardware sampling (`sep`) won't load from it. If the repro
      needs SEP, also fetch the **regular public package** (`--package public`)
      and install SEP from there; otherwise the developer package alone is fine.
11. **(Optional) Fix** — if the root cause is clear and localized, edit source
    under `vcs/<comp>/`, then re-run step 10 to verify. Before the first edit,
    snapshot the pre-fix state:
    `git -C "${CLAUDE_PROJECT_DIR}" diff -- vcs/<comp>` into
    the artifact dir; after, save the fix diff to `<ARTIFACT_DIR>/fix.diff`.
    Do **not** branch/commit/push/open a PR (deferred).
12. **List changed components** — after any edits, print the explicit list of
    modified components, from
    `git -C "${CLAUDE_PROJECT_DIR}" status --short`. Record it
    in the report. If **more than one** component changed, recommend a full
    `all` build (confirm-first) rather than per-component builds.
13. Finalize **ROOTCAUSE.md**: Ticket summary · Reproduction/inputs ·
    **Support-matrix status** (the failing run's OS+microarch and their support
    tags — `in support (Pn)` / `reactive-only (P3)` / `OUT OF SUPPORT` / `unknown`,
    from the synthesis verdict; when out-of-support, say the ticket is likely a
    test-infra issue, not a product regression) · **Reproduction platform** (the
    OS name+version **and** microarch/CPU needed to reproduce — from the synthesis
    result; `platform-agnostic` if there is no OS/arch dependency) · Affected
    component(s) & owners · Evidence · Root-cause hypothesis (+confidence) ·
    Alternatives · Fix applied (if any) + changed components + `fix.diff`
    reference + build/test result · Suggested next verification · Open questions.
14. Print a summary + the **mandatory closing report** (below).

All intermediate notes, logs, diffs, and the report live inside
`jira_artifacts/<key_lower>/`. Source edits go only under `vcs/<comp>/`.

### Phase D — Copilot PR review loop (main loop; only after a PR is open)

Runs **only** when a draft PR has been opened for the fix and Copilot was
requested as a reviewer (see [Branch & PR conventions](#branch--pr-conventions)
— itself a deferred/confirm-first action). Its purpose: let GitHub Copilot
review the PR, then have you (the agent) triage Copilot's comments and give the
user a reasoned assessment — but **never touch the code until the user has
evaluated that assessment.** The loop is: **wait → assess → STOP for the user →
(on approval) fix + push + re-request → wait …**

Copilot review lands under `<ARTIFACT_DIR>/pr_review/`. Use
`PR_REVIEW_DIR = <ARTIFACT_DIR>/pr_review` as the `--dir` for the helper.

**D1 — Wait for Copilot (granted, read-only).** Copilot reviews asynchronously
(usually a few minutes). Poll for it with the helper — one bounded, blocking
call so there is no shell control-flow:

```
python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_pr_review.py" wait <PR#> \
  --dir "<PR_REVIEW_DIR>" --timeout 480
```

- It prints `STATUS=ready|not_ready|no_pr`. On `ready` it also prints
  `REVIEW_ID`, `REVIEW_STATE`, `COMMENT_COUNT`, and `JSON_PATH` (the review
  dumped to `pr_review/copilot_review.json`).
- On `not_ready` (Copilot hasn't posted within the window) **do not spin** — stop
  the turn and tell the user Copilot hasn't reviewed yet and that they can
  re-invoke to wait again. Re-running is a single clean call; a fresh session
  re-triggers the same `wait`.
- Pass `--proxy http://proxy-iind.intel.com:912` if `gh api` times out (the same
  proxy note as PR creation).
- On a later loop round, pass `--after-review-id <previous REVIEW_ID>` so you
  only accept Copilot's **new** review (the one produced after your push), not
  the one you already handled.

**D2 — Assess each comment (granted, read-only), then STOP.** Read
`copilot_review.json`. For **each** Copilot comment, form your own engineering
judgement by looking at the actual code (`Read`/`Grep` the cited
`path`:`line`, cross-checked against your Phase-B root cause). Then present the
user a compact **assessment table** — one row per comment:

| # | File:line | Copilot's point (short) | Your verdict | Why | Severity | Proposed action |
|---|---|---|---|---|---|---|

- **Your verdict** — `agree` / `partly` / `disagree`, in your own voice. You are
  the reviewer of the reviewer: say plainly where Copilot is right, where it is a
  false positive (e.g. flags a pattern that's intentional or already handled),
  and where it's stylistic noise not worth acting on.
- **Why** — one line of concrete reasoning tied to the code, not a restatement of
  Copilot's comment.
- **Proposed action** — `fix` (with a one-line sketch of the change), `skip`
  (with the reason), or `needs-input`.
- Also summarize the review body (`review.state`, any overall summary) above the
  table, and save your assessment to `pr_review/ASSESSMENT.md`.

Then **end the turn.** Explicitly ask nothing via a tool — just state that you
are waiting for the user's evaluation of the assessment before changing any code,
and that fixing is deferred until they respond. Do **not** edit source, push, or
re-request a review in this step. This is the gate.

**D3 — Apply the user's decision, then loop (main loop).** Only after the user
replies with their evaluation (which comments to address, which to skip, any
extra direction): apply the agreed fixes under `vcs/<comp>/` exactly as in
Phase C step 11 (snapshot the pre-fix diff, save the updated `fix.diff`), re-run
the affected component's `run_utest::<comp>::` to confirm, then update the PR:

1. Commit + push the fix commits to the **same PR branch** (per
   [Branch & PR conventions](#branch--pr-conventions); never a new branch, never
   `master`). Committing/pushing is deferred/confirm-first — the same grant that
   opened the PR covers the follow-up pushes.
2. Resolve the addressed comments by replying "Done" and marking them resolved:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_pr_review.py" resolve <PR#> <comment-id-1> [<comment-id-2> ...]
   ```
   Pass the IDs of **only the comments you actually fixed** (from
   `copilot_review.json`'s `comments[].id` field). Comments that were skipped or
   not acted upon should **not** be resolved. The helper checks if a comment is
   already resolved before acting, so it's safe to call multiple times.
3. Re-request a fresh Copilot review so the cycle can continue:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/skills/rootcause/rc_pr_review.py" rerequest <PR#>
   ```
4. Go back to **D1**, passing `--after-review-id <the REVIEW_ID you just
   handled>`, and repeat assess → STOP → fix.

The loop ends when any of: the user says stop; Copilot's new review has
`COMMENT_COUNT=0` (and/or `REVIEW_STATE=APPROVED`); or the user promotes the PR
out of draft themselves. Record each round's outcome (comments, your verdicts,
what was fixed/skipped, the new review state) in `pr_review/ASSESSMENT.md` and
reflect the final state in ROOTCAUSE.md.

**Gate discipline (the whole point of this phase):** you may *wait for* and
*read* Copilot's review freely, and you may *assess* it freely — but the first
code change of every round happens **only after** the user has evaluated that
round's assessment. Never pre-apply "obvious" fixes before the gate.

## Staged permission model

You may freely do anything in the **granted set**. For an **ungranted** action,
do NOT prompt and do NOT attempt it silently — propose it and record it in the
closing report with the exact grant needed.

**GRANTED (silent):**
- *Investigate* — Jira reads; `download` into the artifact folder; read-only
  Confluence via `confluence.py` (`view`/`attachments`/`links`/`search`/`raw`)
  and `confluence.py download` into `<ARTIFACT_DIR>/confluence`; repo-wide
  `Read`/`Grep`/`Glob`; read-only git (`log`/`show`/`diff`/`status`/`blame`,
  `git grep`); `git fetch --tags` (tags + remote-tracking refs only — needed by
  the `[BT]` flow to resolve recent build tags; touches no branch/working tree);
  `ls`/`du`/`file`/`unzip -l`/`unzip -v`; `rc_bootstrap.py`;
  `rc_bt.py` (build → bumped-component list); the Phase-B `Workflow`;
  `rc_pr_review.py wait`/`rerequest`/`resolve` (poll for / re-request / resolve
  comments on a Copilot PR review — `wait` is read-only; `rerequest` only re-adds
  the reviewer the PR already had; `resolve` replies "Done" and marks fixed
  comments as resolved; all are granted so Phase D can run without prompts).
- *Reproduce (one component)* — `scons ... <comp>::` and `run_utest::<comp>::`
  (also `run_bench::<comp>::`).
- *Download a prebuilt build* — `vab_fetch.py <build> --dest "<ARTIFACT_DIR>/build"`
  (into the artifact folder) or, when the user gives you a box,
  `... --remote <user@host> --remote-dest <dir>` (creds supplied at runtime).
- *Fix* — `Edit`/`Write` source under `vcs/<comp>/` (and notes/report under the
  artifact dir), enabling a fix → build → test loop.

**NOT GRANTED — deferred / confirm-first** (list these, with the exact pattern,
in the closing report when relevant):

| Deferred action | Exact grant to add to `~/.claude/settings.json` `allow` |
|---|---|
| Full-repo build | `Bash(python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 all)` |
| Capture a VTune profile | `Bash('C:/Program Files (x86)/Intel/oneAPI/vtune/latest/bin64/vtune.exe' -collect *)` |
| Create a fix branch | `Bash(git -C "${CLAUDE_PROJECT_DIR}" checkout -b uid_<uid>_*)` — name it per [Branch & PR conventions](#branch--pr-conventions) |
| Commit / push / open PR | `Bash(git ... commit *)`, `Bash(git ... push *)`, `Bash(gh pr create *)` — branch name + PR title/body per [Branch & PR conventions](#branch--pr-conventions); PR is always opened `--draft` with `--reviewer "copilot-pull-request-reviewer[bot]"` |
| Add Copilot as reviewer on the PR | `Bash(gh api -X POST *requested_reviewers*)` (the REST path; also used by `rc_pr_review.py rerequest` each loop round) |
| Push fix commits after a Copilot round (Phase D) | same `Bash(git ... commit *)` / `Bash(git ... push *)` grant — pushed to the existing PR branch, never `master`, and **only after the user has evaluated that round's assessment** |
| Update an existing PR's description | `Bash(gh pr edit * --body-file *)` — show the diff (old body vs. new marker-block content, per the update procedure under [Branch & PR conventions](#branch--pr-conventions)) and get explicit approval before running; never regenerate/replace text outside the `rootcause:body` markers |
| Post a Jira comment | already gated by an `ask` rule — **stays confirm-first**, never auto-posts |

## Branch & PR conventions

Whenever you create a branch, commit, or open a PR against `vtune_build` (only
ever once that action is in the granted set — branching/PR is deferred by
default), follow these exactly. They exist so the change is easy for the
component's engineers to review.

**Never attribute the change to an AI/agent.** Commit messages, PR titles/bodies,
and Jira comments must read as ordinary engineering work — no `Co-Authored-By`
agent/Claude trailer, no "generated by", "with Claude Code", "AI-assisted", or
similar. Write in the author's voice; the change stands on its evidence, not its
tooling.

**Branch name** — always:

```
uid_<uid>_<small_logic_description>_<jira_ticket_number>
```

- `<uid>` — **your** identity, not a hard-coded one. Prefer the `ROOTCAUSE_UID`
  env var if set; otherwise derive it from the local git identity
  (`git -C "${CLAUDE_PROJECT_DIR}" config user.email` → the part before `@`, or
  `git config user.name` lowercased, `_`-separated). Never hard-code another
  person's uid.
- `<small_logic_description>` — a few words, lowercase, `_`-separated, describing
  the change (not the ticket title verbatim); e.g. `fix_dwarf5_rnglistx_parse`.
- `<jira_ticket_number>` — the full ticket key, e.g. `VASP-32909`.
- Example (for uid `jdoe`): `uid_jdoe_fix_dwarf5_rnglistx_parse_VASP-32909`.

**PR title** — start with the Jira number, then a short, plain description of
what changed:

```
VASP-32909: fix DWARF5 rnglistx range resolution in dwarf reader
```

**PR body** — dry and to the point; engineers read these and dislike walls of
text. No filler, no AI-slop. Exactly two short sections:

1. **The problem** — the reason for the change. If you have proof (repro, stack
   trace, failing test, log excerpt, `rc_bt.py` bumped-component evidence), cite
   it concretely.
2. **The fix** — what the change does, at the level of the files/behavior touched.

Keep both tight — bullets over prose. Do not restate the whole investigation;
link to `ROOTCAUSE.md`/artifacts if more detail is warranted.

**No trailing bare Jira link in the body.** The PR is already linked to the
ticket and the Jira key is in the title, so do not append a naked
`https://jira.devtools.intel.com/browse/<KEY>` line at the end. The body is
exactly the two sections below — nothing after `### The fix`.

**Do NOT hard-wrap lines inside the body.** GitHub renders a single newline as a
real line break (GFM soft-break → `<br>`), so a paragraph/bullet split across
~72-char lines shows up as a ragged "staircase" instead of flowing to the panel
width. Write each bullet or paragraph as **one long unwrapped line**; let the
browser wrap it. Blank lines between bullets/sections are fine (they're the
paragraph separators). This applies to the `--body-file` content for both
`gh pr create` and `gh pr edit`.

Wrap this generated content in markers so a later update can find and replace
*only* this block, never anything else in the body:

```
<!-- rootcause:body:start -->
### The problem
...
### The fix
...
<!-- rootcause:body:end -->
```

**Updating an existing PR's description later — never blind-overwrite.** The
author may have hand-edited the PR body in the GitHub UI (added context,
reviewer notes, a checklist). Regenerating the whole body from scratch would
silently destroy that. When asked to update/refresh a PR's description:

1. Fetch the live body first: `gh pr view <PR#> --json body -q .body` (write it
   to a temp file — do not rely on memory of what you wrote when the PR was
   created, the author may have changed it since).
2. If it contains `<!-- rootcause:body:start -->` / `<!-- rootcause:body:end -->`:
   replace **only** the text between those markers with the refreshed
   The problem / The fix sections.
   Everything before, after, or outside the markers is the author's and must
   come through byte-for-byte unchanged.
3. If the markers are **not** present (PR predates this convention, or the
   author rewrote the body without them): do **not** overwrite anything.
   Append a new block with the markers after the existing content instead, and
   say plainly in the closing report that you appended rather than replaced
   because no managed markers were found.
4. Apply with `gh pr edit <PR#> --body-file <path>` (a file, never inline
   `--body`, so multi-line content and the markers survive intact).

**Opening the PR** — always open it as a **draft** and always request a review
from **GitHub Copilot**. Never open a non-draft PR from this skill; the author
promotes it out of draft themselves after review.

Two steps (do NOT rely on `gh pr create --reviewer` for Copilot — in this org
that flag uses the GraphQL `requestReviewsByLogin` path, which fails with
`Could not resolve user with login 'copilot-pull-request-reviewer[bot]'`):

1. Create the draft PR (base is the repo's default branch, usually `master`),
   with the body wrapped in the `rootcause:body` markers from the start (so a
   later update can find and replace it cleanly) via `--body-file`:
   ```
   gh pr create --draft --base master \
     --title "VASP-<num>: <short description>" \
     --body-file <path to file containing the marked-up The problem / The fix>
   ```
2. Add Copilot via the **REST** `requested_reviewers` endpoint (this works):
   ```
   gh api -X POST "repos/<owner>/<repo>/pulls/<PR#>/requested_reviewers" \
     -f "reviewers[]=copilot-pull-request-reviewer[bot]"
   ```
   Confirm the response's `requested_reviewers` contains `{"login":"Copilot",
   "type":"Bot"}`. If it does not, report it in the closing report rather than
   leaving the PR silently reviewer-less.

- `--draft` is mandatory — every PR this skill opens starts as a draft.
- Adding Copilot as a reviewer is mandatory.
- Once the PR is open and Copilot is requested, proceed to
  [Phase D](#phase-d--copilot-pr-review-loop-main-loop-only-after-a-pr-is-open):
  wait for Copilot's review, assess it, then stop for the user's evaluation
  before any further code change.
- **Network:** GitHub's API (`api.github.com`) may be reachable only via the
  corporate proxy even when `git push` works directly. If `gh`/`gh api` times
  out, prefix with `export https_proxy=http://proxy-iind.intel.com:912
  http_proxy=http://proxy-iind.intel.com:912` and retry.

**Link the PR back in Jira** — once the draft PR is open, post a Jira comment on
the ticket with exactly this format (nothing more):

```
PR with fix: <PR URL>
```

This is still a Jira comment, so the confirm-first rule below applies: preview
with `jira.py comment ... --dry-run`, then post only on explicit approval. Posting
may need the corporate proxy (same `export https_proxy/http_proxy=…:912` prefix).

## Safety rules

- **Never auto-post a Jira comment.** If you propose one, show the exact final
  text and use `jira.py comment ... --dry-run` for preview. A real post is
  confirm-first (a global `ask` rule enforces this regardless) — never post
  without explicit user approval, even at max autonomy.
- **Never** echo/log/write `JIRA_TOKEN` or any secret into files, the report, or
  output.
- **Write only** under `jira_artifacts/<key_lower>/` (notes/report/logs/diffs)
  and `vcs/<comp>/` (source fix). Never elsewhere.
- **Never run a full `all` build** without explicit confirmation — build only the
  affected component. Prefer proposing `all` when the fix touched *many*
  components.
- **Never** commit, push, create branches, or open PRs unless that action is in
  the granted set (deferred by default). The repo is shared, checked out on
  `master` — do not switch branches. Snapshot the pre-fix diff before editing so
  the change is reviewable. **When branching/PR *is* granted**, name the branch
  and write the PR strictly per [Branch & PR conventions](#branch--pr-conventions)
  below — never push directly to `master`.
- **Phase D gate — never change code before the user evaluates.** You may wait
  for, read, and assess Copilot's PR review freely, but the first source edit,
  commit, push, or re-request of each review round happens **only after** the
  user replies to that round's assessment. Present the assessment, then stop the
  turn and wait. Never pre-apply a fix because it "looks obviously right."

## Closing report (mandatory — every run)

End with:
- Where artifacts landed (`<ARTIFACT_DIR>`), the top hypothesis (+confidence),
  the list of changed components, and whether a fix was applied and verified.
- **Support-matrix status** — one line: was the failing result collected on a
  supported platform? Give the OS+microarch and their tags (`in support (Pn)` /
  `reactive-only (P3)` / `OUT OF SUPPORT (dropped/absent)` / `unknown`). If
  out-of-support, say plainly that this is likely a test-infra/out-of-support
  issue rather than a product regression.
- **Reproduction platform** — state plainly which OS (name+version) and
  microarch/CPU the failure needs to reproduce (or `platform-agnostic` if it has
  no OS/arch dependency), with the one-line reason it's pinned there. This tells
  the user exactly what box to book for a live repro. If a matching booked box is
  already known (see the repro-box memories), name it.
- If a PR was opened and Phase D ran: the PR URL, whether Copilot has reviewed,
  and where you are in the loop (awaiting the user's evaluation of round N /
  fixed-and-re-requested / Copilot approved / stopped). If you are at the gate,
  say so plainly: "waiting for your evaluation before touching the code."
- A **"Permissions I still lack for full autonomy"** section: each deferred
  action you would have taken next + the exact tool pattern that would unlock it
  (from the table above). If nothing was deferred, say
  "none — the granted set was sufficient."
