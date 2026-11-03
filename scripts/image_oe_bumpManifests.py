#!/usr/bin/env python3
import argparse
import glob
import os
import sys
from dataclasses import dataclass
from typing import List
from xml.dom import minidom


@dataclass
class Product_Additional_Affected_Path:
    product_name: str
    additional_paths: List[str]


additional_affected_paths: List[Product_Additional_Affected_Path] = [
    # torizon/<vendor>/next.xml <include>s base/bsp (see
    # _repoint_includes_to_pinned) instead of listing their projects
    # directly, so base/pinned.xml and bsp/pinned.xml must also be bumped
    # from this build's projRevDict, or they'd keep whatever revision the
    # last unrelated build happened to leave them at.
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

# Toradex related projects.
# These are the remote names in manifest files owned by toradex.
toradexProjects = ["tdx", "toradex-torizon", "toradex", "torizon", "githt", "gitltdx"]


def get_target_branch_name(
    product_name: str, pipeline_type: str, manifest_branch: str
) -> str:
    return f"bump-{product_name}-{manifest_branch}-{pipeline_type}"


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
    """Return a dictionary that contains the project name and revision"""
    res = dict()
    manifestDoc = minidom.parse(updatedManifestFile)
    projects = manifestDoc.getElementsByTagName("project")
    for project in projects:
        res.update(
            {project.attributes["name"].value: project.attributes["revision"].value}
        )
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
    # original manifest file's directory. Not glob'd: this file may not exist
    # yet (e.g. first extint bump for a vendor that only ships next.xml) and
    # still needs to be created, seeded from the original manifest file.
    target_file = (
        f"{manifest_root_path}/{original_manifest_file_dir}/"
        f"{update_file_pipeline_type_map[pipeline_type]}"
    )
    file_list.append(target_file)
    return file_list


def _attr_with_default(element, attr_name, default_element):
    # <project> may omit "remote"/"revision" and inherit them from <default>
    # (repo manifest format). Fall back there instead of KeyError'ing.
    if element.hasAttribute(attr_name):
        return element.getAttribute(attr_name)
    if default_element is not None and default_element.hasAttribute(attr_name):
        return default_element.getAttribute(attr_name)
    return None


def _repoint_includes_to_pinned(manifestDoc):
    # A freshly-seeded integration.xml/release.xml must be a pinned result,
    # not still floating. next.xml's <include>s point at the shared
    # base/bsp integration.xml (unpinned); repoint them at pinned.xml
    # (already frozen) so the seeded file doesn't silently keep tracking
    # master through the include.
    for include in manifestDoc.getElementsByTagName("include"):
        name = include.getAttribute("name")
        if name.endswith("integration.xml"):
            include.setAttribute("name", name[: -len("integration.xml")] + "pinned.xml")


def bump_xmls(
    projRevDict, fileList, pipelineType, originManifestFile=None, dryrun=False
) -> List[str]:
    changedFiles = []
    for f in fileList:
        if os.path.isfile(f):
            manifestDoc = minidom.parse(f)
        elif originManifestFile and os.path.isfile(originManifestFile):
            # f doesn't exist yet -- seed from the origin manifest (see comment above target_file in get_relevant_manifest_xml_files).
            manifestDoc = minidom.parse(originManifestFile)
            _repoint_includes_to_pinned(manifestDoc)
        else:
            continue
        defaultElements = manifestDoc.getElementsByTagName("default")
        defaultElement = defaultElements[0] if defaultElements else None
        projects = manifestDoc.getElementsByTagName("project")
        for project in projects:
            projectName = project.attributes["name"].value
            if projRevDict.get(projectName, False):
                remote = _attr_with_default(project, "remote", defaultElement)
                if pipelineType == "extint" and remote in toradexProjects:
                    continue
                else:
                    revision = projRevDict.get(projectName, False)
                    if revision:
                        currentRevision = _attr_with_default(
                            project, "revision", defaultElement
                        )
                        if currentRevision != revision:
                            # Always set explicitly on the project itself --
                            # touching <default> would shift every other
                            # project that inherits from it too.
                            project.setAttribute("revision", revision)
                            changedFiles.append(f)

        # Only write the file if it was changed
        if f in changedFiles:
            output = (manifestDoc.toxml(encoding="UTF-8")).decode("utf-8")
            # Need to add line break before <manifest> in order to keep same format as original
            output = output.replace("<manifest>", "\n<manifest>")
            if not dryrun:
                with open(f, "w") as writer:
                    writer.write(output + "\n")
                    writer.close()

    # Remove duplicated elements and return
    return list(set(changedFiles))


def parsArgs():
    parser = argparse.ArgumentParser(prog="image-eo-bumpManifests.py")
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

    sub_parsers = parser.add_subparsers(help="sub-command help", dest="cmd")
    bump_manifest_parser = sub_parsers.add_parser(
        "bump-manifest", help="Bump manifest files."
    )

    bump_manifest_parser.add_argument(
        "--updatedManifestFile",
        required=True,
        help="The manifest file with the updated freezed revisions.",
    )

    bump_manifest_parser.add_argument(
        "--originManifestFile",
        required=True,
        help=(
            "Original manifest file that the build is made of."
            "This path needs to relative to the manifest root path."
        ),
    )

    bump_manifest_parser.add_argument(
        "--manifestRootPath",
        required=True,
        help="Path to where manifest mono repo is checked out.",
    )

    get_branch_name_parser = sub_parsers.add_parser(
        "get-target-branch-name",
        help="Get target branch name for the manifest based on origin manifest file and build piepline type.",
    )
    get_branch_name_parser.add_argument(
        "--manifestBranch", required=True, help="The manifest branch name."
    )

    return parser.parse_args()


def main():
    args = parsArgs()
    if args.cmd == "get-target-branch-name":
        target_branch_name = get_target_branch_name(
            args.productName, args.pipelineType, args.manifestBranch
        )
        print(target_branch_name)
        return
    elif args.cmd == "bump-manifest":
        # TOR-4556: MATRIX_BUILD_STATUS is exported by the Jenkinsfile's
        # `environment {}` block into every sh step's env automatically --
        # read it here instead of adding a Groovy-side gate, so the
        # Jenkinsfile stays untouched.
        matrix_build_status = os.environ.get("MATRIX_BUILD_STATUS")
        if matrix_build_status != "SUCCESS":
            print(
                f"Skipping manifest bump: matrix build did not succeed "
                f"(MATRIX_BUILD_STATUS={matrix_build_status!r}).",
                file=sys.stderr,
            )
            sys.exit(1)

        filesToModify = get_relevant_manifest_xml_files(
            args.productName,
            args.originManifestFile,
            args.manifestRootPath,
            args.pipelineType,
        )
        revisionsDict = get_revision_dict(args.updatedManifestFile)
        originManifestFullPath = os.path.join(
            args.manifestRootPath.rstrip("/"), args.originManifestFile
        )
        changedFiles = bump_xmls(
            revisionsDict,
            filesToModify,
            args.pipelineType,
            originManifestFile=originManifestFullPath,
        )
        print(" ".join(changedFiles))
        return


if __name__ == "__main__":
    main()
