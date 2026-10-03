from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from vegavisuals import Registry, __version__
from vegavisuals import identity
from vegavisuals.cli import main
from vegavisuals.mcp_server import create_server
from tests.test_registry import DockerMock, FakeMCP

IMAGE_A = "sha256:" + "1" * 64
IMAGE_B = "sha256:" + "2" * 64
SECRET = "not-for-runtime-identity-secret-78263"
distribution_root = identity._distribution_root


class IdentityDockerMock(DockerMock):
    def __init__(self):
        super().__init__()
        self.identity_status = 0
        self.identity_override = None
        self.repo_digests = []

    def __call__(self, command, *, cwd, timeout):
        if command[:3] == ["docker", "image", "inspect"] and "{{json .Id}}" in command[4]:
            self.calls.append(command)
            value = {"image_id": self.image_id, "contract": self.renderer_contract, "repo_digests": self.repo_digests}
            return {"returncode": self.identity_status, "stdout": self.identity_override if self.identity_override is not None else json.dumps(value), "stderr": SECRET}
        return super().__call__(command, cwd=cwd, timeout=timeout)


class RuntimeIdentityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vegavisuals-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.consumer = self.root / "consumer"
        self.consumer.mkdir()
        (self.consumer / "private.txt").write_text(SECRET)
        self.package = self.root / "site" / "vegavisuals"
        shutil.copytree(identity.PACKAGE_ROOT, self.package, ignore=shutil.ignore_patterns("__pycache__"))
        self.metadata = self.package.parent / f"vegavisuals-{__version__}.dist-info"
        self.metadata.mkdir()
        self.write_metadata(__version__)
        (self.metadata / "RECORD").write_text("vegavisuals/registry.py,,\n")
        (self.metadata / "direct_url.json").write_text(json.dumps({
            "url": "https://user:" + SECRET + "@example.invalid/wheel?token=" + SECRET,
            "archive_info": {"hashes": {"sha256": "a" * 64}},
        }))
        for target, value in (("vegavisuals.identity.PACKAGE_ROOT", self.package),
                              ("vegavisuals.identity._distribution_root", lambda roots: self.metadata),
                              ("vegavisuals.registry.source_checkout", lambda: None),
                              ("vegavisuals.registry.asset_root", lambda: self.package / "assets")):
            active = patch(target, value)
            active.start()
            self.addCleanup(active.stop)
        self.runner = IdentityDockerMock()
        self.registry = self.registry_for(self.consumer)
        self.runner.renderer_contract = self.registry._renderer_contract(self.registry.assets / "compat/vl-convert-1.9.0.json")

    def write_metadata(self, version):
        (self.metadata / "METADATA").write_text(f"Metadata-Version: 2.4\nName: vegavisuals\nVersion: {version}\n\n{SECRET}\n")

    def registry_for(self, root, image=IMAGE_A):
        registry = Registry(root, runner=self.runner, renderer_image_id=image)
        self.addCleanup(registry.close)
        return registry

    def codes(self, report):
        return {item["code"] for item in report["mismatches"]}

    def test_installed_identity_is_stable_bounded_and_read_only(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.consumer.rglob("*") if p.is_file()}
        with patch.dict(os.environ, {"SECRET_TEST_TOKEN": SECRET}), patch.object(
            self.registry, "ensure_renderer", side_effect=AssertionError("identity prepared a renderer")
        ):
            first = self.registry.runtime_identity()
            second = self.registry.runtime_identity()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["instance"], second["instance"])
        self.assertEqual(first["instance"]["pid"], os.getpid())
        self.assertEqual(first["interpreter"]["executable"], sys.executable)
        self.assertEqual(first["package"]["loaded_version"], __version__)
        self.assertEqual(first["package"]["mode"], "installed")
        self.assertEqual(first["package"]["startup"]["distribution"]["archive_sha256"], "a" * 64)
        self.assertEqual(first["renderer"]["observed_image_id"], IMAGE_A)
        self.assertEqual(first["renderer"]["repo_digests"], [])
        self.assertEqual(first["workspace"], {"root": str(self.consumer), "binding_matches_startup": True})
        self.assertNotIn(SECRET, json.dumps(first))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.consumer.rglob("*") if p.is_file()})
        self.assertEqual(list(self.consumer.iterdir()), [self.consumer / "private.txt"])
        self.assertEqual(len(self.runner.calls), 2)
        self.assertTrue(all(c[:3] == ["docker", "image", "inspect"] for c in self.runner.calls))
        first["package"]["startup"]["version_file"] = "tampered response"
        self.assertEqual(self.registry.runtime_identity()["package"]["startup"]["version_file"], __version__)

    def test_loaded_version_and_startup_survive_disk_metadata_changes(self):
        initial = self.registry.runtime_identity()
        self.write_metadata("9.9.9")
        (self.package / "_version.py").write_text('__version__ = "9.9.9"\n')
        changed = self.registry.runtime_identity()
        self.assertFalse(changed["ok"])
        self.assertEqual(changed["instance"], initial["instance"])
        self.assertEqual(changed["package"]["loaded_version"], __version__)
        self.assertEqual(changed["package"]["startup"], initial["package"]["startup"])
        self.assertEqual(changed["package"]["current_disk"]["version_file"], "9.9.9")
        self.assertEqual(changed["package"]["current_disk"]["distribution"]["version"], "9.9.9")
        self.assertTrue({"version-file-differs-from-loaded", "distribution-version-differs-from-loaded", "package-files-changed", "distribution-metadata-changed"} <= self.codes(changed))

    def test_metadata_failure_and_source_text_are_not_executed_or_disclosed(self):
        sentinel = self.root / "executed"
        (self.package / "_version.py").write_text(f'__version__ = __import__("pathlib").Path({str(sentinel)!r}).write_text("executed")\n')
        (self.metadata / "direct_url.json").write_text(json.dumps({"url": SECRET, "archive_info": [SECRET]}))
        result = self.registry.runtime_identity()
        self.assertFalse(result["ok"])
        self.assertFalse(sentinel.exists())
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue({"package-files-unavailable", "distribution-metadata-unavailable"} <= self.codes(result))

    def test_missing_or_failed_inspection_never_reuses_partial_stdout(self):
        for code in (1, 124, 127):
            with self.subTest(code=code):
                self.runner.identity_status = code
                result = self.registry.runtime_identity()
                self.assertFalse(result["ok"])
                self.assertIsNone(result["renderer"]["observed_image_id"])
                self.assertIsNone(result["renderer"]["repo_digests"])
                self.assertEqual(result["renderer"]["inspection_returncode"], code)
                self.assertIn("renderer-inspection-failed", self.codes(result))
                self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(all(c[:3] == ["docker", "image", "inspect"] and c[-1] == IMAGE_A for c in self.runner.calls))

    def test_observed_id_contract_and_repo_digest_are_distinct(self):
        self.runner.image_id = IMAGE_B
        self.runner.repo_digests = ["[::1]:5000/example/renderer@sha256:" + "e" * 64,
                                   "ghcr.io/example/renderer@sha256:" + "f" * 64]
        result = self.registry.runtime_identity()
        self.assertFalse(result["ok"])
        self.assertEqual(result["renderer"]["expected_image_id"], IMAGE_A)
        self.assertEqual(result["renderer"]["observed_image_id"], IMAGE_B)
        self.assertEqual(result["renderer"]["repo_digests"], self.runner.repo_digests)
        self.assertIn("renderer-image-id-mismatch", self.codes(result))
        self.runner.image_id, self.runner.renderer_contract = IMAGE_A, "0" * 64
        result = self.registry.runtime_identity()
        self.assertFalse(result["ok"])
        self.assertEqual(result["renderer"]["observed_contract_sha256"], "0" * 64)
        self.assertIn("renderer-contract-mismatch", self.codes(result))

    def test_unexpected_inspection_data_and_secrets_are_not_reflected(self):
        for value in (SECRET, '{"image_id":"' + SECRET + '"}', json.dumps({
            "image_id": IMAGE_A, "contract": self.runner.renderer_contract,
            "repo_digests": ["https://user:" + SECRET + "@example.invalid/image@sha256:" + "f" * 64],
        })):
            with self.subTest(value=value):
                self.runner.identity_override = value
                result = self.registry.runtime_identity()
                self.assertFalse(result["ok"])
                self.assertIn("renderer-inspection-invalid", self.codes(result))
                self.assertNotIn(SECRET, json.dumps(result))

    def test_binding_selection_and_instance_are_fixed_and_independent(self):
        other = self.root / "other-consumer"
        other.mkdir()
        second = self.registry_for(other)
        first_identity = self.registry.runtime_identity()
        with patch.dict(os.environ, {"VEGAVISUALS_RENDERER_IMAGE_ID": IMAGE_B, "MCP_CONSUMER_WORKSPACE": str(other)}):
            again = self.registry.runtime_identity()
        self.assertEqual(again["instance"], first_identity["instance"])
        self.assertEqual(again["workspace"]["root"], str(self.consumer))
        self.assertEqual(again["renderer"]["requested_image"], IMAGE_A)
        self.assertNotEqual(second.runtime_identity()["instance"]["id"], again["instance"]["id"])
        self.consumer.rename(self.root / "retired-root")
        self.consumer.mkdir()
        replaced = self.registry.runtime_identity()
        self.assertFalse(replaced["ok"])
        self.assertFalse(replaced["workspace"]["binding_matches_startup"])
        self.assertIn("workspace-binding-changed", self.codes(replaced))

    def test_normal_mode_and_invalid_profile_do_not_prepare(self):
        with patch.dict(os.environ, {}, clear=True):
            normal = self.registry_for(self.consumer, image=None)
        result = normal.runtime_identity()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["renderer"]["selection_mode"], "profile")
        self.assertIsNone(result["renderer"]["expected_image_id"])
        self.assertEqual(result["renderer"]["requested_image"], "vegavisuals/render:vl-convert-1.9.0")
        self.runner.calls.clear()
        invalid = normal.runtime_identity("unknown-profile")
        self.assertFalse(invalid["ok"])
        self.assertIn("renderer-profile-unavailable", self.codes(invalid))
        self.assertEqual(self.runner.calls, [])

    def test_cli_tool_and_resource_share_versioned_failure_and_instance_data(self):
        with patch("vegavisuals.cli.Registry", return_value=self.registry), patch("vegavisuals.cli._print") as output:
            self.assertEqual(main(["runtime-identity"]), 0)
            cli_result = output.call_args.args[0]
        server = create_server(self.registry, FakeMCP)
        tool_result = server.tools["runtime_identity"]()
        resource_result = json.loads(server.resources["vegavisuals://runtime/identity"]())
        self.assertEqual(cli_result["instance"], tool_result["instance"])
        self.assertEqual(tool_result["instance"], resource_result["instance"])
        self.runner.identity_status = 1
        with patch("vegavisuals.cli.Registry", return_value=self.registry), patch("vegavisuals.cli._print") as output:
            self.assertEqual(main(["runtime-identity"]), 1)
            self.assertEqual(output.call_args.args[0]["schema_version"], 1)
        self.assertFalse(server.tools["runtime_identity"]()["ok"])
        self.assertFalse(json.loads(server.resources["vegavisuals://runtime/identity"]())["ok"])

    def test_source_revision_is_data_only_and_ignores_git_environment(self):
        source = self.root / "source"
        git = source / ".git"
        (git / "refs/heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs/heads/main").write_text("a" * 40 + "\n")
        with patch.dict(os.environ, {"GIT_DIR": "/does-not-exist"}), patch("subprocess.run", side_effect=AssertionError("identity ran Git")):
            self.assertEqual(identity._source_revision(source), "a" * 40)
            (git / "refs/heads/main").unlink()
            (git / "packed-refs").write_text("b" * 40 + " refs/heads/main\n")
            self.assertEqual(identity._source_revision(source), "b" * 40)
            (git / "HEAD").write_text("ref: refs/../../private.txt\n")
            self.assertIsNone(identity._source_revision(source))

    def test_diagnostic_inventory_covers_all_packaged_files(self):
        actual = {p.relative_to(self.package).as_posix() for p in self.package.rglob("*") if p.is_file()}
        self.assertEqual(set(identity.PACKAGE_FILES), actual)

    def test_changed_process_and_missing_distribution_remain_explicit(self):
        first = self.registry.runtime_identity()
        with patch("vegavisuals.identity.os.getpid", return_value=first["instance"]["pid"] + 1):
            changed = self.registry.runtime_identity()
        self.assertIn("process-changed", self.codes(changed))
        self.assertEqual(changed["instance"]["startup_pid"], first["instance"]["pid"])
        with patch("vegavisuals.identity._distribution_root", return_value=None):
            missing = self.registry.runtime_identity()
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["package"]["mode"], "installed")
        self.assertIsNone(missing["package"]["current_disk"]["distribution"])
        self.assertIn("distribution-metadata-changed", self.codes(missing))

    def test_metadata_symlink_is_not_read(self):
        path = self.metadata / "METADATA"
        path.unlink()
        path.symlink_to(self.consumer / "private.txt")
        result = self.registry.runtime_identity()
        self.assertFalse(result["ok"])
        self.assertIn("distribution-metadata-unavailable", self.codes(result))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_consumer_metadata_cannot_shadow_the_loaded_installation(self):
        shadow = self.consumer / "vegavisuals-9.9.9.dist-info"
        shadow.mkdir()
        (shadow / "METADATA").write_text("Name: vegavisuals\nVersion: 9.9.9\n")
        with patch.object(sys, "path", [str(self.consumer), *sys.path]):
            self.assertEqual(distribution_root(self.registry._identity.distribution_roots), self.metadata)
