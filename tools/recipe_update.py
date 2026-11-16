"""Moves both WHIP txAdmin recipes to each WHIPFX resource's latest release, and pushes that to main.

GitHub runs it (.github/workflows/recipe-update.yml) when a resource publishes a release: each resource repo has
templates/recipes.yml as .github/workflows/recipes.yml, which starts that workflow here. Every resource is checked, not
only the one that published, so the next run catches up one that was missed.

  - A resource the recipes get with download_github (src: WHIPFX/<repo>) gets the new release's tag as its `ref:`.
  - A resource the recipes get as its release's zips gets the new release's zips once its Release zips run has
    attached them (tools/release_zips.py). This waits for that run.

Each resource that moves is one commit, "chore: the recipes install <repo> <version>", pushed to main when it is ready.
bob74_ipl (not WHIPFX's) and oxmysql (WHIPFX's fork of upstream, its version in the comment above it) stay by hand.

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
from release_zips import ORG, recipe_tasks  # noqa: E402  (its tasks, so both scripts write them alike)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPES = ("whip-legacy.yaml", "whip-enhanced.yaml")
WAIT_SECONDS = 45 * 60  # the longest a release's zips are waited for (a Release zips run takes about 10 minutes)
POLL_SECONDS = 30

SRC = re.compile(r"^    src: WHIPFX/([\w.-]+)$")
REF = re.compile(r"^    ref: (\S+)$")
ZIP_URL = re.compile(r"^    url: https://github\.com/WHIPFX/([\w.-]+)/releases/download/([^/\s]+)/([\w.-]+)$")


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


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8", newline="") as f:
        return f.read().split("\n")


def write(name, lines):
    with open(os.path.join(ROOT, name), "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines))


def is_part(repo, asset):
    """Is this asset one of the resource's release zips (<repo>-<n>.zip)?"""
    return re.fullmatch(rf"{re.escape(repo)}-\d+\.zip", asset) is not None


def resources(lines):
    """Every WHIPFX resource in one recipe: {repo: ('ref', tag) or ('zips', tag)}."""
    found = {}
    for i, line in enumerate(lines):
        m = SRC.match(line)
        if m:
            for field in lines[i + 1:]:
                if not field.startswith("    "):
                    break
                r = REF.match(field)
                if r:
                    found[m.group(1)] = ("ref", r.group(1))
            continue
        m = ZIP_URL.match(line)
        if m and is_part(m.group(1), m.group(3)):
            repo, tag = m.group(1), m.group(2)
            if found.get(repo, ("zips", tag)) != ("zips", tag):
                raise Refused(f"{repo}: its zips come from more than one release")
            found[repo] = ("zips", tag)
    return found


def move_ref(lines, repo, tag):
    for i, line in enumerate(lines):
        if line == f"    src: {ORG}/{repo}":
            j = i + 1
            while j < len(lines) and lines[j].startswith("    "):
                if REF.match(lines[j]):
                    lines[j] = f"    ref: {tag}"
                    return
                j += 1
    raise Refused(f"{repo}: no ref: under its src:")


def move_zips(lines, repo, tag, names):
    """Swaps the resource's run of download_file / unzip tasks for the new release's, and the version in a comment
    just above them that names the resource (whip_gunny v2.0.1, as its release's zips ...)."""
    def ours(i):
        m = ZIP_URL.match(lines[i + 1]) if i + 1 < len(lines) else None
        return m is not None and m.group(1) == repo and is_part(repo, m.group(3))

    starts = [i for i, line in enumerate(lines) if line == "  - action: download_file" and ours(i)]
    if not starts:
        raise Refused(f"{repo}: no release zips to replace")
    first, end = starts[0], starts[-1] + 1
    dest = None
    # Every task from the first download to the last unzip must be the resource's own, so nothing else is replaced.
    while end < len(lines) and (lines[end].startswith("    ") or lines[end] == ""
                                or lines[end] in ("  - action: unzip", "  - action: download_file")):
        line = lines[end]
        if line == "  - action: download_file" and not ours(end):
            break
        if line.startswith("    src: ") and not is_part(repo, os.path.basename(line[len("    src: "):])):
            raise Refused(f"{repo}: an unzip among its tasks is not its own")
        if line.startswith("    dest: "):
            dest = dest or line[len("    dest: "):]
        end += 1
    if not dest:
        raise Refused(f"{repo}: its unzips have no dest:")
    while end > first and lines[end - 1] == "":  # keep the blank lines after the run as they were
        end -= 1
    new = recipe_tasks(repo, tag, dest, names).split("\n")
    while new and new[-1] == "":
        new.pop()
    lines[first:end] = new
    for k in range(max(0, first - 3), first):
        lines[k] = re.sub(rf"^(  # {re.escape(repo)} )v?\d\S*(, as its release's zips)", rf"\g<1>{tag}\g<2>", lines[k])


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
    if git("diff", "--quiet", "--", *RECIPES, check=False) == 0:
        print(f"{repo}: already at {tag}")
        return
    version = tag[1:] if re.match(r"v\d", tag) else tag
    git("commit", "-q", "-m", f"chore: the recipes install {repo} {version}", "--", *RECIPES)
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

    found = [resources(read(name)) for name in RECIPES]
    if found[0] != found[1]:
        sys.exit(f"The recipes don't list the same releases: {RECIPES[0]} {found[0]} and {RECIPES[1]} {found[1]}")

    todo = []
    for repo, (kind, tag) in sorted(found[0].items()):
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
                for name in RECIPES:
                    lines = read(name)
                    if kind == "ref":
                        move_ref(lines, repo, new)
                    else:
                        move_zips(lines, repo, new, names)
                    write(name, lines)
                land(repo, new, not args.no_push)
            except Refused as e:
                print(f"::error::{e}")
                git("checkout", "--", *RECIPES, check=False)
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
