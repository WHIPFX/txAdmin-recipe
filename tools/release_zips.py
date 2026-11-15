"""Builds a WHIP resource's release zips and attaches them to its GitHub release.

txAdmin gives each download 150 seconds and that can't be changed from a recipe. download_github fetches a zip that
GitHub builds on request, which is often too slow for the big resources. So for those, the recipes download zips
attached to the release instead: they come from GitHub's CDN, and each is at most --max-mb MiB so it finishes in time on
a slow connection. Each zip holds a share of the resource's files, at their paths in the resource; the recipe unzips
them all into the resource's folder.

GitHub runs it: each such resource has templates/release-zips.yml as .github/workflows/release-zips.yml, which runs
.github/workflows/release-zips.yml here when it publishes a release. The run's summary has the tasks to paste into both
recipes. It also runs on a computer with git and gh (logged in with write access to the repo):

    py tools/release_zips.py whip_media_props v1.0.0            # build, upload, and print the recipe's tasks
    py tools/release_zips.py whip_media_props v1.0.0 --no-upload
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ORG = "WHIPFX"
# Already compressed: storing them as they are makes the zips build and unzip faster, for the same size
STORED = {".webp", ".webm", ".png", ".jpg", ".jpeg", ".ogg", ".mp3", ".mp4", ".zip", ".awc"}
FIXED_TIME = (2000, 1, 1, 0, 0, 0)  # every entry gets this time, so the same files give the same zips


def run(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def split(files, max_bytes):
    """Fills each part with files in path order, so folders stay together, until the next file would pass max_bytes."""
    parts, part, size = [], [], 0
    for path, sha, file_size in files:
        file_size += 2 * len(path.encode()) + 100  # its two headers in the zip
        if part and size + file_size > max_bytes:
            parts.append(part)
            part, size = [], 0
        part.append((path, sha))
        size += file_size
    if part:
        parts.append(part)
    return parts


class Blobs:
    """Reads files as git stores them, not as a checkout would write them: on Windows git can turn LF into CRLF."""

    def __init__(self, repo_dir):
        self.git = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo_dir,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, sha):
        self.git.stdin.write(sha.encode() + b"\n")
        self.git.stdin.flush()
        header = self.git.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise RuntimeError(f"git cat-file: {header}")
        data = self.git.stdout.read(int(header[2]))
        self.git.stdout.read(1)  # the newline after each object
        return data

    def close(self):
        self.git.stdin.close()
        self.git.wait()


def write_zip(zip_path, blobs, files):
    with zipfile.ZipFile(zip_path, "w") as z:
        for path, sha in files:
            info = zipfile.ZipInfo(path, FIXED_TIME)
            info.external_attr = 0o644 << 16
            stored = os.path.splitext(path)[1].lower() in STORED
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            z.writestr(info, blobs.read(sha), compresslevel=None if stored else 9)


def recipe_tasks(repo, tag, dest, names):
    lines = []
    for name in names:
        lines += [
            "  - action: download_file",
            f"    url: https://github.com/{ORG}/{repo}/releases/download/{tag}/{name}",
            f"    path: ./tmp/{name}",
            "",
            "  - action: unzip",
            f"    src: ./tmp/{name}",
            f"    dest: {dest}",
            "",
        ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("repo", help="the repo's name in WHIPFX, e.g. whip_media_props")
    p.add_argument("tag", help="the release's tag, e.g. v1.0.0")
    p.add_argument("--max-mb", type=float, default=40, help="largest zip, in MiB (default 40: 150 s at about 2.3 Mbit/s)")
    p.add_argument("--dest", help="the resource's folder in the recipe (default ./resources/[assets]/[media]/<repo>)")
    p.add_argument("--out", help="where to keep the zips (default a temporary folder, removed afterwards)")
    p.add_argument("--no-upload", action="store_true", help="only build the zips")
    args = p.parse_args()

    repo, tag = args.repo, args.tag
    dest = args.dest or f"./resources/[assets]/[media]/{repo}"
    work = tempfile.mkdtemp(prefix=f"{repo}-")
    try:
        src = os.path.join(work, "src")
        print(f"Cloning {ORG}/{repo} at {tag}...", file=sys.stderr)
        run("git", "clone", "--quiet", "--no-checkout", "--depth", "1", "--branch", tag, f"https://github.com/{ORG}/{repo}.git", src)

        # The files GitHub's own zip of the tag would have: everything the tag tracks
        listing = run("git", "ls-tree", "-r", "-l", "-z", "HEAD", cwd=src).split("\0")
        files = []
        for entry in filter(None, listing):
            meta, path = entry.split("\t", 1)
            _, kind, sha, size = meta.split()
            if kind == "blob":
                files.append((path, sha, int(size)))

        out = args.out or os.path.join(work, "zips")
        os.makedirs(out, exist_ok=True)
        names = []
        blobs = Blobs(src)
        for i, part in enumerate(split(files, int(args.max_mb * 1024 * 1024)), 1):
            name = f"{repo}-{i}.zip"
            write_zip(os.path.join(out, name), blobs, part)
            names.append(name)
            print(f"  {name}: {len(part)} files, {os.path.getsize(os.path.join(out, name)) / 1048576:.1f} MiB",
                  file=sys.stderr)
        blobs.close()

        if not args.no_upload:
            full = f"{ORG}/{repo}"
            print(f"Uploading to {full} {tag}...", file=sys.stderr)
            run("gh", "release", "upload", tag, *[os.path.join(out, n) for n in names], "--clobber", "-R", full)
            # A rebuild with fewer parts leaves the old higher-numbered parts: remove them
            assets = run("gh", "release", "view", tag, "-R", full, "--json", "assets", "--jq", ".assets[].name")
            for asset in assets.split():
                if asset.startswith(f"{repo}-") and asset.endswith(".zip") and asset not in names:
                    run("gh", "release", "delete-asset", tag, asset, "-R", full, "--yes")
                    print(f"  removed the old {asset}", file=sys.stderr)

        print(recipe_tasks(repo, tag, dest, names))
    finally:
        if not args.out:
            # git makes its objects read-only, which stops Windows removing them
            shutil.rmtree(work, onexc=lambda fn, path, _: (os.chmod(path, 0o666), fn(path)))


if __name__ == "__main__":
    main()
