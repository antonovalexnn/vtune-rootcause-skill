#!/usr/bin/env python
"""GitHub Copilot PR-review helper for the `rootcause` skill.

After the skill opens a draft PR and requests a review from GitHub Copilot
(the `copilot-pull-request-reviewer[bot]`), Copilot reviews asynchronously --
usually within a few minutes, sometimes longer. This helper does the fiddly,
deterministic parts so the agent doesn't shell out control-flow of its own
(which would trigger extra permission prompts):

  wait   <PR#>  -- block-poll the GitHub API until Copilot submits a *new*
                   review (or a timeout), then dump it structured to disk.
  rerequest <PR#> -- re-request a Copilot review after pushing fix commits,
                   so the assess -> gate -> fix loop can go another round.
  resolve <PR#> <comment-id> [<comment-id> ...] -- reply on each PR review
                   comment (default "Done", override with -m/--message) and
                   mark its enclosing review *thread* resolved. Resolution is a
                   property of PullRequestReviewThread, not the comment: the
                   helper fetches the PR's threads via GraphQL, maps each REST
                   comment id to its thread (thread comment databaseId ==
                   REST comment id), skips threads already resolved, and calls
                   resolveReviewThread with the thread node id.

All network I/O goes through `gh api` (subprocess); nothing is reimplemented.
The GitHub REST endpoints used are read-only for `wait`:
  repos/<owner>/<repo>/pulls/<PR>/reviews    (submitted reviews)
  repos/<owner>/<repo>/pulls/<PR>/comments   (inline review comments)
and, for `rerequest`, the same POST requested_reviewers endpoint the skill
already uses to add Copilot in the first place.

  rc_pr_review.py wait <PR#> [--repo-slug owner/repo] [--dir DIR]
       [--timeout 480] [--poll 30] [--after-review-id N] [--proxy URL]
  rc_pr_review.py rerequest <PR#> [--repo-slug owner/repo] [--proxy URL]
  rc_pr_review.py resolve <PR#> <comment-id> [<comment-id> ...]
       [--repo-slug owner/repo] [--proxy URL]

`wait` output (parse-friendly, one key=value per line):
  STATUS=ready|not_ready|no_pr
  PR=<n>
  REPO_SLUG=<owner/repo>
  REVIEW_ID=<id>              # newest Copilot review (only when ready)
  REVIEW_STATE=<COMMENTED|APPROVED|CHANGES_REQUESTED>
  REVIEW_SUBMITTED_AT=<iso>
  BODY_PRESENT=yes|no         # whether the review carries a summary body
  COMMENT_COUNT=<n>           # inline comments tied to that review
  JSON_PATH=<abs path to copilot_review.json>   # only when ready
  WAITED_SECONDS=<n>

The JSON dump (copilot_review.json) is the source of truth the agent reads to
assess each comment:
  {"pr":N, "repo_slug":"o/r", "review":{id,state,body,submitted_at,html_url},
   "comments":[{id, path, line, body, diff_hunk, html_url}, ...]}

Exit 0 whenever the poll completed cleanly (STATUS=ready or not_ready), 2 on a
usage error, 1 on an unrecoverable API/`gh` failure (message names the cause;
the corporate proxy may be needed -- pass --proxy http://proxy-iind.intel.com:912).
"""

import argparse
import json
import os
import subprocess
import sys
import time

# The VTune build repo. Resolution order (matches rc_bootstrap.py): explicit
# --repo > VTUNE_BUILD_DIR env > CLAUDE_PROJECT_DIR (the Claude Code workspace
# root, normally the vtune_build checkout) > the current working directory.
DEFAULT_REPO = (
    os.environ.get("VTUNE_BUILD_DIR")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or os.getcwd())

# Copilot's reviewer bot. In the *reviews* list its user.login surfaces as
# "Copilot" (type "Bot"); the requested-reviewer login is the [bot] form. Match
# generously: any login containing "copilot" with a Bot type.
COPILOT_LOGIN = "copilot-pull-request-reviewer[bot]"


def _die(msg, code=2):
    sys.stderr.write("ERROR: " + msg + "\n")
    raise SystemExit(code)


def _gh(args, proxy=None):
    """Run a `gh` command; return (rc, stdout_text, stderr_text).

    When --proxy is given, export it into the child env for both http and https
    so `gh api` can reach api.github.com through the corporate proxy (which the
    skill notes may be required even when plain `git push` works directly).
    """
    env = dict(os.environ)
    if proxy:
        env["https_proxy"] = proxy
        env["http_proxy"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
    proc = subprocess.run(
        ["gh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode, out, err


def _gh_json(args, proxy=None):
    """Run a `gh api` call expected to return JSON; parse and return it."""
    rc, out, err = _gh(args, proxy=proxy)
    if rc != 0:
        _die("gh {0} failed (rc={1}): {2}".format(
            " ".join(args), rc, err.strip() or out.strip()), code=1)
    try:
        return json.loads(out) if out.strip() else []
    except ValueError as exc:
        _die("could not parse JSON from `gh {0}`: {1}".format(
            " ".join(args), exc), code=1)


def _resolve_repo_slug(repo, explicit, proxy):
    """owner/repo for the PR API. Prefer --repo-slug, else the origin remote."""
    if explicit:
        return explicit
    rc, out, err = _gh(
        ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        proxy=proxy)
    if rc == 0 and out.strip():
        return out.strip()
    # Fall back to parsing the git origin URL (no network).
    proc = subprocess.run(
        ["git", "-C", repo, "remote", "get-url", "origin"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 0:
        url = proc.stdout.decode("utf-8", "replace").strip()
        slug = url.rstrip("/")
        if slug.endswith(".git"):
            slug = slug[:-4]
        # https://github.com/OWNER/REPO or git@github.com:OWNER/REPO
        for sep in ("github.com/", "github.com:"):
            if sep in slug:
                return slug.split(sep, 1)[1]
    _die("could not resolve owner/repo; pass --repo-slug owner/repo.")


def _resolve_pr(repo, pr_arg, slug, proxy):
    """PR number: explicit positional, else the PR for the current branch."""
    if pr_arg:
        return int(pr_arg)
    rc, out, err = _gh(
        ["pr", "view", "--repo", slug, "--json", "number", "-q", ".number"],
        proxy=proxy)
    if rc == 0 and out.strip():
        try:
            return int(out.strip())
        except ValueError:
            pass
    return None


def _is_copilot(user):
    if not isinstance(user, dict):
        return False
    login = (user.get("login") or "").lower()
    return "copilot" in login


def _fetch_copilot_review(slug, pr, proxy, after_id):
    """Return (review_dict, [inline_comment_dicts]) for the newest Copilot
    review with id > after_id, or (None, []) if none yet."""
    reviews = _gh_json(
        ["api", "--paginate",
         "repos/{0}/pulls/{1}/reviews".format(slug, pr)],
        proxy=proxy)
    cop = [r for r in reviews
           if _is_copilot(r.get("user")) and int(r.get("id", 0)) > after_id]
    if not cop:
        return None, []
    # Newest by id (== newest submission for the same bot).
    review = max(cop, key=lambda r: int(r.get("id", 0)))
    rid = int(review["id"])
    comments = _gh_json(
        ["api", "--paginate",
         "repos/{0}/pulls/{1}/comments".format(slug, pr)],
        proxy=proxy)
    mine = [c for c in comments
            if int(c.get("pull_request_review_id") or 0) == rid]
    return review, mine


def _trim_comment(c):
    return {
        "id": c.get("id"),
        "path": c.get("path"),
        "line": c.get("line") if c.get("line") is not None else c.get("original_line"),
        "body": c.get("body"),
        "diff_hunk": c.get("diff_hunk"),
        "html_url": c.get("html_url"),
    }


def cmd_wait(args):
    slug = _resolve_repo_slug(args.repo, args.repo_slug, args.proxy)
    pr = _resolve_pr(args.repo, args.pr, slug, args.proxy)
    if pr is None:
        print("STATUS=no_pr")
        print("REPO_SLUG={0}".format(slug))
        return
    after_id = int(args.after_review_id or 0)

    start = time.time()
    deadline = start + max(0, args.timeout)
    review, comments = None, []
    while True:
        review, comments = _fetch_copilot_review(slug, pr, args.proxy, after_id)
        if review is not None:
            break
        if time.time() >= deadline:
            break
        time.sleep(max(1, args.poll))
    waited = int(time.time() - start)

    print("PR={0}".format(pr))
    print("REPO_SLUG={0}".format(slug))
    if review is None:
        print("STATUS=not_ready")
        print("WAITED_SECONDS={0}".format(waited))
        return

    out_dir = args.dir or os.path.join(
        DEFAULT_REPO, "jira_artifacts")
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "copilot_review.json")
    payload = {
        "pr": pr,
        "repo_slug": slug,
        "review": {
            "id": review.get("id"),
            "state": review.get("state"),
            "body": review.get("body"),
            "submitted_at": review.get("submitted_at"),
            "html_url": review.get("html_url"),
        },
        "comments": [_trim_comment(c) for c in comments],
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    print("STATUS=ready")
    print("REVIEW_ID={0}".format(review.get("id")))
    print("REVIEW_STATE={0}".format(review.get("state")))
    print("REVIEW_SUBMITTED_AT={0}".format(review.get("submitted_at")))
    print("BODY_PRESENT={0}".format(
        "yes" if (review.get("body") or "").strip() else "no"))
    print("COMMENT_COUNT={0}".format(len(comments)))
    print("JSON_PATH={0}".format(json_path))
    print("WAITED_SECONDS={0}".format(waited))


def cmd_rerequest(args):
    slug = _resolve_repo_slug(args.repo, args.repo_slug, args.proxy)
    pr = _resolve_pr(args.repo, args.pr, slug, args.proxy)
    if pr is None:
        _die("no PR resolved to re-request a review on.", code=1)
    rc, out, err = _gh(
        ["api", "-X", "POST",
         "repos/{0}/pulls/{1}/requested_reviewers".format(slug, pr),
         "-f", "reviewers[]={0}".format(COPILOT_LOGIN)],
        proxy=args.proxy)
    if rc != 0:
        _die("re-request failed: {0}".format(err.strip() or out.strip()), code=1)
    ok = False
    try:
        data = json.loads(out) if out.strip() else {}
        ok = any(_is_copilot(u) for u in data.get("requested_reviewers", []))
    except ValueError:
        pass
    print("PR={0}".format(pr))
    print("REPO_SLUG={0}".format(slug))
    print("RE_REQUESTED={0}".format("yes" if ok else "unconfirmed"))


def _split_slug(slug):
    if "/" not in slug:
        _die("repo slug must be owner/repo, got: {0}".format(slug))
    owner, name = slug.split("/", 1)
    return owner, name


def _fetch_review_threads(slug, pr, proxy):
    """Return the PR's review threads (paginated):
      [{"thread_id": <graphql node id>, "is_resolved": bool,
        "comment_db_ids": set([<REST comment id>, ...])}, ...]

    Resolution state and the thread node id both live on
    PullRequestReviewThread -- NOT on PullRequestReviewComment (a common
    mistake: `isResolved` does not exist on the comment type, and
    resolveReviewThread needs the *thread* node id, not a comment node id).
    Each thread's comments expose `databaseId`, which is the same integer as
    the REST comment id the agent passes in from copilot_review.json.
    """
    owner, name = _split_slug(slug)
    query = (
        "query($owner:String!,$name:String!,$pr:Int!,$cursor:String){"
        "repository(owner:$owner,name:$name){"
        "pullRequest(number:$pr){"
        "reviewThreads(first:100,after:$cursor){"
        "pageInfo{hasNextPage endCursor}"
        "nodes{id isResolved comments(first:100){nodes{databaseId}}}"
        "}}}}"
    )
    threads = []
    cursor = None
    while True:
        call = ["api", "graphql",
                "-f", "query={0}".format(query),
                "-f", "owner={0}".format(owner),
                "-f", "name={0}".format(name),
                "-F", "pr={0}".format(pr)]
        if cursor:
            call += ["-f", "cursor={0}".format(cursor)]
        data = _gh_json(call, proxy=proxy)
        if isinstance(data, dict) and data.get("errors"):
            _die("GraphQL errors fetching review threads: {0}".format(
                json.dumps(data["errors"])[:400]), code=1)
        try:
            rt = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (KeyError, TypeError):
            _die("unexpected GraphQL response fetching review threads: {0}".format(
                json.dumps(data)[:400]), code=1)
        for node in rt.get("nodes", []):
            ids = set()
            for c in node.get("comments", {}).get("nodes", []):
                db = c.get("databaseId")
                if db is not None:
                    ids.add(int(db))
            threads.append({
                "thread_id": node.get("id"),
                "is_resolved": bool(node.get("isResolved")),
                "comment_db_ids": ids,
            })
        page = rt.get("pageInfo", {}) or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
        else:
            break
    return threads


def cmd_resolve(args):
    """Resolve PR review comments by replying (default "Done") and marking the
    enclosing review thread resolved.

    Only acts on threads that are not already resolved. Resolution is a
    property of the review *thread*: this fetches the PR's threads via GraphQL,
    maps each requested REST comment id to its thread (via the thread
    comments' databaseId), replies on the comment, then calls
    resolveReviewThread with the *thread* node id.
    """
    slug = _resolve_repo_slug(args.repo, args.repo_slug, args.proxy)
    pr = _resolve_pr(args.repo, args.pr, slug, args.proxy)
    if pr is None:
        _die("no PR resolved to resolve comments on.", code=1)

    comment_ids = [int(cid) for cid in args.comment_ids]
    if not comment_ids:
        _die("no comment IDs provided.", code=2)

    message = args.message if getattr(args, "message", None) else "Done"

    threads = _fetch_review_threads(slug, pr, args.proxy)
    # Map REST comment id -> its enclosing thread.
    by_comment = {}
    for t in threads:
        for db in t["comment_db_ids"]:
            by_comment[db] = t

    resolved_count = 0
    skipped_count = 0
    failed = []

    for cid in comment_ids:
        thread = by_comment.get(cid)
        if thread is None:
            failed.append((cid, "no review thread found for this comment id on PR #{0}".format(pr)))
            continue
        if thread["is_resolved"]:
            skipped_count += 1
            continue

        # Reply on the comment (REST reply to a review comment).
        rc, out, err = _gh(
            ["api", "-X", "POST",
             "repos/{0}/pulls/{1}/comments/{2}/replies".format(slug, pr, cid),
             "-f", "body={0}".format(message)],
            proxy=args.proxy)
        if rc != 0:
            failed.append((cid, "reply failed: {0}".format(err.strip() or out.strip())))
            continue

        # Resolve the *thread* (not the comment) via GraphQL.
        mutation = ("mutation($threadId:ID!){"
                    "resolveReviewThread(input:{threadId:$threadId}){"
                    "thread{isResolved}}}")
        rc, out, err = _gh(
            ["api", "graphql", "-f", "query={0}".format(mutation),
             "-f", "threadId={0}".format(thread["thread_id"])],
            proxy=args.proxy)
        if rc != 0:
            failed.append((cid, "resolve mutation failed: {0}".format(err.strip() or out.strip())))
            continue

        # Mark locally so sibling comments in the same thread aren't re-processed.
        thread["is_resolved"] = True
        resolved_count += 1

    print("PR={0}".format(pr))
    print("REPO_SLUG={0}".format(slug))
    print("RESOLVED={0}".format(resolved_count))
    print("SKIPPED_ALREADY_RESOLVED={0}".format(skipped_count))
    print("FAILED={0}".format(len(failed)))
    if failed:
        for cid, reason in failed:
            print("  comment {0}: {1}".format(cid, reason))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        prog="rc_pr_review.py",
        description="Wait for / re-request a GitHub Copilot PR review.")
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help="VTune build repo root (default: %(default)s)")
    p.add_argument("--repo-slug", default=None,
                   help="owner/repo for the PR API (default: from origin remote)")
    p.add_argument("--proxy", default=None,
                   help="HTTP(S) proxy for gh api, e.g. http://proxy-iind.intel.com:912")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wait", help="block-poll until Copilot submits a review")
    w.add_argument("pr", nargs="?", help="PR number (default: PR of current branch)")
    w.add_argument("--dir", default=None,
                   help="dir to write copilot_review.json into (the pr_review folder)")
    w.add_argument("--timeout", type=int, default=480,
                   help="max seconds to poll (default: %(default)s; keep < 600 for one Bash call)")
    w.add_argument("--poll", type=int, default=30,
                   help="seconds between polls (default: %(default)s)")
    w.add_argument("--after-review-id", default=None,
                   help="only accept a Copilot review with id greater than this "
                        "(the id from the previous loop round; omit on first wait)")
    w.set_defaults(func=cmd_wait)

    r = sub.add_parser("rerequest", help="re-request a Copilot review after a push")
    r.add_argument("pr", nargs="?", help="PR number (default: PR of current branch)")
    r.set_defaults(func=cmd_rerequest)

    s = sub.add_parser("resolve", help="resolve PR review comments by replying and marking the thread resolved")
    s.add_argument("pr", nargs="?", help="PR number (default: PR of current branch)")
    s.add_argument("comment_ids", nargs="+", help="REST comment IDs (databaseId) to resolve")
    s.add_argument("-m", "--message", default=None,
                   help="reply text posted before resolving (default: 'Done')")
    s.set_defaults(func=cmd_resolve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
