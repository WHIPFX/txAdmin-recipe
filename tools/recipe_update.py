"""Moves the WHIP txAdmin recipes to each WHIPFX resource's latest release, and pushes that to main.

GitHub runs it (.github/workflows/recipe-update.yml) when a resource publishes a release: each resource repo has
templates/recipes.yml as .github/workflows/recipes.yml, which starts that workflow here. Every resource is checked, not
only the one that published, so the next run catches up one that was missed.

The recipes are made from recipe/manifest.toml (tools/recipes.py), so a move changes the resource's entry there and
then writes every recipe from it again:

  - A resource the recipes get with download_github (github = "source") gets the new release's tag.
  - A resource the recipes get as its release's zips (github = "release-zips") gets the new release's tag, and its
    number of zips as parts, once its Release zips run has attached them (tools/release_zips.py). This waits for that
    run.

Each resource that moves is one commit of the manifest and the recipes, "chore: the recipes install <repo> <version>",
pushed to main when it is ready. bob74_ipl (not WHIPFX's) and oxmysql (WHIPFX's fork of upstream, a release build)
stay by hand.

It also runs on a computer with git, and a token for GitHub's API in GH_TOKEN (60 calls an hour without one):

    py tools/recipe_update.py --dry-run     # what would move, changing nothing
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recipes  # noqa: E402  (makes every recipe from the manifest)
from release_zips import ORG  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = "recipe/manifest.toml"
# What a move changes: the manifest, and every recipe tools/recipes.py writes from it
FILES = (MANIFEST, *sorted(recipes.render_all(recipes.load_manifest()).keys()))
WAIT_SECONDS = 45 * 60  # the longest a release's zips are waited for (a Release zips run takes about 10 minutes)
POLL_SECONDS = 30


class Refused(Exception):
    """This resource can't be moved: said, and the run fails once the others are done."""


def api(path):
    req = urllib.request.Request(f"https://api.github.com/{path}", headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "whip-recipe-update",
    })
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def is_part(repo, asset):
    """Is this asset one of the resource's release zips (<repo>-<n>.zip)?"""
    return re.fullmatch(rf"{re.escape(repo)}-\d+\.zip", asset) is not None


def resources(manifest):
    """Every WHIPFX resource the update moves: {repo: ('ref', tag) or ('zips', tag)}."""
    found = {}
    for r in manifest.resources:
        owner, _, repo = r.repo.partition("/")
        if owner != ORG or r.github == "release-asset":
            continue  # bob74_ipl and oxmysql: by hand
        if r.name != repo:
            raise Refused(f"{r.repo}: its name in the manifest is {r.name}, not {repo}")
        found[repo] = ("ref", r.tag) if r.github == "source" else ("zips", r.tag)
    return found


def move(text, repo, tag, parts=None):
    """The manifest's text with the resource's tag (and, for zips, its parts) changed, and nothing else."""
    lines = text.split("\n")
    at = [i for i, line in enumerate(lines) if line == f'repo = "{ORG}/{repo}"']
    if len(at) != 1:
        raise Refused(f"{repo}: it is in {MANIFEST} {len(at)} times")
    first = at[0]
    while first > 0 and not lines[first - 1].startswith("["):
        first -= 1
    last = at[0]
    while last + 1 < len(lines) and not lines[last + 1].startswith("["):
        last += 1
    wanted = {"tag": f'tag = "{tag}"'} | ({"parts": f"parts = {parts}"} if parts is not None else {})
    for i in range(first, last + 1):
        key = lines[i].split(" = ", 1)[0]
        if key in wanted:
            lines[i] = wanted.pop(key)
    if wanted:
        raise Refused(f"{repo}: no {' or '.join(wanted)} in its entry in {MANIFEST}")
    return "\n".join(lines)


def zips_ready(repo, tag):
    """The new release's zip names once its Release zips run has attached them, else None (still to come)."""
    runs = api(f"repos/{ORG}/{repo}/actions/workflows/release-zips.yml/runs?per_page=10")
    if runs is None:
        raise Refused(f"{repo}: it has no Release zips workflow (.github/workflows/release-zips.yml)")
    runs = runs["workflow_runs"]
    if any(r["status"] != "completed" for r in runs):
        return None
    release = api(f"repos/{ORG}/{repo}/releases/tags/{tag}")
    parts = sorted((a["name"] for a in (release or {}).get("assets", [])
                    if a["state"] == "uploaded" and is_part(repo, a["name"])),
                   key=lambda n: int(n.rsplit("-", 1)[1].split(".")[0]))
    if parts:
        if parts != [f"{repo}-{n}.zip" for n in range(1, len(parts) + 1)]:
            raise Refused(f"{repo} {tag}: its zips are not numbered 1 to {len(parts)}: {', '.join(parts)}")
        return parts
    mine = [r for r in runs if r["head_branch"] == tag]
    if mine and mine[0]["conclusion"] != "success":
        raise Refused(f"{repo} {tag}: its Release zips run ended {mine[0]['conclusion']}: {mine[0]['html_url']}")
    return None


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check).returncode


def land(repo, tag, push):
    if git("diff", "--quiet", "--", *FILES, check=False) == 0:
        print(f"{repo}: already at {tag}")
        return
    version = tag[1:] if re.match(r"v\d", tag) else tag
    git("commit", "-q", "-m", f"chore: the recipes install {repo} {version}", "--", *FILES)
    if not push:
        print(f"{repo}: moved to {tag}, committed")
        return
    for _ in range(3):
        if git("push", "-q", "origin", "HEAD:main", check=False) == 0:
            print(f"{repo}: moved to {tag}, pushed")
            return
        git("pull", "-q", "--rebase", "origin", "main")  # someone else pushed first: on top of theirs
    raise Refused(f"{repo}: could not push to main")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="only say what would move")
    p.add_argument("--no-push", action="store_true", help="commit, but don't push")
    args = p.parse_args()

    if recipes.main(["--check"]) != 0:
        sys.exit(f"The recipes don't match {MANIFEST}: fix that first, so a move only changes what it moves")

    todo = []
    for repo, (kind, tag) in sorted(resources(recipes.load_manifest()).items()):
        latest = api(f"repos/{ORG}/{repo}/releases/latest")
        if latest is None:
            print(f"{repo}: no release yet")
        elif latest["tag_name"] == tag:
            print(f"{repo}: {tag}, its latest release")
        else:
            todo.append((repo, kind, tag, latest["tag_name"]))

    refused = []
    deadline = time.monotonic() + WAIT_SECONDS
    while todo:
        waiting = []
        for repo, kind, old, new in todo:
            try:
                names = zips_ready(repo, new) if kind == "zips" else None
                if kind == "zips" and names is None:
                    waiting.append((repo, kind, old, new))
                    continue
                if args.dry_run:
                    print(f"{repo}: would move from {old} to {new}" + (f" ({len(names)} zips)" if names else ""))
                    continue
                path = os.path.join(ROOT, MANIFEST)
                with open(path, encoding="utf-8") as f:
                    text = move(f.read(), repo, new, len(names) if names else None)
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                recipes.main([])  # every recipe, written again from the manifest
                land(repo, new, not args.no_push)
            except Refused as e:
                print(f"::error::{e}")
                git("checkout", "--", *FILES, check=False)
                refused.append(repo)
        todo = waiting
        if not todo:
            break
        if args.dry_run or time.monotonic() > deadline:
            for repo, _, old, new in todo:
                if args.dry_run:
                    print(f"{repo}: would move from {old} to {new} once its Release zips run has attached the zips")
                else:
                    print(f"::error::{repo} {new}: no zips attached after {WAIT_SECONDS // 60} minutes")
                    refused.append(repo)
            break
        print(f"Waiting for the zips of {', '.join(f'{r} {n}' for r, _, _, n in todo)}...")
        time.sleep(POLL_SECONDS)

    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
