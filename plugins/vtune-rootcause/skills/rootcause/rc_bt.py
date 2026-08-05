#!/usr/bin/env python
"""Build-tracking (BT) helper for the `rootcause` skill.

VTune's automated test system files `[BT]` Jira bugs that carry a "Found in
build" number. That build maps to a git tag in the VTune build repo, and diffing
that tag against the build before it reveals exactly which components were bumped
in that build -- a strong prior for the regressing component.

This helper does the fiddly, deterministic part so the agent doesn't have to
reason it out each run:

  build number  -> git tag  (#<build>+<stream>+jN+, optional ++good suffix)
  found tag     -> baseline tag (predecessor / last-good in the same stream)
  tag pair      -> SConstruct `component_versions` diff (component: old -> new)

Pure stdlib, no network of its own beyond shelling to `git` (which may run
`git fetch --tags` once when the build tag is missing locally). It makes NO
working-tree or branch changes -- only `git fetch --tags`, `git tag`, `git diff`,
all read-only w.r.t. the checked-out branch.

  rc_bt.py <build> [--stream NAME] [--repo DIR]
           [--baseline predecessor|good|both] [--no-fetch]

Output (parse-friendly, one key=value per line, then a markdown table):
  FOUND_BUILD=<n>
  FOUND_TAG=<tag>
  STREAM=<stream>
  BASELINE_KIND=predecessor|good
  BASELINE_TAG=<tag>            # repeated per baseline kind when --baseline both
  FETCHED=yes|no
  BUMPED_COUNT=<n>
  <blank line>
  | component | old | new |
  |---|---|---|
  | ... |

Exit 0 on success, 2 on a usage error, 1 when the build tag can't be resolved
(the message names the exact `git fetch --tags` / stream to try).
"""

import argparse
import os
import re
import subprocess
import sys

# The VTune build repo. Resolution order (matches rc_bootstrap.py): explicit
# --repo > VTUNE_BUILD_DIR env > CLAUDE_PROJECT_DIR (the Claude Code workspace
# root, normally the vtune_build checkout) > the current working directory.
DEFAULT_REPO = (
    os.environ.get("VTUNE_BUILD_DIR")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or os.getcwd())

# Tags look like: #630829+stable+j2+   or   #629788+stable+j2++good
# Structure is #<build>+<stream>+<job>+[+good]. The job token varies across
# streams (j1/j2/p1/p2/phase_test/release_jenkins/ml), so accept any alnum job;
# `+` reliably delimits fields (stream names use `_`/`.`, never `+`). Greedy
# stream + backtracking peels the job token off the tail correctly.
TAG_RE = re.compile(r"^#(\d+)\+(.+)\+([A-Za-z0-9_]+)\+(\+good)?$")

# A component_versions entry on a diff line, e.g.
#     +    'tbb': {'version': '2022.0.2', 'vcs': 'github', ...},
# Match the quoted name that opens a dict literal, then the 'version' inside it.
# Anchoring on the `{` after the name skips the platform-override lines
# (component_versions['x']['version'] = ...) which use a different syntax.
ENTRY_RE = re.compile(
    r"^[+-]\s*'([A-Za-z0-9_]+)'\s*:\s*\{.*?'version'\s*:\s*'([^']+)'")


def _die(msg, code=2):
    sys.stderr.write("ERROR: " + msg + "\n")
    raise SystemExit(code)


def _git(repo, *args):
    """Run a git command in `repo`; return (rc, stdout, stderr) as text."""
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode, out, err


def _list_build_tags(repo, build):
    """All tags for a specific build number, across streams."""
    _, out, _ = _git(repo, "tag", "--list", "#{0}+*".format(build))
    return [t for t in out.split() if t]


def _parse_tag(tag):
    """(build:int, stream:str, job:str, is_good:bool) or None if unparseable."""
    m = TAG_RE.match(tag)
    if not m:
        return None
    return (int(m.group(1)), m.group(2), m.group(3), bool(m.group(4)))


def _one_stream(cands):
    """Collapse candidates that differ only by the ++good marker.

    A build+stream+job is often tagged both plain and `++good`; those name the
    same point, so they are not real ambiguity. Return (tag, stream) preferring
    the plain tag, or (None, distinct_stream_job_count) if genuinely ambiguous.
    """
    by_streamjob = {}  # (stream, job) -> {False: plain_tag, True: good_tag}
    for t, p in cands:
        by_streamjob.setdefault((p[1], p[2]), {})[p[3]] = t
    if len(by_streamjob) != 1:
        return None, len(by_streamjob)
    (streamname, _job), variants = next(iter(by_streamjob.items()))
    return (variants.get(False) or variants.get(True)), streamname


def _pick_found_tag(candidates, stream):
    """Choose the found-build tag from same-build candidates.

    With a stream hint, keep only tags whose stream contains it. Return
    (tag, resolved_stream) or (None, reason) so the caller can report cleanly.
    """
    parsed = [(t, _parse_tag(t)) for t in candidates]
    parsed = [(t, p) for t, p in parsed if p]
    if not parsed:
        return None, "no parseable #<build>+<stream>+job+ tag among: {0}".format(
            ", ".join(candidates))
    if stream:
        matched = [(t, p) for t, p in parsed if stream in p[1]]
        if not matched:
            # Stream hint given but nothing matched -- fall through to the
            # unique-build heuristic below.
            pass
        else:
            # Prefer an exact stream match to narrow multiple substring hits.
            exact = [(t, p) for t, p in matched if p[1] == stream]
            use = exact if exact else matched
            tag, resolved = _one_stream(use)
            if tag is not None:
                return tag, resolved
            return None, (
                "stream {0!r} matches multiple streams; pass a more specific "
                "--stream: {1}".format(
                    stream, ", ".join(t for t, _ in use)))
    tag, resolved = _one_stream(parsed)
    if tag is not None:
        return tag, resolved
    return None, "multiple streams tagged this build; pass --stream: {0}".format(
        ", ".join(t for t, _ in parsed))


def _baseline_tag(repo, build, stream, good_only):
    """Largest same-stream tag with build < `build` (optionally ++good only)."""
    _, out, _ = _git(repo, "tag", "--list", "*{0}*".format(stream))
    best = None  # (build, tag)
    for tag in out.split():
        p = _parse_tag(tag)
        if not p:
            continue
        b, s, _job, is_good = p
        if s != stream or b >= build:
            continue
        if good_only and not is_good:
            continue
        if best is None or b > best[0]:
            best = (b, tag)
    return best[1] if best else None


def _bumped_components(repo, baseline_tag, found_tag):
    """Parse SConstruct component_versions diff -> {name: (old, new)}."""
    rc, out, err = _git(repo, "diff", baseline_tag, found_tag, "--", "SConstruct")
    if rc != 0:
        _die("git diff {0} {1} failed: {2}".format(
            baseline_tag, found_tag, err.strip()), code=1)
    old, new = {}, {}
    for ln in out.splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        m = ENTRY_RE.match(ln)
        if not m:
            continue
        name, ver = m.group(1), m.group(2)
        (new if ln[0] == "+" else old)[name] = ver
    bumped = {}
    for name in sorted(set(old) | set(new)):
        o, n = old.get(name), new.get(name)
        if o != n:
            bumped[name] = (o, n)
    return bumped


def _emit_table(bumped):
    print()
    print("| component | old | new |")
    print("|---|---|---|")
    for name in sorted(bumped):
        o, n = bumped[name]
        print("| {0} | {1} | {2} |".format(name, o or "(none)", n or "(none)"))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        prog="rc_bt.py",
        description="Resolve a VTune build number to its bumped-component list.")
    p.add_argument("build", help="the 'Found in build' number, e.g. 630829")
    p.add_argument("--stream", default="",
                   help="build-stream hint from the '[BT] [<stream>]' summary "
                        "prefix, e.g. stable (disambiguates same-build tags)")
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help="VTune build repo root (default: %(default)s)")
    p.add_argument("--baseline", choices=("predecessor", "good", "both"),
                   default="predecessor",
                   help="baseline to diff against (default: predecessor)")
    p.add_argument("--no-fetch", action="store_true",
                   help="do not run 'git fetch --tags' if the build tag is "
                        "missing locally")
    args = p.parse_args(argv)

    if not re.match(r"^\d+$", args.build.strip()):
        _die("build must be a number, got {0!r}.".format(args.build))
    build = int(args.build.strip())

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        _die("repo dir not found: {0} (set --repo or VTUNE_BUILD_DIR).".format(repo))

    # 1) Locate the found-build tag, fetching once if it's not local yet.
    fetched = False
    candidates = _list_build_tags(repo, build)
    if not candidates and not args.no_fetch:
        rc, _out, err = _git(repo, "fetch", "--tags")
        if rc != 0:
            _die("git fetch --tags failed: {0}".format(err.strip()), code=1)
        fetched = True
        candidates = _list_build_tags(repo, build)
    if not candidates:
        hint = ("run 'git -C \"{0}\" fetch --tags' and retry".format(repo)
                if args.no_fetch else "the build may not be tagged / not pushed")
        _die("no git tag for build {0}; {1}.".format(build, hint), code=1)

    found_tag, resolved = _pick_found_tag(candidates, args.stream)
    if found_tag is None:
        _die(resolved, code=1)
    stream = resolved

    # 2) Choose baseline(s) and diff SConstruct for each.
    kinds = (["predecessor", "good"] if args.baseline == "both"
             else [args.baseline])

    print("FOUND_BUILD={0}".format(build))
    print("FOUND_TAG={0}".format(found_tag))
    print("STREAM={0}".format(stream))
    print("FETCHED={0}".format("yes" if fetched else "no"))

    any_baseline = False
    for kind in kinds:
        base = _baseline_tag(repo, build, stream, good_only=(kind == "good"))
        print("BASELINE_KIND={0}".format(kind))
        if not base:
            print("BASELINE_TAG=(none found before build {0} on stream {1})".format(
                build, stream))
            print("BUMPED_COUNT=0")
            continue
        any_baseline = True
        bumped = _bumped_components(repo, base, found_tag)
        print("BASELINE_TAG={0}".format(base))
        print("BUMPED_COUNT={0}".format(len(bumped)))
        _emit_table(bumped)

    if not any_baseline:
        _die("no baseline tag before build {0} on stream {1}; try "
             "--baseline predecessor or a wider fetch.".format(build, stream),
             code=1)


if __name__ == "__main__":
    main()
