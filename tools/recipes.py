"""Makes the WHIP txAdmin recipes from recipe/manifest.toml, so every recipe installs the same list.

    python tools/recipes.py           # write whip-legacy.yaml, whip-enhanced.yaml and their -lite variants
    python tools/recipes.py --check   # exit 1 if a committed recipe differs from what the manifest makes (CI runs this)

The recipes it writes into the repo download from GitHub. tools/cdn.py calls render() with source="cdn" to make the
same recipes with every download from WHIP's CDN, once CI has uploaded and checked the zips there.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "recipe" / "manifest.toml"
# txAdmin gives an unzip 180 seconds. A zip of a few thousand small files unzips in seconds on most machines, but some
# servers have taken longer than that (a slow disk, or antivirus checking each new file). A task's timeoutSeconds raises it.
UNZIP_TIMEOUT = 1800
WIDTH = 120  # the recipes' comment lines are wrapped at this width
MEDIA_SCRIPTS = ("get-whip-media.ps1", "get-whip-media.sh")

Variant = Literal["full", "lite"]
Source = Literal["github", "cdn"]


@dataclass(frozen=True)
class Resource:
    name: str
    repo: str
    tag: str
    dest: str
    github: Literal["source", "release-zips", "release-asset"]
    parts: int = 0
    asset: str = ""
    heavy: bool = False
    note: str = ""


@dataclass(frozen=True)
class Section:
    title: str
    note: str
    resources: tuple[Resource, ...]


@dataclass(frozen=True)
class Platform:
    key: str
    title: str
    name: str
    lite_name: str
    server_cfg: str
    min_fx_version: int = 0
    min_fx_note: str = ""


@dataclass(frozen=True)
class Manifest:
    version: str
    author: str
    max_part_mib: float
    min_mbit: float
    platforms: dict[str, Platform]
    sections: tuple[Section, ...]
    cfg: dict[str, str] = field(default_factory=dict)

    @property
    def resources(self) -> list[Resource]:
        return [r for s in self.sections for r in s.resources]


@dataclass(frozen=True)
class Part:
    """One download: where it comes from, and (for the CDN, which built it) its size and sha256."""

    url: str
    name: str
    size: int = 0
    sha256: str = ""


def load_manifest(path: Path = MANIFEST) -> Manifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rec = data["recipe"]
    platforms = {key: Platform(key=key, **p) for key, p in data["platform"].items()}
    sections = tuple(
        Section(s["title"], s.get("note", ""), tuple(Resource(**r) for r in s["resource"])) for s in data["section"]
    )
    cfg = {key: (path.parent / p.server_cfg).read_text(encoding="utf-8") for key, p in platforms.items()}
    manifest = Manifest(rec["version"], rec["author"], float(rec["max_part_mib"]), float(rec["min_mbit"]),
                        platforms, sections, cfg)
    validate(manifest)
    return manifest


def validate(m: Manifest) -> None:
    seen: set[str] = set()
    for r in m.resources:
        if r.name in seen:
            raise ValueError(f"{r.name} is in the manifest twice")
        seen.add(r.name)
        if r.github == "release-zips" and r.parts < 1:
            raise ValueError(f"{r.name}: release-zips needs parts")
        if r.github == "release-asset" and not r.asset:
            raise ValueError(f"{r.name}: release-asset needs asset")
        if not r.dest.startswith("./resources/") or ".." in r.dest:
            raise ValueError(f"{r.name}: dest must be under ./resources/")


def github_parts(r: Resource) -> list[Part]:
    """The files the GitHub recipes download for a resource that isn't a download_github."""
    base = f"https://github.com/{r.repo}/releases/download/{r.tag}"
    if r.github == "release-asset":
        return [Part(f"{base}/{r.asset}", r.asset)]
    return [Part(f"{base}/{r.name}-{i}.zip", f"{r.name}-{i}.zip") for i in range(1, r.parts + 1)]


def cdn_parts(r: Resource, index: dict, base: str) -> list[Part]:
    """The files the CDN recipes download for a resource, from its build index (tools/cdn.py)."""
    if index.get("resource") != r.name or index.get("tag") != r.tag:
        raise ValueError(f"the build index for {r.name} {r.tag} is for {index.get('resource')} {index.get('tag')}")
    return [Part(f"{base.rstrip('/')}/{p['key']}", p["name"], p["size"], p["sha256"]) for p in index["parts"]]


def rule(title: str) -> str:
    head = f"  # ---- {title} "
    return head + "-" * max(4, WIDTH - len(head))


def comment(text: str, indent: str = "") -> list[str]:
    return [f"{indent}# {line}".rstrip() for line in textwrap.wrap(text, WIDTH - len(indent) - 2)] or [f"{indent}#"]


def unzip_task(src: str, dest: str, timeout: bool) -> list[str]:
    lines = ["  - action: unzip", f"    src: {src}", f"    dest: {dest}"]
    return lines + ([f"    timeoutSeconds: {UNZIP_TIMEOUT}"] if timeout else [])


def resource_tasks(r: Resource, parts: list[Part] | None, source: Source) -> list[str]:
    """parts is None for a download_github (only from GitHub)."""
    out: list[str] = []
    if r.note:
        out.append(f"  # {r.note.format(tag=r.tag)}")
    if parts is None:
        return out + ["  - action: download_github", f"    src: {r.repo}", f"    ref: {r.tag}", f"    dest: {r.dest}", ""]
    for p in parts:
        out += ["  - action: download_file", f"    url: {p.url}", f"    path: ./tmp/{p.name}", ""]
        # The GitHub recipes have always unzipped oxmysql's small release build with txAdmin's default time
        out += unzip_task(f"./tmp/{p.name}", r.dest, timeout=source == "cdn" or r.github != "release-asset") + [""]
    return out


def block(text: str, indent: str = "      ") -> list[str]:
    return [f"{indent}{line}".rstrip() for line in text.rstrip("\n").split("\n")]


def human_mb(n: int) -> str:
    return f"{round(n / 1_000_000):,} MB"


def heavy_names(m: Manifest) -> str:
    names = [r.name for r in m.resources if r.heavy]
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)


def media_items(m: Manifest, source: Source, indexes: dict[str, dict] | None, base: str) -> list[tuple[Part, str]]:
    items: list[tuple[Part, str]] = []
    for r in m.resources:
        if not r.heavy:
            continue
        parts = cdn_parts(r, indexes[r.name], base) if source == "cdn" and indexes else github_parts(r)
        items += [(p, r.dest.removeprefix("./")) for p in parts]
    return items


def ps1_script(m: Manifest, items: list[tuple[Part, str]], where: str) -> str:
    rows = "\n".join(f"    ,@('{p.url}', '{dest}', {p.size}, '{p.sha256}')" for p, dest in items)
    head = "\n".join(comment(
        f"Adds WHIP's heavy packs ({heavy_names(m)}) to a server installed with a WHIP lite recipe. It downloads "
        f"{len(items)} zips from {where}, checks each one, and unzips it into its resource's folder. If it stops, "
        "run it again: it keeps the zips it already has."))
    return f"""{head}
# Run it in this folder (the one with server.cfg), with the server stopped:
#   powershell -ExecutionPolicy Bypass -File get-whip-media.ps1
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = $PSScriptRoot
$tmp = [IO.Path]::Combine($root, 'tmp-whip-media')
[IO.Directory]::CreateDirectory($tmp) | Out-Null
# url, folder, size and sha256 (0 and '' when not known)
$parts = @(
{rows}
)

function Test-Part($file, $size, $sha) {{
    if (-not [IO.File]::Exists($file)) {{ return $false }}
    if ($size -gt 0 -and (New-Object IO.FileInfo $file).Length -ne $size) {{ return $false }}
    if ($sha -and (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $sha) {{ return $false }}
    return $true
}}

$web = New-Object Net.WebClient
foreach ($p in $parts) {{
    $url, $dest, $size, $sha = $p
    $file = [IO.Path]::Combine($tmp, $url.Split('/')[-1])
    for ($try = 1; -not (Test-Part $file $size $sha); $try++) {{
        if ($try -gt 3) {{ throw "Could not download $url" }}
        if ($try -gt 1) {{ Write-Host '  It failed: trying again'; Start-Sleep -Seconds 5 }}
        Write-Host "Downloading $url"
        try {{
            $web.DownloadFile($url, "$file.part")
            [IO.File]::Delete($file)
            [IO.File]::Move("$file.part", $file)
        }} catch {{ Write-Host "  $($_.Exception.Message)" }}
    }}
    $target = [IO.Path]::GetFullPath([IO.Path]::Combine($root, $dest))
    Write-Host "Unzipping into $dest"
    $zip = [IO.Compression.ZipFile]::OpenRead($file)
    try {{
        foreach ($entry in $zip.Entries) {{
            $out = [IO.Path]::GetFullPath([IO.Path]::Combine($target, $entry.FullName))
            if (-not $out.StartsWith($target + [IO.Path]::DirectorySeparatorChar)) {{ throw "Unsafe path in the zip: $($entry.FullName)" }}
            if ($entry.FullName.EndsWith('/')) {{ continue }}
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($out)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $out, $true)
        }}
    }} finally {{ $zip.Dispose() }}
}}
[IO.Directory]::Delete($tmp, $true)
Write-Host 'Done. Start the server: the ensure [assets] line in server.cfg starts the new packs.'
"""


def sh_script(m: Manifest, items: list[tuple[Part, str]], where: str) -> str:
    rows = "\n".join(f"get '{p.url}' '{dest}' {p.size} '{p.sha256}'" for p, dest in items)
    head = "\n".join(comment(
        f"Adds WHIP's heavy packs ({heavy_names(m)}) to a server installed with a WHIP lite recipe. It downloads "
        f"{len(items)} zips from {where}, checks each one, and unzips it into its resource's folder. If it stops, "
        "run it again: it keeps the zips it already has. It needs curl and unzip."))
    return f"""#!/bin/sh
{head}
# Run it in this folder (the one with server.cfg), with the server stopped:  sh get-whip-media.sh
set -eu
cd "$(dirname "$0")"
for tool in curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || {{ echo "get-whip-media.sh needs $tool" >&2; exit 1; }}
done
tmp='tmp-whip-media'
mkdir -p "$tmp"

sha256() {{
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}}
# file size sha256: is the file there and whole? (size 0 and an empty sha256 when not known)
ok() {{
  [ -f "$1" ] || return 1
  if [ "$2" -gt 0 ] && [ "$(wc -c < "$1" | tr -d ' ')" -ne "$2" ]; then return 1; fi
  if [ -n "$3" ] && [ "$(sha256 "$1")" != "$3" ]; then return 1; fi
  return 0
}}
# url folder size sha256
get() {{
  file="$tmp/${{1##*/}}"
  try=1
  until ok "$file" "$3" "$4"; do
    if [ "$try" -gt 3 ]; then echo "Could not download $1" >&2; exit 1; fi
    if [ "$try" -gt 1 ]; then echo "  It failed: trying again"; sleep 5; fi
    echo "Downloading $1"
    curl -fsSL --retry 3 -o "$file.part" "$1" && mv -f "$file.part" "$file" || true
    try=$((try + 1))
  done
  echo "Unzipping into $2"
  mkdir -p "$2"
  unzip -o -q "$file" -d "$2"
}}

{rows}
rm -rf "$tmp"
echo "Done. Start the server: the ensure [assets] line in server.cfg starts the new packs."
"""


def render(m: Manifest, platform: str, variant: Variant, source: Source = "github",
           indexes: dict[str, dict] | None = None, base: str = "", release_id: str = "") -> str:
    """One recipe. For source="cdn", indexes has each resource's build index and base is the CDN's URL."""
    p = m.platforms[platform]
    if source == "cdn" and (not base or indexes is None):
        raise ValueError("a CDN recipe needs the CDN's base URL and every resource's build index")
    host = base.split("://", 1)[-1].split("/", 1)[0] if base else "GitHub"
    where = f"WHIP's CDN ({host})" if source == "cdn" else "GitHub"

    tasks: list[str] = []
    total = 0
    github_calls = 0
    for section in m.sections:
        body: list[str] = []
        for r in section.resources:
            if variant == "lite" and r.heavy:
                continue
            if source == "cdn":
                parts: list[Part] | None = cdn_parts(r, indexes[r.name], base)  # type: ignore[index]
                total += sum(x.size for x in parts)
            else:
                parts = None if r.github == "source" else github_parts(r)
                github_calls += parts is None
            body += resource_tasks(r, parts, source)
        if body:
            tasks += [rule(section.title)] + ([f"  # {section.note}"] if section.note else []) + body
    tasks += ["  - action: remove_path", "    path: ./tmp", ""]

    if variant == "lite":
        items = media_items(m, source, indexes, base)
        tasks += [rule("The heavy packs: a separate download, once the install is done"),
                  *comment("Run get-whip-media.ps1 (Windows) or get-whip-media.sh (Linux) in the server folder to "
                           f"add {heavy_names(m)}.", "  ")]
        for name, script in zip(MEDIA_SCRIPTS, (ps1_script(m, items, where), sh_script(m, items, where))):
            tasks += ["  - action: write_file", f"    file: ./{name}", "    data: |", *block(script), ""]

    tasks += [rule("The database: empty. WHIP makes every table itself on its first start; there is no SQL file."),
              "  - action: connect_database", "",
              rule("server.cfg: txAdmin fills in the {{...}} values when the deploy finishes"),
              "  - action: write_file", "    file: ./server.cfg", "    data: |", *block(m.cfg[platform])]

    other = "whip-enhanced" if platform == "legacy" else "whip-legacy"
    head = [f"# WHIP for a {p.title} server: a txAdmin recipe (https://github.com/citizenfx/txAdmin/blob/master/docs/recipe.md).",
            "# On txAdmin's setup page choose \"Remote URL Template\" and paste this file's URL. It makes a whole new server.",
            "#"]
    head += comment("Made by tools/recipes.py from recipe/manifest.toml, which lists every resource at the tag it "
                    "installs (a release on its own repository): change the manifest, not this file. "
                    f"{other}.yaml has the same list. GitHub moves the manifest to a WHIPFX resource's latest release "
                    "when it publishes one (.github/workflows/recipe-update.yml); bob74_ipl and oxmysql are by hand.")
    head.append("#")
    if source == "github":
        head += comment("txAdmin gives each download 150 seconds, and a recipe can't change that. download_github gets "
                        "a zip that GitHub builds on request, often too slowly for the big resources. So those come as "
                        "zips attached to their releases, from GitHub's CDN, each 40 MiB or less (about 2.3 Mbit/s in "
                        "150 seconds). GitHub makes them when one of those repos publishes a release "
                        f"(tools/release_zips.py), then puts their tasks here. Their unzips get {UNZIP_TIMEOUT} seconds: some servers (a slow disk, "
                        "or antivirus checking each new file) take more than the 180 txAdmin allows by default to write "
                        "a few thousand small files.")
        head += comment(f"Each download_github is one call to GitHub's API (60 an hour without a token): {github_calls} "
                        "here. A server that can't reach GitHub can use the same recipe from WHIP's CDN: see the README.")
    else:
        head += comment(f"Every file comes from {where}, not from GitHub: release {release_id}. CI zipped each resource "
                        "at its tag, downloaded every zip back through the CDN to check it, and only then published this "
                        f"file (tools/cdn.py). Each zip is {m.max_part_mib:g} MiB or less, so it finishes within "
                        f"txAdmin's 150 seconds per download at {m.min_mbit:g} Mbit/s. Unzips get {UNZIP_TIMEOUT} seconds.")
    if variant == "lite":
        head.append("#")
        head += comment(f"The lite recipe: everything except the heavy packs ({heavy_names(m)}), so it downloads and "
                        "unzips far less. It writes get-whip-media.ps1 and get-whip-media.sh into the server folder: run "
                        "one once the install is done to add the heavy packs, outside txAdmin's time limits. Until then "
                        "the server has none of them.")

    # txAdmin recommends a description under 256 characters: tests check every variant
    size = f"Downloads {human_mb(total)} from WHIP's CDN." if source == "cdn" else (
        "Downloads about 830 MB from GitHub." if variant == "full" else "")
    lite = " Lite: no media packs or Mirror Park homes." if variant == "lite" else ""
    desc = ("WHIP, a complete server base: characters, inventory, money and banks, jobs, cars, homes, and an admin panel "
            f"in game (/whip). For a {p.title} server.{lite} {size}").strip()
    meta = ["$engine: 3", "$onesync: on"]
    if p.min_fx_version:
        meta.append(f"$minFxVersion: {p.min_fx_version}   # {p.min_fx_note}")
    meta += [f"name: {p.lite_name if variant == 'lite' else p.name}", f"version: {m.version}", f"author: {m.author}",
             "description: >-", *("  " + line for line in textwrap.wrap(desc, WIDTH - 4)), "", "tasks:"]
    return "\n".join(head + [""] + meta + tasks) + "\n"


def file_name(platform: str, variant: Variant) -> str:
    return f"whip-{platform}{'-lite' if variant == 'lite' else ''}.yaml"


def render_all(m: Manifest, source: Source = "github", **kw) -> dict[str, str]:
    return {file_name(p, v): render(m, p, v, source, **kw) for p in m.platforms for v in ("full", "lite")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only compare: exit 1 if a recipe is out of date")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--out", type=Path, default=ROOT, help="where the recipes are (default: the repo)")
    args = ap.parse_args(argv)

    stale = []
    for name, text in render_all(load_manifest(args.manifest)).items():
        path = args.out / name
        current = path.read_bytes().decode("utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(name)
        if not args.check:
            path.write_bytes(text.encode("utf-8"))  # bytes: LF on every OS, as txAdmin expects
            print(f"wrote {name}")
    if args.check and stale:
        print(f"Out of date with recipe/manifest.toml: {', '.join(stale)}. Run `python tools/recipes.py` and commit.",
              file=sys.stderr)
        return 1
    if args.check:
        print("The recipes match recipe/manifest.toml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
