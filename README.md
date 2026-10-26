# gh-cache-download

Download a GitHub Actions cache entry to your machine, by key, to see what is actually in it.

```console
$ gh cache list -R Jwrede/gh-cache-download
9507487879  e2e-40288214454-1  459 B  2026-10-26T07:50:08Z  2026-10-26T07:50:10Z

$ gh cache-download get e2e-40288214454-1 -R Jwrede/gh-cache-download -x
Cache 9507487879: refs/heads/main, 459 bytes, version b107c4986568
Run 40288300859: https://github.com/Jwrede/gh-cache-download/actions/runs/40288300859
Archive: cache-9507487879/cache.tzst (459 bytes)
Extracted 6 entries to cache-9507487879/files

$ find cache-9507487879/files -type f -o -type l
cache-9507487879/files/e2e-data/link
cache-9507487879/files/e2e-data/sub/a.txt
cache-9507487879/files/__parent__/__parent__/__parent__/.e2e-home/b.txt
```

The cache above was saved by this repository's CI from `e2e-data` (in the workspace) and
`~/.e2e-home` (outside it). The whole `get` took 14 seconds.

GitHub stores cache entries for `actions/cache`, `setup-node`, `setup-python` and similar
actions, but there is no button, API call or `gh` command that returns the archive. The cache
service hands out download URLs only to workflow runs. This extension starts a small helper
workflow on the branch that owns the cache, lets that run fetch the raw archive and upload it as
an artifact, then downloads and optionally extracts the artifact.

It has two parts:

- `gh cache-download`, a [gh](https://cli.github.com) extension (one Python 3.10+ file, no
  dependencies) that finds the entry, dispatches the helper workflow, waits for it and downloads
  the result.
- `Jwrede/gh-cache-download@v1`, a composite action that fetches one cache entry by key and
  uploads it as an artifact. The helper workflow uses it, and you can also use it directly in
  your own workflows.

## Why

Debugging a cache that is restored but not used ("why does the build ignore my cache?", "why is
this cache 2 GB?") needs the cache contents, and GitHub does not provide them:

- [cli/cli#9125](https://github.com/cli/cli/issues/9125) "Add a way to download github actions
  caches": 113 reactions, open since May 2024. A gh maintainer forwarded it to the Actions team
  in July 2025; there is no command yet. The workarounds in the thread are workflow snippets
  that restore the cache with `actions/cache` (which needs the original `path` list to match the
  cache version) and upload it as an artifact.
- [actions/cache#105](https://github.com/actions/cache/issues/105) "Ability to download cache
  and inspect contents": 34 reactions, closed by the stale bot in 2023 after a maintainer said
  downloading was not planned. Comments continued in 2025.
- `gh cache list` / `gh cache delete` and
  [actions/gh-actions-cache](https://github.com/actions/gh-actions-cache) list and delete
  entries but cannot download them.

This tool fetches the archive by key and version, so it does not need to know which paths were
cached.

## Install

```sh
gh extension install Jwrede/gh-cache-download
```

Then add the helper workflow to the repository, commit it, and push it to the default branch
(and to any other branch whose caches you want to download):

```sh
gh cache-download init        # writes .github/workflows/cache-download.yml
git add .github/workflows/cache-download.yml
git commit -m "Add cache-download helper workflow"
git push
```

The workflow only runs when you dispatch it. It has `actions: read` and `contents: read`
permissions, keeps the artifact for one day, and does not check out the repository.

## Usage

```sh
gh cache list                                # find the key
gh cache-download get KEY                    # downloads to ./cache-<id>/cache.tzst
gh cache-download get KEY -x                 # also extracts to ./cache-<id>/files
gh cache-download get KEY --ref dev -D out   # key exists on several branches
gh cache-download extract cache.tzst out     # extract an archive you already have
```

`get` looks the key up with `GET /repos/{owner}/{repo}/actions/caches` (exact key match), then
runs the helper workflow on the cache's branch with the key and version, waits for the run
(`--timeout`, default 900 s) and downloads the artifact.

### Extracted layout

`actions/cache` stores paths relative to `$GITHUB_WORKSPACE`, so anything cached outside the
workspace (for example `~/.npm` or `~/.cache/pip`) is stored as `../../../.npm/...`. `extract`
never writes outside the target directory: each `..` becomes a `__parent__` directory, absolute
paths lose their leading `/`, and members that would escape the directory (for example symlinks
to absolute paths) are skipped with a message. With the default workspace
`/home/runner/work/REPO/REPO`, `~/.npm` ends up at `files/__parent__/__parent__/__parent__/.npm`.

zstd archives (the default on GitHub-hosted runners) need the `zstd` command or Python 3.14+;
gzip and plain tar archives need nothing extra. Only archives from Linux runners are tested.

### Using the action directly

```yaml
permissions:
  actions: read
steps:
  - uses: Jwrede/gh-cache-download@v1
    with:
      key: npm-linux-${{ hashFiles('package-lock.json') }}
      artifact-name: npm-cache
```

Inputs: `key` (required), `version` (optional; looked up by key when empty, which needs
`actions: read`), `artifact-name`, `retention-days` (default 1), `token`. Outputs:
`archive-path`, `version`, `size`.

## Limits

- A run can only read caches of its own ref and of the default branch. `get` therefore
  dispatches on the cache's branch or tag, and the helper workflow must exist there.
- Caches created by pull request runs (`refs/pull/N/merge`) can only be read by runs of that
  pull request, which a manual dispatch cannot start. For those, add the action to the pull
  request's workflow (as in "Using the action directly") and download the artifact with
  `gh run download`.
- The action calls the cache service's `GetCacheEntryDownloadURL` endpoint, the same internal
  API that `actions/cache` v4+ uses through `@actions/cache`. GitHub does not document it as a
  public API, so it may change. Tested on github.com only.
- The artifact counts against the repository's artifact storage until it expires (one day by
  default).

## Development

```sh
python3 -m unittest discover -s tests -v
node --test tests/fetch-cache.test.js
```

CI also runs an end-to-end job that saves a real cache entry with `actions/cache/save`,
downloads it with the action and extracts it with the CLI.

## License

MIT
