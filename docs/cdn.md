# WHIP's CDN

The recipes, and every file they download, are also served from a Cloudflare R2 bucket behind a Cloudflare custom
domain: WHIP's CDN. A server that can't reach GitHub, or reaches it too slowly for txAdmin's 150 seconds per download,
installs from there. The GitHub recipes stay as they are, as the fallback.

CI does all of it (`.github/workflows/cdn.yml`, which runs `tools/cdn.py`). Nobody uploads by hand.

## What's in the bucket

| Key | What it is | Cache-Control |
| --- | --- | --- |
| `releases/<resource>/<tag>/<sha256>/<resource>-<n>.zip` | A resource's files at its tag, in zips of at most `max_part_mib` | a year, immutable |
| `releases/<resource>/<tag>/index.json` | The list of that build's zips, with sizes and sha256s: written last, once every zip is there | a year, immutable |
| `recipes/<release>/whip-*.yaml`, `recipes/<release>/release.json` | One release's four recipes, and what went into them | a year, immutable |
| `recipes/whip-*.yaml` | The stable recipes: a copy of the current release's | 60 seconds |
| `recipes/releases.json` | Every release (id, time, commit, each resource's tag and zips, each recipe's sha256), the current one, and the history of promotes and rollbacks | 60 seconds |

Nothing under `releases/` or `recipes/<release>/` is ever replaced: every write there is create-only
(`If-None-Match: *`, which R2 applies atomically), and an upload fails rather than overwrite a different file. A
zip's key has its sha256 in it, so two builds of the same tag that came out different (another zlib, another OS) can
never meet. Only the stable recipe paths and `releases.json` change, and a rollback is a copy back from
`recipes/<release>/`.

## A release

A push to `main` that changes `recipe/` (or the tools) runs the workflow. Each job runs only if the one before passed.

1. **check**: the unit tests, and the committed recipes are what `recipe/manifest.toml` makes.
2. **build** (on `vars.WHIP_HEAVY_RUNNER`, else `ubuntu-latest`): for each resource whose tag isn't in the bucket yet,
   clone it at the tag, zip every file it tracks (as `tools/release_zips.py` does), and upload the zips, then the
   index. A resource already in the bucket at its tag is not built again, so a release usually builds one resource.
3. **verify**: download every zip of every resource back through the public CDN URL, as txAdmin will, and check:
   HTTP 200 with no redirect, no Cloudflare challenge (`cf-mitigated`), `Content-Type: application/zip` (not a web
   page), the size and sha256 the build recorded, and that the zip finishes in 90% of txAdmin's 150 seconds at
   `min_mbit` (3 Mbit/s: 40 MiB takes 112 s). It prints how fast each one came from the runner too.
4. **promote** (in the `cdn` environment, one at a time): make the four CDN recipes from the verified builds, check
   them with txAdmin's parser rules (`tools/validate_recipes.cjs`), upload them to `recipes/<release>/` and check them
   through the CDN. Only then copy them to `recipes/whip-*.yaml`, record the release in `releases.json`, purge those
   URLs from Cloudflare's cache, and check the stable URLs until they serve the new recipes (with a short cache, and
   under txAdmin's 4.5 seconds to load a recipe). If they don't, it puts the previous release back and fails.

The release id is the UTC time and the commit, e.g. `20261116-120000-87d08a6`. A run whose commit is older than the
one the CDN already serves (two pushes whose runs finished out of order) doesn't promote.

## Staging a resource's release

When a resource repo publishes a release, its `release-zips.yml` (from `templates/release-zips.yml`) can also call
this workflow to build, upload and verify that one resource at the new tag, without changing any recipe. The zips are
then on the CDN before the manifest moves to the tag, and the publish only promotes. It runs when the
`WHIP_CDN_BASE` variable is set for that repo (an organization variable covers them all) and the `R2_*` secrets are
there (organization secrets, shared with those repos).

## Rolling back

Actions > CDN > Run workflow, with **action** `rollback` and **release_id** the release to go back to. The ids are in
`<cdn>/recipes/releases.json` and in each publish run's summary. It copies that release's recipes back to the stable
paths (nothing is rebuilt: its zips are still in the bucket), records it, purges and checks the stable URLs. A
rollback can go forward again the same way. To retire a bad release for good, fix the manifest and push: that makes a
new release.

## Setting it up

1. **A bucket.** Cloudflare dashboard > R2 > Create bucket (e.g. `whip-cdn`). Don't add lifecycle rules that delete
   anything under `releases/` or `recipes/`: rollbacks need them. Optionally, add a bucket lock rule on the
   `releases/` prefix so nothing there can be deleted.
2. **A custom domain.** The bucket > Settings > Custom Domains > Connect Domain (e.g. `cdn.example.com`, on a zone in
   the same Cloudflare account). Leave the `r2.dev` URL off. Turn on Smart Tiered Cache for the zone. On that
   hostname, don't put anything that challenges requests (Bot Fight Mode, Under Attack mode, a WAF challenge or
   Access): txAdmin can't solve a challenge. The verify step fails if one shows up.
3. **An R2 API token.** R2 > Manage API tokens > Create API token: **Object Read & Write**, for that bucket only. It
   gives an Access Key ID and a Secret Access Key.
4. **Optional, a cache purge token.** My Profile > API Tokens > Create Token: **Zone > Cache Purge > Purge**, for that
   zone only. Without it, the stable recipes still change everywhere within 60 seconds (their max-age).
5. **Secrets** (Settings > Secrets and variables > Actions > Secrets; organization secrets if resource repos stage
   releases too):
   - `R2_ACCOUNT_ID`: the Cloudflare account ID (the S3 endpoint is `https://<account>.r2.cloudflarestorage.com`)
   - `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`: from step 3
   - `R2_BUCKET`: the bucket's name
   - `CF_API_TOKEN`, `CF_ZONE_ID`: from step 4 (optional)
6. **Variables** (the same page > Variables):
   - `WHIP_CDN_BASE`: the custom domain's URL, e.g. `https://cdn.example.com`, with no `/` at the end
   - `WHIP_HEAVY_RUNNER` (optional): `boat` to run build and verify on Boat CI's VMs. Unset, they run on
     `ubuntu-latest`.
7. **An environment.** Settings > Environments > New environment `cdn`. Add required reviewers to make every promote
   and rollback wait for a maintainer's approval, and limit it to the `main` branch. Without reviewers they run as soon
   as verify passes.
8. **Optional, Boat CI.** With the Boat CLI: `boat ci connect --org <your Boat org>` installs the Boat CI GitHub App
   on the WHIPFX organization; this repo is public, so also `boat ci public WHIPFX on --users "<maintainers>"`, so
   only runs that maintainers start use the VMs (a fork's pull request never reaches the heavy jobs anyway). Then set
   `WHIP_HEAVY_RUNNER` to `boat`. `boat ci --repo WHIPFX/txAdmin-recipe` shows its jobs, `boat ci logs <id>` a job's
   log.
9. **The first release.** Actions > CDN > Run workflow (action `publish`). The first one builds every resource, so it
   takes longest. Then put the CDN's URLs in the README, in place of `<cdn>`.
10. **Resource repos (optional).** Copy `templates/release-zips.yml` to each big resource's
    `.github/workflows/release-zips.yml` (with its `{{DEST}}`) so its releases are staged on the CDN as well.

Until `WHIP_CDN_BASE` and the `R2_*` secrets are set, the workflow only prints a notice and does nothing.

## Trying it on one computer

`tools/cdn.py` takes a `dir:<folder>` store in place of the bucket, and `serve` serves that folder the way the CDN
serves the bucket, so the whole pipeline runs without Cloudflare:

    python tools/cdn.py serve --store dir:/tmp/bucket --port 8787 &
    python tools/cdn.py build   --store dir:/tmp/bucket --dist /tmp/dist
    python tools/cdn.py upload  --store dir:/tmp/bucket --dist /tmp/dist
    python tools/cdn.py verify  --base http://127.0.0.1:8787 --dist /tmp/dist
    python tools/cdn.py promote --store dir:/tmp/bucket --base http://127.0.0.1:8787 --dist /tmp/dist --release-id test-1
    python tools/cdn.py rollback --store dir:/tmp/bucket --base http://127.0.0.1:8787 --to test-1

Wrangler's local R2 (`wrangler dev`, with `local_dev.experimental_s3_credentials` on the bucket) also works, through
the real S3 code path: `--store s3:<bucket> --endpoint http://127.0.0.1:8787/cdn-cgi/local/r2/s3` and
`--base http://127.0.0.1:8787/cdn-cgi/local/r2/public/<bucket>`. `python -m unittest discover -s tools/tests` runs
the whole pipeline against throwaway git repos.
