# Torizon manifest

This repository holds the [`repo`](https://gerrit.googlesource.com/git-repo/)
tool manifests used to check out Torizon OS: the set of Yocto layers, their
git remotes, and the revisions to build each supported vendor/SoC platform
from.

> [!WARNING]
> The release manifest should be avoided until Torizon OS 8 is officially released.
> Their pinned revisions are not validated yet and Torizon OS may not build with
> them.

## Supported vendors

Each vendor lives in its own folder under `torizon/`:

| Folder            | Vendor / platform                          |
|--------------------|--------------------------------------------|
| `torizon/tdx/`     | Toradex SoMs (via `meta-toradex-bsp`/`meta-toradex-distro`, silicon-agnostic) |
| `torizon/mtk/`     | MediaTek Genio (`meta-mediatek-bsp`), ADLINK SMARC modules (`meta-adlink-mtk`) |
| `torizon/nxp/`     | NXP i.MX community BSP (`meta-imx`, non-Toradex boards) |
| `torizon/nvidia/`  | NVIDIA Jetson (Tegra), `meta-tegra`        |
| `torizon/qcom/`    | Qualcomm Dragonwing (`meta-qcom`), Arduino VENTUNO Q (`meta-qcom-arduino`) |
| `torizon/syn/`     | Synaptics Astra                            |
| `torizon/ti/`      | TI community BSP (`meta-ti`, non-Toradex boards) |
| `torizon/x86/`     | Intel/AMD x86, `meta-intel`                |
| `torizon/blaze/`   | Blaze (`imx6sx-blaze`), a customer i.MX 6SoloX board; `next.xml` only, see [README-blaze](https://github.com/torizon/meta-torizon-bsp/blob/wrynose-8.x.y/docs/README-blaze.md) |

`base/` and `bsp/` are shared, vendor-agnostic includes (oe-core/bitbake
base layers, and common BSP layers) pulled in by the vendor manifests via
`<include>`.

See here a complete list of [Torizon OS supported devices](https://developer.toradex.com/torizon/torizoncore/supported-hardware/).

## Manifest files and what they mean

Within a vendor folder, the manifest filename says what it locks down:

- **`next.xml`** - the manifest where all layers are on bleeding edge. No
  hash pinned, every layer tracking the HEAD of it's branch.
- **`integration.xml`** - the manifest used for Torizon OS development. All
  external layers (non-Torizon) are pinned, and all Torizon layers
  are on bleeding edge.
- **`release.xml`** - the manifest used to do our `monthly`/`quarterly`
  releases. All hashes are pinned, so anyone can checkout a release version tag
  and sync the same layers commits used for that specific release.

## Getting started

```sh
repo init -u https://github.com/torizon/manifest.git -b wrynose-8.x.y -m torizon/tdx/release.xml
repo sync
```

Swap `torizon/tdx/release.xml` for the manifest of the vendor you're
targeting (see the table above).

- [Build Torizon OS
  guide](https://developer.toradex.com/torizon/in-depth/build-torizoncore-from-source-with-yocto-projectopenembedded/)

## CI

`.github/workflows/manifest-ci.yml` runs on every push/PR to `wrynose-8.x.y`:

- **lint xml** - every `*.xml` in the repo must be well-formed.
- **test scripts** - runs `scripts/tests` (see [Manifest bump](#manifest-bump)).
- **repo init/sync (\<vendor\> \<manifest\>)** - for each vendor folder, a real
  `repo init` + `repo sync` against that vendor's manifests, at the
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

## Manifest bump

After each image build, Jenkins (`Jenkinsfile.ManifestBump` in
image-pipeline-scripts) pins the revisions it built into this repo with
`scripts/image_oe_bumpManifests.py`, and opens a bump MR from
`bump-torizon-<vendor>-<branch>-<pipeline>`:

```sh
repo manifest -r -o frozen.xml
BRANCH=$(scripts/image_oe_bumpManifests.py --productName torizon --pipelineType nightly \
  get-target-branch-name --manifestBranch wrynose-8.x.y)   # vendor from $BUILD_MANIFEST_FILE
FILES=$(scripts/image_oe_bumpManifests.py --productName torizon --pipelineType nightly \
  bump-manifest --originManifestFile torizon/tdx/next.xml \
  --updatedManifestFile frozen.xml --manifestRootPath ./)
```

- Builds use `torizon/<vendor>/next.xml`. `nightly` bumps that vendor's
  `release.xml`, and `extint` bumps its `integration.xml`, where the
  Torizon layers stay on their branch.
- `base/pinned.xml` and `bsp/pinned.xml` are bumped only for vendors whose
  manifest `<include>`s them (today `tdx`), and are shared by its
  `integration.xml` and `release.xml`.
- Only `revision`/`upstream` of `<project>`s are edited; the rest of each
  file is kept as is.
- A missing `integration.xml`/`release.xml` is skipped, not created.
- Nothing is bumped unless `MATRIX_BUILD_STATUS` is `SUCCESS`, and the
  script fails if it is unset. Set it by hand when running the script
  locally, and add `--dry-run` to only list the files that would change.

Tests: `python3 -m unittest discover -s scripts/tests -v`.
