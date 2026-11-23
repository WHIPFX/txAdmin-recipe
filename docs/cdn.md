# WHIP's CDN

The recipes, and every file they download, are also served from a Cloudflare R2 bucket at `https://media.whipfx.net`:
WHIP's CDN. A server that can't reach GitHub, or reaches it too slowly for txAdmin's 150 seconds per download,
installs from there. The GitHub recipes stay as they are, as the fallback.

CI does all of it (`.github/workflows/cdn.yml`, which runs `tools/cdn.py`) on GitHub's own runners. Nobody uploads
by hand.

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
`recipes/<release>/`. The bucket lock on `releases/` (setup step 2) makes R2 itself refuse any overwrite or delete
there.

## A release

A push to `main` that changes `recipe/` (or the tools) runs the workflow. That includes every Recipe update: it pushes
the manifest's moves as the WHIP Recipes app, and an app's pushes start workflows. A run with nothing new to publish
stops at promote with a notice: promote makes the live release's recipes again from the manifest, and if they come
out the same, there is nothing to do. Runs go one at a time, in the order they started. Each job runs only if the
one before passed.

1. **check**: the unit tests, and the committed recipes are what `recipe/manifest.toml` makes.
2. **build**: for each resource whose tag isn't in the bucket yet, clone it at the tag, zip every file it tracks (as
   `tools/release_zips.py` does), and upload the zips, then the index. A resource already in the bucket at its tag is
   not built again, so a release usually builds one resource.
3. **verify**: download every zip of every resource back through the public CDN URL, as txAdmin will, and check:
   HTTP 200 with no redirect, no Cloudflare challenge (`cf-mitigated`), `Content-Type: application/zip` (not a web
   page), the size and sha256 the build recorded, and that the zip finishes in 90% of txAdmin's 150 seconds at
   `min_mbit` (3 Mbit/s: 40 MiB takes 112 s). It prints how fast each one came from the runner too.
4. **promote**: make the four CDN recipes from the verified builds, check them with txAdmin's parser rules
   (`tools/validate_recipes.cjs`), upload them to `recipes/<release>/` and check them through the CDN. Only then copy
   them to `recipes/whip-*.yaml`, record the release in `releases.json`, purge those URLs from Cloudflare's cache (with
   the optional purge token), and check the stable URLs until they serve the new recipes (with a short cache, and
   under txAdmin's 4.5 seconds to load a recipe). If they don't, it puts the previous release back and fails.

The release id is the UTC time, the commit and the attempt, e.g. `20261116-120000-87d08a6-1`: re-running a failed
promote makes a new id. A run whose commit is older than the one the CDN already serves doesn't promote.

## Rolling back

Actions > CDN > Run workflow, with **action** `rollback` and **release_id** the release to go back to. The ids are in
`https://media.whipfx.net/recipes/releases.json` and in each publish run's summary. It copies that release's recipes
back to the stable paths (nothing is rebuilt: its zips are still in the bucket), records it, purges and checks the
stable URLs. A rollback can go forward again the same way.

A rollback only holds until the next publish, and any resource's next release publishes. The manifest still names the
bad tag, and the Recipe update moves a tag set back by hand forward again. So to retire a bad release for good,
publish a fixed release of that resource.

## Setting it up

1. **A bucket.** Cloudflare dashboard > R2 > Create bucket (e.g. `whip-media`). Don't add lifecycle rules that delete
   anything under `releases/` or `recipes/`: rollbacks need them.
2. **A bucket lock on `releases/`.** The bucket > Settings > Bucket lock rules > Add rule, prefix `releases/`, retained
   indefinitely. R2 then refuses to overwrite or delete anything there, whoever asks.
3. **The custom domain.** The bucket > Settings > Custom Domains > Connect Domain: `media.whipfx.net`. Leave the
   `r2.dev` URL off. On that hostname, don't put anything that challenges requests (Bot Fight Mode, Under Attack mode,
   a WAF challenge or Access): txAdmin can't solve a challenge. The verify step fails if one shows up.
4. **An R2 API token.** R2 > Manage API tokens > Create API token: **Object Read & Write**, for that bucket only. It
   gives an Access Key ID and a Secret Access Key.
5. **The environment.** In this repo, Settings > Environments > New environment `cdn`. Under Deployment branches and
   tags, choose Selected branches and add `main`. No reviewers are needed. Then add these as the environment's
   secrets:
   - `R2_ACCOUNT_ID`: the Cloudflare account ID (the S3 endpoint is `https://<account>.r2.cloudflarestorage.com`)
   - `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`: from step 4
   - `R2_BUCKET`: the bucket's name

   Optionally, `CF_API_TOKEN` (My Profile > API Tokens > Create Token: **Zone > Cache Purge > Purge**, for that zone)
   and `CF_ZONE_ID`. Without them the stable recipes still change everywhere within 60 seconds (their max-age).
6. **The variable.** Settings > Secrets and variables > Actions > Variables > New repository variable:
   `WHIP_CDN_BASE` = `https://media.whipfx.net`. Until it's set, the workflow only prints a notice and does nothing.
7. **The first release.** Actions > CDN > Run workflow (action `publish`). The first one builds every resource
   (about 830 MB), so it takes longest.

Keep the R2 key in the `cdn` environment only, not in repository or organization secrets: only runs on `main` can
read it there.

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
