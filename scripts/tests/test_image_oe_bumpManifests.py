"""End-to-end tests for image_oe_bumpManifests.py.

Each test runs the script the way Jenkinsfile.ManifestBump does, against a
copy of this repo's manifests and a fake `repo manifest -r` output in which
every project of the vendor's next.xml is pinned to FAKE_SHA. Expected values
are derived from the manifests themselves, so ordinary manifest changes or a
new release branch don't break the tests. Edge cases use small fixtures.

Run with: python3 -m unittest discover -s scripts/tests -v
"""
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from xml.dom import minidom

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "image_oe_bumpManifests.py")
FAKE_SHA = "f" * 40
TARGET = {"nightly": "release.xml", "extint": "integration.xml"}


def _includes(root, manifest):
    """Files a manifest pulls in through <include>, recursively."""
    doc = minidom.parse(os.path.join(root, manifest))
    found = []
    for include in doc.getElementsByTagName("include"):
        name = include.getAttribute("name")
        found += [name] + _includes(root, name)
    return found


def _projects(root, manifest):
    """Map project name -> attributes, for a manifest and its includes.

    "remote" and "revision" fall back to the file's <default>.
    """
    res = {}
    for f in [manifest] + _includes(root, manifest):
        doc = minidom.parse(os.path.join(root, f))
        defaults = doc.getElementsByTagName("default")
        default = defaults[0] if defaults else None
        for p in doc.getElementsByTagName("project"):
            attrs = dict(p.attributes.items())
            for key in ("remote", "revision"):
                if key not in attrs and default is not None and default.hasAttribute(key):
                    attrs[key] = default.getAttribute(key)
            res[attrs["name"]] = attrs
    return res


class BumpTestCase(unittest.TestCase):
    def setUp(self):
        self._fresh_tree()

    def _fresh_tree(self):
        """Point self.root at a new copy of this repo's manifests."""
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        for d in ("base", "bsp", "torizon"):
            shutil.copytree(os.path.join(REPO_ROOT, d), os.path.join(self.root, d))

    def _write(self, path, content):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(content).lstrip())

    def _read(self, path):
        with open(os.path.join(self.root, path), encoding="utf-8") as f:
            return f.read()

    def _snapshot(self):
        files = {}
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                rel = os.path.relpath(os.path.join(dirpath, n), self.root)
                if rel != "frozen.xml":
                    files[rel] = self._read(rel)
        return files

    def _freeze(self, vendor):
        """Fake `repo manifest -r` output for torizon/<vendor>/next.xml."""
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<manifest>"]
        for name, attrs in _projects(self.root, f"torizon/{vendor}/next.xml").items():
            lines.append(
                f'  <project name="{name}" revision="{FAKE_SHA}" '
                f'upstream="{attrs["revision"]}" dest-branch="{attrs["revision"]}"/>'
            )
        self._write("frozen.xml", "\n".join(lines + ["</manifest>", ""]))

    def _run(self, origin, pipeline, status="SUCCESS", extra=(), check=True):
        env = dict(os.environ)
        env.pop("MATRIX_BUILD_STATUS", None)
        if status is not None:
            env["MATRIX_BUILD_STATUS"] = status
        res = subprocess.run(
            [sys.executable, SCRIPT, "--productName", "torizon",
             "--pipelineType", pipeline, "bump-manifest",
             "--originManifestFile", origin,
             "--updatedManifestFile", "frozen.xml",
             "--manifestRootPath", "./", *extra],
            cwd=self.root, env=env, capture_output=True, text=True,
        )
        if check:
            self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def _bump(self, vendor, pipeline, freeze=True, **kwargs):
        """Run a bump; return (printed files, changed files)."""
        if freeze:
            self._freeze(vendor)
        before = self._snapshot()
        res = self._run(f"torizon/{vendor}/next.xml", pipeline, **kwargs)
        after = self._snapshot()
        self.assertEqual(set(before), set(after), "files created or removed")
        changed = sorted(f for f in before if before[f] != after[f])
        for f in changed:
            self._assert_only_pins_changed(f, before[f], after[f])
        printed = sorted(os.path.normpath(f) for f in res.stdout.split())
        return printed, changed

    def _assert_only_pins_changed(self, path, old, new):
        """Every changed line is a <project> whose only edits are pins."""
        strip = lambda line: re.sub(r'\s(revision|upstream)="[^"]*"', "", line)
        old_lines, new_lines = old.splitlines(), new.splitlines()
        self.assertEqual(len(old_lines), len(new_lines), path)
        for a, b in zip(old_lines, new_lines):
            if a != b:
                self.assertIn("<project", a, f"{path}: non-project line changed")
                self.assertEqual(
                    strip(a), strip(b),
                    f"{path}: more than revision/upstream changed:\n"
                    + "\n".join(difflib.ndiff([a], [b])),
                )


class RealManifestsTest(BumpTestCase):
    """Bumps of this repo's own manifests."""

    def _expected_files(self, vendor, pipeline):
        target = f"torizon/{vendor}/{TARGET[pipeline]}"
        return sorted([target] + _includes(self.root, target))

    def _assert_pinned(self, vendor, pipeline, floating=()):
        target = f"torizon/{vendor}/{TARGET[pipeline]}"
        before = _projects(REPO_ROOT, target)
        after = _projects(self.root, target)
        upstream = _projects(self.root, f"torizon/{vendor}/next.xml")
        for name, attrs in after.items():
            if name not in upstream:
                continue
            if attrs.get("remote") in floating:
                self.assertEqual(attrs["revision"], before[name]["revision"], name)
            else:
                self.assertEqual(attrs["revision"], FAKE_SHA, name)
                self.assertEqual(attrs.get("upstream"), upstream[name]["revision"], name)
                self.assertNotIn("dest-branch", attrs, name)

    def test_nightly_pins_every_layer(self):
        for vendor in ("tdx", "nvidia"):
            with self.subTest(vendor=vendor):
                self._fresh_tree()
                printed, changed = self._bump(vendor, "nightly")
                expected = self._expected_files(vendor, "nightly")
                self.assertEqual(changed, expected)
                self.assertEqual(printed, expected)
                self._assert_pinned(vendor, "nightly")

    def test_extint_keeps_torizon_layers_floating(self):
        for vendor in ("tdx", "nvidia"):
            with self.subTest(vendor=vendor):
                self._fresh_tree()
                _, changed = self._bump(vendor, "extint")
                self.assertEqual(changed, self._expected_files(vendor, "extint"))
                self._assert_pinned(vendor, "extint", floating=("torizon",))

    def test_base_bsp_only_bumped_when_included(self):
        # nvidia lists its layers directly: base/ and bsp/ must not move.
        self.assertFalse(
            any(f.startswith(("base/", "bsp/")) for f in _includes(self.root, "torizon/nvidia/next.xml"))
        )
        _, changed = self._bump("nvidia", "nightly")
        self.assertFalse([f for f in changed if f.startswith(("base/", "bsp/"))])

    def test_missing_target_is_skipped(self):
        os.remove(os.path.join(self.root, "torizon/nvidia/integration.xml"))
        printed, changed = self._bump("nvidia", "extint")
        self.assertEqual((printed, changed), ([], []))

    def test_failed_matrix_build_bumps_nothing(self):
        for status in ("FAILURE", None):
            with self.subTest(status=status):
                printed, changed = self._bump("tdx", "nightly", status=status)
                self.assertEqual((printed, changed), ([], []))

    def test_dry_run_lists_without_writing(self):
        printed, changed = self._bump("nvidia", "nightly", extra=["--dry-run"])
        self.assertEqual(printed, ["torizon/nvidia/release.xml"])
        self.assertEqual(changed, [])

    def test_invalid_origin_manifest_fails(self):
        self._freeze("tdx")
        for origin in ("./next.xml", "torizon/next.xml", "/abs/torizon/tdx/next.xml"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self._run(origin, "nightly", check=False).returncode, 0)


class InPlaceEditTest(BumpTestCase):
    """The bump edits pins in the file's own text and nothing else."""

    def test_formatting_is_preserved(self):
        self._write("torizon/fx/next.xml", """
            <?xml version="1.0" encoding="UTF-8"?>
            <manifest>
              <remote alias="repo" name="a"    fetch="https://example.com/a"/>
              <remote alias="repo" name="bb"   fetch="https://example.com/b"/>
              <default remote="a" revision="main"/>
              <include name="base/integration.xml" />
              <project name="inherits" path="l/inherits"/>
              <project name="no-upstream" path="l/no-upstream" revision="dev"/>
              <!-- <project name="no-upstream" path="old" revision="old"/> -->
              <project name="floating"   remote="bb" path="l/floating" revision="main">
                <linkfile dest="x" src="y"/>
              </project>
            </manifest>
            """)
        release = """
            <?xml version="1.0" encoding="UTF-8"?>
            <manifest>
              <remote alias="repo" name="a"    fetch="https://example.com/a"/>
              <remote alias="repo" name="bb"   fetch="https://example.com/b"/>
              <default remote="a" revision="main"/>
              <include name="base/pinned.xml" />
              <project name="inherits" path="l/inherits"/>
              <project name="no-upstream" path="l/no-upstream" revision="0000000000000000000000000000000000000000"/>
              <!-- <project name="no-upstream" path="old" revision="old"/> -->
              <!-- non-ASCII comment: configuração -->
              <project name="entity" path="l/entity" revision="{'d' * 40}" upstream="a&#38;b"/>
              <project name="floating"   remote="bb" path="l/floating" revision="main">
                <linkfile dest="x" src="y"/>
              </project>
            </manifest>
            """.replace("{'d' * 40}", "d" * 40)
        self._write("torizon/fx/release.xml", release)
        self._write("frozen.xml", f"""
            <manifest>
              <project name="inherits" revision="{'a' * 40}" upstream="main" dest-branch="main"/>
              <project name="no-upstream" revision="{'b' * 40}" upstream="dev"/>
              <project name="entity" revision="{'d' * 40}" upstream="a&amp;b"/>
              <project name="floating" revision="{'c' * 40}" upstream="main"/>
            </manifest>
            """)
        _, changed = self._bump("fx", "nightly", freeze=False)
        self.assertEqual(changed, ["torizon/fx/release.xml"])
        expected = (
            textwrap.dedent(release).lstrip()
            .replace('name="inherits" path="l/inherits"/>',
                     f'name="inherits" path="l/inherits" revision="{"a" * 40}" upstream="main"/>')
            .replace('revision="0000000000000000000000000000000000000000"/>',
                     f'revision="{"b" * 40}" upstream="dev"/>')
            .replace('path="l/floating" revision="main">',
                     f'path="l/floating" revision="{"c" * 40}" upstream="main">')
        )
        self.assertEqual(self._read("torizon/fx/release.xml"), expected)


class TargetBranchNameTest(unittest.TestCase):
    def _run(self, manifest_file):
        env = dict(os.environ)
        env.pop("BUILD_MANIFEST_FILE", None)
        if manifest_file is not None:
            env["BUILD_MANIFEST_FILE"] = manifest_file
        return subprocess.run(
            [sys.executable, SCRIPT, "--productName", "torizon",
             "--pipelineType", "nightly", "get-target-branch-name",
             "--manifestBranch", "some-branch"],
            env=env, capture_output=True, text=True,
        )

    def test_includes_vendor(self):
        res = self._run("torizon/tdx/next.xml")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "bump-torizon-tdx-some-branch-nightly")

    def test_fails_without_valid_vendor(self):
        for value in ("default.xml", "./next.xml", "torizon/next.xml",
                      "/abs/torizon/tdx/next.xml", "", None):
            with self.subTest(value=value):
                self.assertNotEqual(self._run(value).returncode, 0)


if __name__ == "__main__":
    unittest.main()
