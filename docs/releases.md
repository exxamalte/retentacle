# Releases and update checks

> Part of the [documentation](../README.md#documentation)

## How the badge decides

The in-app update check (described in
[Web interface](web-ui.md#update-notifications)) compares the running version
against the newest published release:

* **GitHub releases are required.** The check calls the GitHub Releases API
  (`/releases/latest`) of the project repository. The **Release workflow** in
  this repository automatically creates a GitHub Release from every pushed `v*`
  tag, so tagging a version is all it takes — no manual release step.
* **Pre-releases are never announced.** Only plain numeric versions such as
  `v2026.6.0` can light up the badge. Tags with any appended suffix —
  `-dev`, `-alpha`, `-beta`, `-rc`, `.dev1`, `b1` and similar — are classified
  as pre-releases: the Release workflow creates them marked as pre-release (and
  keeps them out of GitHub's "latest"), and the in-app check defensively ignores
  any release flagged `prerelease` or `draft`.
* The project repository is public, so no token is needed. It talks to
  `api.github.com` anonymously (at most ~4 requests/day thanks to the cache,
  far below the 60/hour limit). For a **private** repository (e.g. your own
  fork with releases), set `RETENTACLE_UPDATE_TOKEN` to a token with read
  access to that repo's contents.

## Cutting a release

Bump the version in `pyproject.toml`, then tag and push:

| Tag | Result |
| --- | --- |
| `v2026.6.0` | Stable GitHub Release, becomes GitHub's "latest", moves the container `latest` tag, triggers the in-app update badge. |
| `v2026.6.0-beta1` (or `-alpha`, `-rc`, `b1`) | Pre-release GitHub Release and pre-release container tag; existing installs are not prompted to update to it. |

Both kinds publish container images automatically through the *Container*
workflow — see [Docker deployment](docker.md#image-tags) for the resulting tags.

## Release notes

The Release workflow generates notes from the merged pull requests in the range
between two tags:

* **Pre-releases** compare against the previously published release, so each
  `-beta`, `-rc` or `-dev` tag lists what changed since its predecessor.
* **Stable releases** compare against the previous *stable* tag instead of the
  last published release. Without that, a release preceded by six betas would
  only show the changes since beta 6 and hide everything the betas introduced.

If a version line has no earlier stable tag at all (the first stable release
after a series of `-dev` builds), the workflow falls back to the newest tag
outside the current pre-release series.

## Controlling the check

| Variable | Default | Purpose |
| --- | --- | --- |
| `RETENTACLE_UPDATE_CHECK` | enabled | Set to `0` to disable update checks entirely. |
| `RETENTACLE_UPDATE_REPO` | `exxamalte/retentacle` | Check a different `owner/repo`. |
| `RETENTACLE_UPDATE_TOKEN` | — | Bearer token for private repositories. |

The check runs in the background when a signed-in admin views the dashboard or
the Settings page, and results are cached for 6 hours — the app is fully
usable without any internet access, it just never shows the badge.
