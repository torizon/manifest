# Torizon manifest

This repository holds the [`repo`](https://gerrit.googlesource.com/git-repo/)
tool manifests used to check out Torizon OS: the set of Yocto layers, their
git remotes, and the revisions to build each supported vendor/SoC platform
from.

## Supported vendors

Each vendor lives in its own folder under `torizon/`:

| Folder            | Vendor / platform                          |
|--------------------|--------------------------------------------|
| `torizon/tdx/`     | Toradex SoMs (via `meta-toradex-bsp`/`meta-toradex-distro`, silicon-agnostic) |
| `torizon/nxp/`     | NXP i.MX community BSP (`meta-imx`, non-Toradex boards) |
| `torizon/nvidia/`  | NVIDIA Jetson (Tegra), `meta-tegra`        |
| `torizon/syn/`     | Synaptics Astra                            |
| `torizon/ti/`      | TI community BSP (`meta-ti`, non-Toradex boards) |
| `torizon/x86/`     | Intel/AMD x86, `meta-intel`                |

`base/` and `bsp/` are shared, vendor-agnostic includes (oe-core/bitbake
base layers, and common BSP layers) pulled in by the vendor manifests via
`<include>`.

See here a complete list of [Torizon OS supported devices](https://developer.toradex.com/torizon/torizoncore/supported-hardware/).

## Manifest files and what they mean

Within a vendor folder, the manifest filename says what it locks down:

- **`next.xml`** - the manifest where all layers are on bleeding edge. No
  hash pinned, every layer tracking the HEAD of it's branch.
- **`integration.xml`** - the manifest used for Torizon OS development. All
  external layers (non-Toradex) are pinned, and all Torizon and Toradex layers
  are on bleeding edge.
- **`release.xml`** - the manifest used to do our `monthly`/`quarterly`
  releases. All hashes are pinned, so anyone can checkout a release version tag
  and sync the same layers commits used for that specific release.

**On `master` specifically**, only `next.xml` exists per vendor, since on
`master` we only are for full integration, and we don't do releases.

## Getting started

```sh
repo init -u <this-repo-url> -b master -m torizon/tdx/release.xml
repo sync
```

Swap `torizon/tdx/release.xml` for the manifest of the vendor you're
targeting (see the table above).

- [Build Torizon OS
  guide](https://developer.toradex.com/torizon/in-depth/build-torizoncore-from-source-with-yocto-projectopenembedded/)

## CI

`.github/workflows/manifest-ci.yml` runs on every push/PR to `master`:

- **lint xml** - every `*.xml` in the repo must be well-formed.
- **repo init/sync (\<vendor\>)** - for each vendor folder, a real
  `repo init` + `repo sync` against that vendor's `release.xml`, at the
  exact commit under test. It's shallow and blob-less
  (`--depth=1 --clone-filter=blob:none`) so it stays fast while still
  proving every include, remote, and revision actually resolves - not a
  dry run.

### Testing CI locally with `act`

You don't have to push and wait on Actions to validate a workflow change.
[`act`](https://github.com/nektos/act) runs the same workflow locally
against Docker.

Install it (no root needed):

```sh
curl -sL https://github.com/nektos/act/releases/latest/download/act_Linux_x86_64.tar.gz \
  | tar -xz act
mkdir -p ~/.local/bin && mv act ~/.local/bin/
export PATH="$HOME/.local/bin:$PATH"   # add to your shell rc to persist
```

Docker must be running. Pin the runner image to one that actually has
git/curl/apt like a real `ubuntu-latest` GitHub-hosted runner (act's
default minimal image doesn't); either pass it on every call or keep a
local, untracked `.actrc` in the repo root:

```
-P ubuntu-latest=catthehacker/ubuntu:act-latest
```

Then run:

```sh
# everything the "push" trigger would run
act push

# just the fast lint job
act push -j lint-xml

# repo init/sync for one vendor, fast iteration
act push -j repo-init-sync --matrix vendor:tdx

# the full repo-init-sync matrix, all vendors (real network I/O
# against upstream remotes, ~1-2 min)
act push -j repo-init-sync
```

`.actrc` is intentionally not committed (`.gitignore` covers it, along
with any secrets files, custom event payloads, or local artifact/cache
server output `act` may drop) - keep it as a local file per the snippet
above.
