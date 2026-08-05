---
name: implement-feature
description: >-
  Autonomously implement a feature in the Intel VTune build repo, driven by
  EITHER a Jira ticket key (e.g. VASP-32909; any project prefix) OR a free-text
  feature description typed in the agent window. In ticket mode it reads the
  ticket + comments + attachments (and any linked Confluence page); in free-text
  mode it works straight from the user's written request. It designs the change
  against the vcs/ components via a multi-agent workflow, STOPS for the user to
  approve the plan, then implements it, adds tests (unit-preferred, integration
  as fallback), and — confirm-first — opens a Copilot-reviewed draft PR, acting
  within a staged permission model. Use when the user gives a ticket key and
  asks to build/implement/add the feature it describes, or asks to implement a
  feature they describe directly in chat.
argument-hint: <TICKET-KEY | feature description>
effort: max
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - Workflow
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
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_bootstrap.py" *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_pr_review.py" wait *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_pr_review.py" rerequest *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_pr_review.py" resolve *)
  - Bash(python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 *::)
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

# implement-feature — autonomous Jira/free-text → VTune feature implementer

Given either one Jira ticket key **or** a free-text feature description, design
and implement the feature against the VTune build repo — your **workspace root**,
`${CLAUDE_PROJECT_DIR}` (open Claude Code with your `vtune_build` checkout as the
working directory; the `if_*.py` helpers also honour a `VTUNE_BUILD_DIR` env
override) — acting **autonomously** within a staged permission model, but pausing
once, after design, for the user to approve the plan before any code is written.
Reuse the bundled `jira` skill for all Jira I/O (ticket mode only) and the
`confluence` skill for any wiki page linked from the ticket or the request —
never reimplement either.

## Autonomy contract

While this skill runs you act autonomously and **never ask the user questions**
mid-run (`AskUserQuestion` is disabled here on purpose). You operate strictly
within the **currently-granted** permission set. Anything outside it is **not
attempted and not silently prompted** — it is *deferred* and enumerated in the
mandatory closing report. This autonomy is scoped to this skill only; normal
chat behavior is unchanged.

The one exception is a hard failure that blocks all progress (e.g. in ticket
mode `JIRA_TOKEN` is unset, or the repo is missing) — stop and report it
plainly. Do not attempt auth workarounds.

**Two gates are not violations of this contract.** This skill deliberately stops
the turn twice and hands control back:
1. **The plan-approval gate (Phase B.5)** — after design you present the
   implementation plan and *end the turn* to await the user's approval before
   writing any code.
2. **The Copilot-review gate (Phase E)** — after Copilot reviews the PR you
   present your assessment and *end the turn* to await the user's evaluation.

Both are ordinary end-of-turn stops, not mid-run `AskUserQuestion` prompts — you
are not blocking on an interactive prompt, you have simply finished the
autonomous work you were permitted to do and handed control back. Do not use
`AskUserQuestion` for either (still disabled); just stop and wait for the user's
next message.

## Input — two modes

The argument is classified once, at the start:

- **Ticket mode** — the argument matches `PREFIX-NNNNN` (the prefix is usually
  `VASP` but may differ — do not hard-code it). Normalize:
  - `KEY_UPPER` = the key upper-cased (for Jira API calls).
  - `key_lower` = the key lower-cased (for the artifact folder name).
- **Free-text mode** — anything else. The whole argument (plus any surrounding
  chat context the user gave) **is** the feature request; there is no ticket, so
  make **no** `jira.py` calls. The user's words are the source of truth.

`if_bootstrap.py` (Phase A step 1) does this same classification and prints
`INPUT_MODE=ticket|freetext` so the rest of the workflow can branch. Trust its
verdict.

## Artifact folder — the single source of truth

Everything downloaded or written lives under:

```
${CLAUDE_PROJECT_DIR}/jira_artifacts/<folder>/
```

`<folder>` is `key_lower` in ticket mode, or a deterministic sanitized slug of
the description in free-text mode (both produced by `if_bootstrap.py`). Nothing
is written outside it — not the repo root, not `/tmp`. This directory is
git-ignored (the repo's `.gitignore` is a whitelist), so artifacts stay local.
The only other place you may write is component source under `vcs/<comp>/` (an
Implementation — see the permission model). Layout the bootstrap creates:

```
jira_artifacts/<folder>/
├── attachments/        # everything downloaded from Jira (ticket mode)
├── confluence/         # pages + docs pulled from any linked wiki page
├── NOTES.md            # raw fetched material / the request (you fill this in)
└── IMPLEMENTATION.md   # the structured design + plan (you fill this in)
# build/ and impl.diff are added later when you implement
# pr_review/ (copilot_review.json + your assessment) is added in Phase E
```

## Reusing the jira skill (`jira.py`) — ticket mode only

In **ticket mode**, all Jira I/O goes through `jira.py`. `JIRA_TOKEN` must be
set in the environment (a Jira PAT); if a `jira.py` call fails with the token
error, stop and report it. Never echo/write the token anywhere. In **free-text
mode** these commands are not used at all.

| Intent | Command |
|--------|---------|
| Read a ticket | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" view KEY_UPPER` |
| Read comments | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comments KEY_UPPER --limit 30` |
| List attachments | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" attachments KEY_UPPER` |
| Download all attachments | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" download KEY_UPPER --all --dir "<ATTACHMENTS_DIR>"` |
| Extra fields | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" raw KEY_UPPER --fields status,fixVersions,issuelinks,labels,priority,components` |
| Related tickets | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" search "JQL"` |
| Preview a comment (never auto-post) | `python "${CLAUDE_PLUGIN_ROOT}/skills/jira/jira.py" comment KEY_UPPER -m "text" --dry-run` |

## Reusing the confluence skill (`confluence.py`) — both modes

Feature specs, design docs, and API contracts often live on an on-prem wiki page
(`wiki.ith.intel.com`) — linked from a ticket, or pasted straight into a
free-text request. All Confluence I/O goes through the sibling `confluence`
skill's `confluence.py` (read-only). `CONFLUENCE_TOKEN` must be set in the
environment (a Confluence PAT); if a call fails with the token error, note it and
continue (a linked wiki page is helpful context, **not** a hard blocker). Never
echo/write the token anywhere. Save everything into `<ARTIFACT_DIR>/confluence`
(use `CONFLUENCE_DIR_ARG` from the bootstrap output).

| Intent | Command |
|--------|---------|
| Read a linked page | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" view "<wiki-url>"` |
| List its attached docs | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" attachments "<wiki-url>"` |
| Download the docs into the artifact folder | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" download "<wiki-url>" --all --dir "<CONFLUENCE_DIR_ARG>"` |
| See the links inside the page (to follow) | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" links "<wiki-url>"` |
| Find a page by topic | `python "${CLAUDE_PLUGIN_ROOT}/skills/confluence/confluence.py" search 'title ~ "Design"'` |

`view`/`attachments`/`links`/`search` also accept a bare pageId or `SPACE/Title`.
`links` splits results into external URLs, internal wiki pages (follow with
`view "SPACE/Title"`), and attachment references.

## Workflow

### Phase A — Understand the request (main loop)

1. **Classify & bootstrap.** Run:
   `python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_bootstrap.py" "<the argument>"`
   (quote it — a free-text description has spaces). It prints `INPUT_MODE`,
   `ARTIFACT_DIR`, `ATTACHMENTS_DIR`, `DOWNLOAD_DIR_ARG`, `CONFLUENCE_DIR`,
   `CONFLUENCE_DIR_ARG`, `NOTES`, `REPORT`. Use `DOWNLOAD_DIR_ARG` as the `--dir`
   for Jira downloads and `CONFLUENCE_DIR_ARG` for `confluence.py download`.
   Branch the rest of Phase A on `INPUT_MODE`.

2. **Ticket mode — fetch the ticket.**
   - `jira.py view KEY_UPPER` — summary, type, priority, status, components,
     labels, assignee, reporter, description, and any **acceptance criteria**.
   - `jira.py comments KEY_UPPER --limit 30` — design discussion, clarifications,
     scope changes (widen only if the ticket is comment-heavy and the tail looks
     relevant).
   - `jira.py attachments KEY_UPPER`, then
     `jira.py download KEY_UPPER --all --dir "<DOWNLOAD_DIR_ARG>"` — specs,
     mockups, API contracts, sample data. Record the *inventory* (filename /
     size / type) in NOTES.md, but **do not read the attachment contents in the
     main loop** — that's the Stage-1 artifact-reader agent's job. Reading large
     files into the main-loop context here would re-bill them through Phases
     B/C/D.

3. **Free-text mode — capture the request.** Make **no** `jira.py` calls.
   Capture the user's description **verbatim** into NOTES.md's *Feature request*
   section — it is the source of truth. Expand implicit requirements into
   explicit, testable bullets in *Requirements & acceptance criteria*. If the
   request is too thin to design against, do not guess and do not ask mid-run:
   note the gaps — they are raised for the user at the Phase B.5 gate.

4. **Linked wiki pages (both modes).** Scan the ticket description/comments (or
   the free-text request) for `wiki.ith.intel.com` URLs. For each, using the
   `confluence` skill: `confluence.py view "<wiki-url>"` — read the page;
   `confluence.py attachments …`, then `confluence.py download "<wiki-url>" --all
   --dir "<CONFLUENCE_DIR_ARG>"` — pull its docs into `<ARTIFACT_DIR>/confluence`;
   `confluence.py links "<wiki-url>"` — note further links worth following. This
   is best-effort: if `CONFLUENCE_TOKEN` is unset or a fetch fails, record it and
   move on — a wiki page is context, not a hard blocker.

5. **Write NOTES.md.** Fill the *Feature request*, *Requirements & acceptance
   criteria*, *Description*, and (ticket mode) *Comments digest* / *Attachments*
   sections; fill *Linked Confluence pages* with each fetched page (title, URL, a
   short digest, docs saved under `confluence/`). Never include the token or any
   secret.

   **Digest, don't hoard.** NOTES.md is the durable context; once material is
   digested into it, **rely on NOTES.md and stop re-reading the raw sources** in
   Phases B/C/D. In particular, a full `confluence.py view` dump can be large —
   capture a short digest into the *Linked Confluence pages* section rather than
   pasting the whole page into the main-loop context; re-read the raw page only
   if a specific detail is later needed.

### Phase B — Design (an ultracode-style Workflow)

6. Call the **`Workflow`** tool with a script that fans out to understand the
   codebase, (conditionally) adversarially reviews the design, then synthesizes
   a concrete implementation plan. This is the intended way to run the deep,
   multi-agent design autonomously. Hand the workflow: the input mode, `KEY_UPPER`
   (ticket mode) or a one-line request title (free-text), the `ARTIFACT_DIR`, the
   **path** to `NOTES.md`, the attachment inventory, and the repo facts below.

   **Token discipline — this is the skill's single biggest cost, so spend
   deliberately.** Three rules govern every `agent()` call:

   - **Pass NOTES.md by path, never by content.** Hand agents the *path* to
     `NOTES.md` (and the `ARTIFACT_DIR`); each agent `Read`s only the sections it
     needs. Inlining the full NOTES.md into every agent prompt replicates it N
     times — don't.
   - **Effort-routing is the primary lever.** Default every finder to
     `opts.effort: 'medium'`. Escalate to `'high'`/`'xhigh'` **only** for a
     `cross_component_feature` class (see Stage 0) and for the synthesis stage.
     The Stage-0 triage-gate itself runs at `effort: 'low'`. Do not put `'xhigh'`
     on every agent.
   - **Do NOT override the model — omit `opts.model`.** Agents inherit the
     session model (Opus), which is what we want. *Do not set
     `opts.model: 'sonnet'`* on workflow agents: in this environment a model
     override collides with extended-thinking and the agent dies with a `400 …
     Expected 'thinking' … but found 'text'` error. The savings come from the
     triage-gate, the adaptive fan-out, `effort: 'medium'`, and passing NOTES.md
     by path — not from downgrading the model.

   Suggested shape:

   - **Stage 0 — triage-gate (one agent, `effort: 'low'`, no model override).**
     Before any fan-out, spend one cheap agent to *size the design*. It `Read`s
     the NOTES.md digest and returns a small structured result (use `schema`):
     `feature_class` ∈ `{new_in_existing_component, new_component,
     cross_component_feature}`, the recommended finder set, and a boolean
     `needs_design_review`. **Everything below scales from this verdict** — do
     not launch the full fan-out unconditionally.
   - **Stage 1 — fan-out finders (parallel), all read-only.** Launch **only the
     finders the Stage-0 verdict called for**. Each agent is blind to the others
     and attacks the problem from a different angle:
     - **requirements analyzer** — turn the acceptance criteria / request into a
       list of concrete, testable behaviors and edge cases (feeds the test plan);
     - **component / integration-point mapper** — from the request + Jira
       `components`/labels + any named modules → the `vcs/<comp>` component(s)
       that own the change, the existing interfaces/APIs to call or extend, the
       `.parts` dependencies involved, and each candidate's `CODEOWNERS`;
     - **existing-pattern finder** — locate an already-implemented feature that
       is structurally similar, so the new code **reuses** the repo's patterns
       and utilities instead of reinventing them (Read/Grep the candidate
       component's `include/ src/`);
     - **test-surface analyzer** — determine how the target component is tested
       today: **gtest** (deps on `googletest.test`/`googletest.test_main`,
       `#include <gtest/gtest.h>`), **TUT** (dep on `tut`, `#include
       <tut2/tutx.h>`), Python (`.py` sources in a `utest/` registered via
       `env.UnitTest`), or only via the integration suites. Report where a new
       test file would live and how `utest.parts` registers sources (most glob
       `Pattern(src_dir='utest', includes=['*.cpp'])`). **This seeds Phase D.**
     - **interface/dependency analyzer** — the public headers under
       `include/<comp>/` and `.parts` files that must change, and who else
       depends on them (so an API addition doesn't break consumers).
   - **Stage 2 — adversarial design review (parallel, *conditional*).** Spawn
     reviewers **only when** Stage 0 set `needs_design_review` **or** the finders
     disagree on the target component / approach. Each reviewer (`effort:
     'medium'`, no model override) critiques the emerging design for: a missed
     acceptance criterion, a breaking public-API change, a simpler alternative
     that reuses more existing code, or a cheaper test strategy. Keep only design
     points that survive scrutiny.
   - **Stage 3 — synthesis (single agent).** Return a structured plan: the target
     component(s) + owners, the files to create/modify with interface sketches,
     the `.parts`/dependency edits, **the test strategy** (unit-preferred: exact
     framework + test-file path + each acceptance criterion → a named test case;
     integration fallback with its caveat), the build/test command, and any open
     questions / ambiguities the request left unresolved.

   **Scale by the Stage-0 class:**

   | Stage-0 class | Finders (Stage 1) | Design review (Stage 2) | Effort (model: inherit) |
   |---|---|---|---|
   | `new_in_existing_component` | mapper + existing-pattern + test-surface | skip unless conflict | medium; synth high |
   | `new_component` | mapper + existing-pattern + test-surface + interface/dep | 1 reviewer | medium; synth high |
   | `cross_component_feature` | all 5 finders | full panel (majority-of-3) | high-xhigh; synth xhigh |

   The workflow is **read-only**: no edits, no builds inside it. It returns one
   consolidated plan, which becomes the Phase B.5 gate material.

   Repo facts to pass to the workflow:
   - Repo root `${CLAUDE_PROJECT_DIR}` (the VTune build checkout); ~155 components
     under `vcs/<comp>/` (each with `include/ src/ utest/ *.parts CODEOWNERS`).
     `vcs/` is git-ignored, so plain Grep/Glob may skip it — agents should use
     `git -C … grep`, `ls`, and direct `Read` under `vcs/<comp>/`.
   - Unit tests use **gtest** (newer components) or **TUT** (legacy C++ core);
     some components register Python unit tests. `env.UnitTest(target, source)`
     in a component's `utest.parts` (usually glob-based) is the builder.
   - Integration/functional tests are a separate Python system:
     `vcs/perfdqtests1/perfdqtests/modules/*.py` (functional, `class
     Test(TestPiersol)`) and `vcs/perftests1` (application/perf) — run via
     generated launcher scripts, **not** scons.
   - Build/test commands (Phase C/D, main loop only):
     `python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 <target>`
     where `<target>` is `<comp>::` (build), `run_utest::<comp>::` (build+run unit
     tests), or `run_bench::<comp>::`.

### Phase B.5 — Plan-approval gate (STOP for the user)

7. Record the returned plan into **IMPLEMENTATION.md** (feature summary,
   requirements, design, test strategy, open items). Then present the user a
   **compact** summary — not the whole doc:
   - the target component(s) + the files you will create/modify;
   - the approach in a few bullets (what existing code/interfaces it reuses);
   - the **test plan** (unit vs integration, framework, which acceptance
     criteria each test covers);
   - any **open questions / ambiguities** — especially in free-text mode or an
     underspecified ticket. This gate is *the* place to resolve them, since
     mid-run `AskUserQuestion` is disabled.

   Then **end the turn** and wait. **Write no source code before the user
   approves.** State plainly that you are waiting for approval (or corrections)
   of the plan before implementing. This is a gate, not an `AskUserQuestion`
   prompt — just stop.

### Phase C — Implement (main loop, only after the user approves the plan)

8. Apply the user's approval/corrections to the plan in IMPLEMENTATION.md.

9. **Snapshot** the pre-change state of the target component before the first
   edit: `git -C "${CLAUDE_PROJECT_DIR}" diff -- vcs/<comp>`
   into the artifact dir.

10. **Implement** — `Edit`/`Write` source under `vcs/<comp>/`, following the
    patterns the Phase-B existing-pattern finder identified (reuse the repo's
    interfaces and utilities; match the surrounding code's style). Edit the
    component's `.parts` / public headers only as the plan calls for.

11. **Build the component** to confirm it compiles:
    `python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 <comp>::`
    (run from the repo root). Capture the log into `<ARTIFACT_DIR>/build/`. Do
    **not** run `... all` — propose it instead (see permission model).

12. Save the implementation diff to `<ARTIFACT_DIR>/impl.diff`. **List changed
    components** from
    `git -C "${CLAUDE_PROJECT_DIR}" status --short` and record
    them. If **more than one** component changed, recommend a full `all` build
    (confirm-first) rather than per-component builds.

### Phase D — Test (best-effort, unit-preferred)

13. Add tests for the feature, driving off the Phase-B test-surface analysis.
    This is **best-effort**: aim for a unit test; missing or red tests do **not**
    block completion — record them in *Open items* and *Test results* of
    IMPLEMENTATION.md.

    **Unit test (preferred).** Add a test file under `vcs/<comp>/utest/` matching
    the component's existing framework:
    - **gtest** — `#include <gtest/gtest.h>`; `TEST(Suite, Case){ … }` or
      `TEST_F(Fixture, Case){ … }` with `ASSERT_*`/`EXPECT_*`. No hand-written
      `main()` when the component depends on `googletest.test_main` (there is a
      stub `main.cpp`).
    - **TUT** — `#include <tut2/tutx.h>`; a
      `namespace { typedef tut::test_group<fixture_t> factory; … factory tf("name"); }`
      block plus `template<> template<> void object::test<N>() { … ENSURE(cond); }`
      bodies. `main.cpp` already exists.
    - **Python** — add a `test_*.py` under the component's Python `utest/` if that
      is how it is tested.

    Registration is usually automatic (most `utest.parts` glob
    `Pattern(src_dir='utest', includes=['*.cpp'])`); only edit the `.parts` if the
    test needs a new dependency, include dir, or `data_src`. Map each acceptance
    criterion to a named test case. Build + run:
    `python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 run_utest::<comp>::`
    Read the result from `logs/run_utest.<comp>.*.log` (TUT prints
    `Test summary: ok: N`; gtest prints `[ PASSED ]`). Iterate
    fix→rebuild→rerun until green, within reason.

    **Integration test (fallback).** Only if the behavior genuinely can't be
    unit-tested (needs a running product / live collection / GUI). Add a
    `vcs/perfdqtests1/perfdqtests/modules/<name>.py` — `class Test(TestPiersol)`
    with an `execute()` method, set `OWNER`, add `is_test_applicable()` for
    OS-conditional cases, use `@log()` decorators and `self.tools.*` helpers;
    follow `vcs/perfdqtests1/docs/rules.txt` and `best_known_methods.txt`. Or add
    a `vcs/perftests1` workload. **Flag clearly** in the report that these run via
    the generated launcher scripts (`run_integration_*` / `run_perftests`), **not
    scons**, and likely **cannot be executed in this local dev checkout** (they
    need a product install + hardware) — so the integration test is authored
    best-effort and its execution is deferred/manual.

14. **Finalize IMPLEMENTATION.md**: Feature summary · Requirements & acceptance
    criteria · Design · Test strategy · Implementation applied (changed
    components, `impl.diff` ref, build result) · Test results (tests added,
    pass/fail with the log reference, or why untestable) · Open items · PR status
    · Permissions I still lack for full autonomy.

15. Print a summary + the **mandatory closing report** (below).

All intermediate notes, logs, diffs, and the doc live inside
`jira_artifacts/<folder>/`. Source edits go only under `vcs/<comp>/`.

### Phase E — Copilot PR review loop (main loop; only after a PR is open)

Runs **only** when a draft PR has been opened for the feature and Copilot was
requested as a reviewer (see [Branch & PR conventions](#branch--pr-conventions)
— itself a deferred/confirm-first action). Its purpose: let GitHub Copilot review
the PR, then have you (the agent) triage Copilot's comments and give the user a
reasoned assessment — but **never touch the code until the user has evaluated
that assessment.** The loop is: **wait → assess → STOP for the user → (on
approval) fix + push + re-request → wait …**

Copilot review lands under `<ARTIFACT_DIR>/pr_review/`. Use
`PR_REVIEW_DIR = <ARTIFACT_DIR>/pr_review` as the `--dir` for the helper.

**E1 — Wait for Copilot (granted, read-only).** Copilot reviews asynchronously
(usually a few minutes). Poll for it with the helper — one bounded, blocking call
so there is no shell control-flow:

```
python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_pr_review.py" wait <PR#> \
  --dir "<PR_REVIEW_DIR>" --timeout 480
```

- It prints `STATUS=ready|not_ready|no_pr`. On `ready` it also prints
  `REVIEW_ID`, `REVIEW_STATE`, `COMMENT_COUNT`, and `JSON_PATH` (the review
  dumped to `pr_review/copilot_review.json`).
- On `not_ready` (Copilot hasn't posted within the window) **do not spin** — stop
  the turn and tell the user Copilot hasn't reviewed yet and that they can
  re-invoke to wait again.
- Pass `--proxy http://proxy-iind.intel.com:912` if `gh api` times out.
- On a later loop round, pass `--after-review-id <previous REVIEW_ID>` so you only
  accept Copilot's **new** review (the one produced after your push).

**E2 — Assess each comment (granted, read-only), then STOP.** Read
`copilot_review.json`. For **each** Copilot comment, form your own engineering
judgement by looking at the actual code (`Read`/`Grep` the cited `path`:`line`,
cross-checked against your Phase-B design). Then present the user a compact
**assessment table** — one row per comment:

| # | File:line | Copilot's point (short) | Your verdict | Why | Severity | Proposed action |
|---|---|---|---|---|---|---|

- **Your verdict** — `agree` / `partly` / `disagree`, in your own voice. You are
  the reviewer of the reviewer: say plainly where Copilot is right, where it is a
  false positive, and where it's stylistic noise not worth acting on.
- **Why** — one line of concrete reasoning tied to the code.
- **Proposed action** — `fix` (with a one-line sketch), `skip` (with the reason),
  or `needs-input`.
- Summarize the review body (`review.state`, any overall summary) above the table,
  and save your assessment to `pr_review/ASSESSMENT.md`.

Then **end the turn.** Just state that you are waiting for the user's evaluation
of the assessment before changing any code, and that fixing is deferred until
they respond. Do **not** edit source, push, or re-request a review in this step.
This is the gate.

**E3 — Apply the user's decision, then loop (main loop).** Only after the user
replies (which comments to address, which to skip, any extra direction): apply
the agreed fixes under `vcs/<comp>/` exactly as in Phase C (snapshot the pre-fix
diff, save the updated `impl.diff`), re-run the affected component's
`run_utest::<comp>::` to confirm, then update the PR:

1. Commit + push the fix commits to the **same PR branch** (per
   [Branch & PR conventions](#branch--pr-conventions); never a new branch, never
   `master`). Committing/pushing is deferred/confirm-first — the same grant that
   opened the PR covers the follow-up pushes.
2. Resolve the addressed comments:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_pr_review.py" resolve <PR#> <comment-id-1> [<comment-id-2> ...]
   ```
   Pass the IDs of **only the comments you actually fixed** (from
   `copilot_review.json`'s `comments[].id`). Skipped comments are **not**
   resolved.
3. Re-request a fresh Copilot review:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/skills/implement-feature/if_pr_review.py" rerequest <PR#>
   ```
4. Go back to **E1**, passing `--after-review-id <the REVIEW_ID you just
   handled>`, and repeat assess → STOP → fix.

The loop ends when any of: the user says stop; Copilot's new review has
`COMMENT_COUNT=0` (and/or `REVIEW_STATE=APPROVED`); or the user promotes the PR
out of draft themselves. Record each round in `pr_review/ASSESSMENT.md` and
reflect the final state in IMPLEMENTATION.md.

**Gate discipline (the whole point of this phase):** you may *wait for* and *read*
Copilot's review freely, and you may *assess* it freely — but the first code
change of every round happens **only after** the user has evaluated that round's
assessment. Never pre-apply "obvious" fixes before the gate.

## Staged permission model

You may freely do anything in the **granted set**. For an **ungranted** action, do
NOT prompt and do NOT attempt it silently — propose it and record it in the closing
report with the exact grant needed.

**GRANTED (silent):**
- *Understand* — Jira reads (ticket mode) + `download` into the artifact folder;
  read-only Confluence via `confluence.py`
  (`view`/`attachments`/`links`/`search`/`raw`) and `confluence.py download` into
  `<ARTIFACT_DIR>/confluence`; repo-wide `Read`/`Grep`/`Glob`; read-only git
  (`log`/`show`/`diff`/`status`/`blame`, `git grep`); `ls`/`du`/`file`/`unzip
  -l`/`unzip -v`; `if_bootstrap.py`; the Phase-B `Workflow`;
  `if_pr_review.py wait`/`rerequest`/`resolve` (poll for / re-request / resolve
  comments on a Copilot PR review — all granted so Phase E can run without
  prompts).
- *Build / test one component* — `scons ... <comp>::` (build), `run_utest::<comp>::`
  (build + run unit tests), `run_bench::<comp>::`.
- *Implement* — `Edit`/`Write` source under `vcs/<comp>/` (and notes/doc/tests
  under the artifact dir), enabling an implement → build → test loop. **Only after
  the user approves the Phase B.5 plan.**

**NOT GRANTED — deferred / confirm-first** (list these, with the exact pattern, in
the closing report when relevant):

| Deferred action | Exact grant to add to `~/.claude/settings.json` `allow` |
|---|---|
| Full-repo build | `Bash(python "C:/Python38/Scripts/scons.py" --dev-layout --build-config=release --debug=time -j 32 all)` |
| Create a feature branch | `Bash(git -C "${CLAUDE_PROJECT_DIR}" checkout -b uid_<uid>_*)` — name it per [Branch & PR conventions](#branch--pr-conventions) |
| Commit / push / open PR | `Bash(git ... commit *)`, `Bash(git ... push *)`, `Bash(gh pr create *)` — branch name + PR title/body per [Branch & PR conventions](#branch--pr-conventions); PR is always opened `--draft` |
| Add Copilot as reviewer on the PR | `Bash(gh api -X POST *requested_reviewers*)` (the REST path; also used by `if_pr_review.py rerequest` each loop round) |
| Push fix commits after a Copilot round (Phase E) | same `Bash(git ... commit *)` / `Bash(git ... push *)` grant — pushed to the existing PR branch, never `master`, and **only after the user has evaluated that round's assessment** |
| Post a Jira comment (ticket mode) | already gated by an `ask` rule — **stays confirm-first**, never auto-posts |

Beyond the granted set, the two **gates** are end-of-turn stops, not violations:
the **plan-approval gate** (Phase B.5, before any code) and the **Copilot gate**
(Phase E, before each fix round).

## Branch & PR conventions

Whenever you create a branch, commit, or open a PR against `vtune_build` (only ever
once that action is in the granted set — branching/PR is deferred by default),
follow these exactly. They exist so the change is easy for the component's
engineers to review.

**Never attribute the change to an AI/agent.** Commit messages, PR titles/bodies,
and Jira comments must read as ordinary engineering work — no `Co-Authored-By`
agent/Claude trailer, no "generated by", "with Claude Code", "AI-assisted", or
similar. Write in the author's voice; the change stands on its evidence, not its
tooling.

**Branch name** — always:

```
uid_<uid>_<small_logic_description>[_<jira_ticket_number>]
```

- `<uid>` — **your** identity, not a hard-coded one. Prefer the `ROOTCAUSE_UID`
  env var if set; otherwise derive it from the local git identity
  (`git -C "${CLAUDE_PROJECT_DIR}" config user.email` → the part before `@`, or
  `git config user.name` lowercased, `_`-separated). Never hard-code another
  person's uid.
- `<small_logic_description>` — a few words, lowercase, `_`-separated, describing
  the change; e.g. `add_json_report_export`.
- `<jira_ticket_number>` — the full ticket key, e.g. `VASP-32909`. **Ticket mode
  only** — in free-text mode omit the suffix entirely.
- Examples (for uid `jdoe`): `uid_jdoe_add_json_report_export_VASP-32909` (ticket) /
  `uid_jdoe_add_json_report_export` (free-text).

**PR title** — a short, plain description of what the feature does:

```
VASP-32909: add JSON export format to the reporter    (ticket mode)
add JSON export format to the reporter                (free-text mode)
```

In **ticket mode** start with the Jira number; in **free-text mode** drop the
prefix.

**PR body** — dry and to the point; two short sections, identical in both modes:

1. **Why** — the reason for the feature (in ticket mode, reference the ticket; in
   free-text mode, the user's stated need).
2. **What** — what the change does, at the level of the files/behavior touched,
   and what tests were added.

Keep both tight — bullets over prose. Link to `IMPLEMENTATION.md`/artifacts if
more detail is warranted.

**Opening the PR** — always open it as a **draft** and always request a review from
**GitHub Copilot**. Never open a non-draft PR from this skill; the author promotes
it out of draft themselves after review.

Two steps (do NOT rely on `gh pr create --reviewer` for Copilot — in this org that
flag uses the GraphQL `requestReviewsByLogin` path, which fails with `Could not
resolve user with login 'copilot-pull-request-reviewer[bot]'`):

1. Create the draft PR (base is the repo's default branch, usually `master`):
   ```
   gh pr create --draft --base master \
     --title "<title per above>" \
     --body "<Why / What per above>"
   ```
2. Add Copilot via the **REST** `requested_reviewers` endpoint (this works):
   ```
   gh api -X POST "repos/<owner>/<repo>/pulls/<PR#>/requested_reviewers" \
     -f "reviewers[]=copilot-pull-request-reviewer[bot]"
   ```
   Confirm the response's `requested_reviewers` contains `{"login":"Copilot",
   "type":"Bot"}`. If it does not, report it in the closing report rather than
   leaving the PR silently reviewer-less.

- `--draft` is mandatory. Adding Copilot as a reviewer is mandatory.
- Once the PR is open and Copilot is requested, proceed to
  [Phase E](#phase-e--copilot-pr-review-loop-main-loop-only-after-a-pr-is-open):
  wait for Copilot's review, assess it, then stop for the user's evaluation before
  any further code change.
- **Network:** GitHub's API (`api.github.com`) may be reachable only via the
  corporate proxy even when `git push` works directly. If `gh`/`gh api` times out,
  prefix with `export https_proxy=http://proxy-iind.intel.com:912
  http_proxy=http://proxy-iind.intel.com:912` and retry.

**Link the PR back in Jira (ticket mode only)** — once the draft PR is open, post a
Jira comment on the ticket with exactly this format (nothing more):

```
PR with fix: <PR URL>
```

This is still a Jira comment, so the confirm-first rule below applies: preview with
`jira.py comment ... --dry-run`, then post only on explicit approval. In free-text
mode there is no ticket — skip this step.

## Safety rules

- **The plan gate is mandatory — never write source before the user approves.**
  You may investigate, design, and write NOTES.md / IMPLEMENTATION.md freely, but
  the first source edit under `vcs/<comp>/` happens **only after** the user has
  approved the Phase B.5 plan. Present the plan, then stop the turn and wait.
- **Never auto-post a Jira comment.** If you propose one, show the exact final text
  and use `jira.py comment ... --dry-run` for preview. A real post is confirm-first
  (a global `ask` rule enforces this regardless) — never post without explicit user
  approval, even at max autonomy.
- **Never** echo/log/write `JIRA_TOKEN`, `CONFLUENCE_TOKEN`, or any secret into
  files, the report, or output.
- **Write only** under `jira_artifacts/<folder>/` (notes/doc/logs/diffs/tests
  staging) and `vcs/<comp>/` (source + tests). Never elsewhere.
- **Never run a full `all` build** without explicit confirmation — build only the
  affected component. Prefer proposing `all` when the change touched *many*
  components.
- **Never** commit, push, create branches, or open PRs unless that action is in the
  granted set (deferred by default). The repo is shared, checked out on `master` —
  do not switch branches. Snapshot the pre-change diff before editing so the change
  is reviewable. **When branching/PR *is* granted**, name the branch and write the
  PR strictly per [Branch & PR conventions](#branch--pr-conventions) — never push
  directly to `master`.
- **Phase E gate — never change code before the user evaluates.** You may wait for,
  read, and assess Copilot's PR review freely, but the first source edit, commit,
  push, or re-request of each review round happens **only after** the user replies
  to that round's assessment. Present the assessment, then stop the turn and wait.

## Closing report (mandatory — every run)

End with:
- The input mode, where artifacts landed (`<ARTIFACT_DIR>`), the target
  component(s), whether the feature was implemented, and the build/test result
  (tests added + pass/fail, or why untestable).
- If you stopped at the **plan-approval gate**, say so plainly: "waiting for your
  approval of the plan before writing any code," and point at IMPLEMENTATION.md.
- If a PR was opened and Phase E ran: the PR URL, whether Copilot has reviewed, and
  where you are in the loop (awaiting the user's evaluation of round N /
  fixed-and-re-requested / Copilot approved / stopped). If you are at that gate, say
  so plainly: "waiting for your evaluation before touching the code."
- A **"Permissions I still lack for full autonomy"** section: each deferred action
  you would have taken next + the exact tool pattern that would unlock it (from the
  table above). If nothing was deferred, say "none — the granted set was
  sufficient."
