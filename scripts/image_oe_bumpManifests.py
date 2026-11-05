#!/usr/bin/env python3
import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass
from typing import List
from xml.dom import minidom
from xml.sax.saxutils import quoteattr


@dataclass
class Product_Additional_Affected_Path:
    product_name: str
    additional_paths: List[str]


additional_affected_paths: List[Product_Additional_Affected_Path] = [
    # A vendor manifest that <include>s base/bsp (today only torizon/tdx)
    # gets those projects from the shared files, so base/pinned.xml and
    # bsp/pinned.xml must also be bumped from this build's projRevDict, or
    # they'd keep whatever revision the last build left them at.
    # Vendors that don't <include> them are filtered out by
    # _manifest_includes_path.
    # base/pinned.xml and bsp/pinned.xml are shared on purpose by
    # tdx/integration.xml and tdx/release.xml, as in scarthgap: both
    # pipelines bump them, so an extint bump also moves what release.xml
    # resolves to, and every bump goes through a reviewed MR. Vendors that
    # list their layers directly keep separate pins per pipeline.
    # "torizon" matches Jenkins' BUILD_PRODUCT_NAME for this repo's
    # builds (image-pipeline-scripts commit 49737bcc renamed the
    # product from "torizoncore" to "torizon" and started passing
    # --productName ${BUILD_PRODUCT_NAME} explicitly). tdxref builds
    # against the separate toradex-manifest.git and never touches
    # this repo, so it has no entry here.
    Product_Additional_Affected_Path(
        product_name="torizon", additional_paths=["base", "bsp"]
    ),
]

# Remotes whose projects stay on a branch in integration.xml, so an extint
# bump must not pin them. Every other layer, Toradex BSP layers included,
# is pinned there (see README).
floatingRemotes = ["torizon"]

# Attributes copied from `repo manifest -r` onto the bumped project.
# repo needs "upstream" to fetch a pinned SHA in shallow (--depth) syncs.
# "dest-branch" is left out: `repo manifest -r` adds it to every project,
# no manifest here uses it, and it only matters to `repo upload`.
pinnedAttributes = ["revision", "upstream"]


def _vendor_of(manifest_file: str) -> str:
    # Jenkins passes the `repo init -m` path, e.g. torizon/tdx/next.xml.
    # Anything else would give a bogus vendor ("." for ./next.xml) or break
    # the path joins with the manifest root (an absolute path), so fail.
    m = re.fullmatch(r"torizon/([^/.][^/]*)/[^/]+\.xml", manifest_file or "")
    if not m:
        raise ValueError(
            f"expected torizon/<vendor>/<file>.xml, got {manifest_file!r}"
        )
    return m.group(1)


def get_target_branch_name(
    product_name: str,
    pipeline_type: str,
    manifest_branch: str,
    origin_manifest_file: str,
) -> str:
    # product_name is "torizon" for every vendor, so the vendor directory
    # must be part of the branch name. Otherwise two vendors' builds
    # force-push the same bump branch and update each other's MR. Fail
    # rather than fall back to a shared name: the image-oe-*.sh scripts
    # default BUILD_MANIFEST_FILE to a bare "default.xml".
    vendor = _vendor_of(origin_manifest_file)
    return f"bump-{product_name}-{vendor}-{manifest_branch}-{pipeline_type}"


def _manifest_includes_path(manifest_file: str, path: str) -> bool:
    # Only bump a shared path (base/bsp) if this vendor's manifest actually
    # <include>s it -- e.g. torizon/nvidia/next.xml lists its own layers
    # directly and never touches base/bsp, so it must not qualify just
    # because it shares product_name "torizon" with torizon/tdx/next.xml.
    if not os.path.isfile(manifest_file):
        return False
    manifestDoc = minidom.parse(manifest_file)
    prefix = f"{path}/"
    return any(
        include.getAttribute("name").startswith(prefix)
        for include in manifestDoc.getElementsByTagName("include")
    )


def get_relevant_product_path(
    product_name: str, manifest_file: str, manifest_root_path: str
) -> List[str]:
    affected_paths = []
    full_manifest_file = f"{manifest_root_path}/{manifest_file}"
    for item in additional_affected_paths:
        if item.product_name == product_name:
            affected_paths.extend(
                path
                for path in item.additional_paths
                if _manifest_includes_path(full_manifest_file, path)
            )
    affected_paths.append(os.path.dirname(manifest_file))
    return affected_paths


def get_revision_dict(updatedManifestFile) -> dict:
    """Map each project name to its pinned attributes (see pinnedAttributes)."""
    res = dict()
    manifestDoc = minidom.parse(updatedManifestFile)
    projects = manifestDoc.getElementsByTagName("project")
    for project in projects:
        res[project.getAttribute("name")] = {
            attr: project.getAttribute(attr)
            for attr in pinnedAttributes
            if project.hasAttribute(attr)
        }
    return res


def get_relevant_manifest_xml_files(
    product_name, original_manifest_file, manifest_root_path, pipeline_type
) -> List[str]:
    manifest_root_path = manifest_root_path.rstrip("/")
    original_manifest_file_dir = os.path.dirname(original_manifest_file)

    file_list = []

    ## Nightly bumps release.xml, extint bumps integration.xml
    update_file_pipeline_type_map = {
        "nightly": "release.xml",
        "extint": "integration.xml",
    }
    affected_paths: List[str] = get_relevant_product_path(
        product_name, original_manifest_file, manifest_root_path
    )

    # Additional paths only bump pinned xml files,
    # the release.xml or integration.xml should not be touched.
    for path in affected_paths:
        # Search pinned-* files.
        file_list.extend(
            glob.glob(
                rf"{manifest_root_path}/{path}/**/pinned*.xml",
                recursive=True,
            )
        )

    # Target release.xml or integration.xml based on pipeline type, in the
    # original manifest file's directory. It may not exist (see bump_xmls).
    target_file = (
        f"{manifest_root_path}/{original_manifest_file_dir}/"
        f"{update_file_pipeline_type_map[pipeline_type]}"
    )
    file_list.append(target_file)
    return file_list


_COMMENT = re.compile(r"<!--.*?-->", re.S)
_PROJECT_TAG = re.compile(r"<project\b[^>]*>")
_DEFAULT_TAG = re.compile(r"<default\b[^>]*>")
_ATTR = r"""\s*=\s*("[^"]*"|'[^']*')"""


def _attrs(tag: str) -> dict:
    # Let the XML parser read the attributes, so entities (&amp;, &#38;...)
    # are unescaped like in projRevDict and equal values compare equal.
    self_closed = tag if tag.endswith("/>") else tag[:-1] + "/>"
    return dict(minidom.parseString(self_closed).documentElement.attributes.items())


def _set_attr(tag: str, name: str, value: str, after: str) -> str:
    """Set an attribute on a start tag, keeping the rest of its text as is.

    A new attribute goes right after `after` if the tag has it, otherwise
    at the end of the tag.
    """
    m = re.search(r"\s" + re.escape(name) + _ATTR, tag)
    if m:
        return tag[: m.start(1)] + quoteattr(value) + tag[m.end(1):]
    anchor = re.search(r"\s" + re.escape(after) + _ATTR, tag)
    pos = anchor.end() if anchor else re.search(r"\s*/?>$", tag).start()
    return f"{tag[:pos]} {name}={quoteattr(value)}{tag[pos:]}"


def bump_xmls(projRevDict, fileList, pipelineType, dryrun=False) -> List[str]:
    changedFiles = []
    for f in fileList:
        # Don't create a missing integration.xml/release.xml from next.xml.
        # Some vendors don't ship them yet on purpose (e.g. syn/mtk BSPs
        # don't build on this release, qcom has no release yet), and
        # manifest-ci.yml excludes them, so a created file would land
        # unvalidated. Adding a vendor's first one is a manual change that
        # also enables it in CI.
        if not os.path.isfile(f):
            print(f"Skipping {f}: file does not exist.", file=sys.stderr)
            continue
        with open(f, encoding="utf-8") as reader:
            text = reader.read()

        # Edit the pinned attributes in the file's own text instead of
        # re-serialising it: minidom would also reflow unrelated lines
        # (aligned <remote>s, "<include ... />") and bury the bump in noise.
        comments = [m.span() for m in _COMMENT.finditer(text)]

        def in_comment(pos):
            return any(start <= pos < end for start, end in comments)

        default = next(
            (_attrs(m.group()) for m in _DEFAULT_TAG.finditer(text)
             if not in_comment(m.start())),
            {},
        )

        def bump_project(match):
            tag = match.group()
            if in_comment(match.start()):
                return tag
            attrs = _attrs(tag)
            pinned = projRevDict.get(attrs.get("name"))
            if not pinned or not pinned.get("revision"):
                return tag
            remote = attrs.get("remote", default.get("remote"))
            if pipelineType == "extint" and remote in floatingRemotes:
                return tag
            for attr, value in pinned.items():
                # Only "revision" may be inherited from <default>.
                current = attrs.get(
                    attr, default.get(attr) if attr == "revision" else None
                )
                if current != value:
                    # Always set explicitly on the project itself --
                    # touching <default> would shift every other
                    # project that inherits from it too.
                    tag = _set_attr(tag, attr, value, after="revision")
            return tag

        bumped = _PROJECT_TAG.sub(bump_project, text)
        if bumped != text:
            changedFiles.append(f)
            if not dryrun:
                with open(f, "w", encoding="utf-8") as writer:
                    writer.write(bumped)

    # Deduplicate, keeping a stable order for the caller's `git add`.
    return list(dict.fromkeys(changedFiles))


def parsArgs():
    parser = argparse.ArgumentParser(prog="image_oe_bumpManifests.py")
    # Add subparser for bump manifest

    parser.add_argument(
        "--pipelineType",
        required=True,
        choices=["nightly", "extint"],
        help="Pipeline type to be used for the image.",
    )

    parser.add_argument(
        "--productName",
        required=True,
        help="Name of the product. This is used to determine which manifest files should be updated",
    )

    sub_parsers = parser.add_subparsers(
        help="sub-command help", dest="cmd", required=True
    )
    bump_manifest_parser = sub_parsers.add_parser(
        "bump-manifest", help="Bump manifest files."
    )

    bump_manifest_parser.add_argument(
        "--updatedManifestFile",
        required=True,
        help="The manifest file with the frozen revisions (repo manifest -r).",
    )

    bump_manifest_parser.add_argument(
        "--originManifestFile",
        required=True,
        help=(
            "Original manifest file that the build is made of. "
            "This path needs to be relative to the manifest root path."
        ),
    )

    bump_manifest_parser.add_argument(
        "--manifestRootPath",
        required=True,
        help="Path to where manifest mono repo is checked out.",
    )

    bump_manifest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the files that would change without writing them.",
    )

    get_branch_name_parser = sub_parsers.add_parser(
        "get-target-branch-name",
        help=(
            "Get target branch name for the manifest based on origin "
            "manifest file and build pipeline type."
        ),
    )
    get_branch_name_parser.add_argument(
        "--manifestBranch", required=True, help="The manifest branch name."
    )
    # Jenkins exports job parameters as environment variables, so the
    # current ManifestBump call (which doesn't pass this flag) still gets
    # a per-vendor branch name.
    get_branch_name_parser.add_argument(
        "--originManifestFile",
        default=os.environ.get("BUILD_MANIFEST_FILE"),
        help=(
            "Manifest file the build is made of, relative to the manifest "
            "root path (default: $BUILD_MANIFEST_FILE)."
        ),
    )

    return parser.parse_args()


def main():
    args = parsArgs()
    if args.cmd == "get-target-branch-name":
        try:
            target_branch_name = get_target_branch_name(
                args.productName,
                args.pipelineType,
                args.manifestBranch,
                args.originManifestFile,
            )
        except ValueError as e:
            sys.exit(f"get-target-branch-name: {e}")
        print(target_branch_name)
        return
    elif args.cmd == "bump-manifest":
        try:
            _vendor_of(args.originManifestFile)
        except ValueError as e:
            sys.exit(f"bump-manifest: {e}")

        # Don't pin revisions from a matrix build that failed: they would
        # land in release.xml/integration.xml as if they were known-good.
        # Jenkinsfile.ManifestBump sets MATRIX_BUILD_STATUS in its
        # environment {} from the triggering matrix build's result. Print an
        # empty file list and exit 0: the Jenkinsfile then commits and pushes
        # nothing, without failing the job a second time for the same build.
        matrix_build_status = os.environ.get("MATRIX_BUILD_STATUS")
        if matrix_build_status != "SUCCESS":
            print(
                f"Skipping manifest bump: matrix build did not succeed "
                f"(MATRIX_BUILD_STATUS={matrix_build_status!r}).",
                file=sys.stderr,
            )
            print()
            return

        filesToModify = get_relevant_manifest_xml_files(
            args.productName,
            args.originManifestFile,
            args.manifestRootPath,
            args.pipelineType,
        )
        revisionsDict = get_revision_dict(args.updatedManifestFile)
        changedFiles = bump_xmls(
            revisionsDict, filesToModify, args.pipelineType, dryrun=args.dry_run
        )
        print(" ".join(changedFiles))
        return


if __name__ == "__main__":
    main()
