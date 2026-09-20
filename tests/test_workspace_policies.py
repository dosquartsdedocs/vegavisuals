from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from yaml import safe_dump, safe_load

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vegavisuals import Registry
from vegavisuals.errors import PolicyError
from vegavisuals.registry import LOCK_NAME, MANIFEST_NAME, RECEIPT_PATH
from tests.test_registry import DockerMock, vl_spec, write


CACHE_PATH = ".cache/vegavisuals"
EXPECTED_POLICIES = [
    {
        "path": CACHE_PATH,
        "type": "directory",
        "role": "render-cache-and-publication-recovery",
        "git": "ignored",
        "cleanup": "explicit",
    },
    {
        "path": MANIFEST_NAME,
        "type": "file",
        "role": "visualization-source-manifest",
        "git": "versioned",
        "cleanup": "never",
    },
    {
        "path": LOCK_NAME,
        "type": "file",
        "role": "managed-output-provenance",
        "git": "consumer",
        "cleanup": "explicit",
    },
    {
        "path": RECEIPT_PATH,
        "type": "file",
        "role": "companion-freshness-receipt",
        "git": "consumer",
        "cleanup": "explicit",
    },
]


def git(
    root: pathlib.Path, *args: str, env: dict[str, str] | None = None, input_data: bytes | None = None,
) -> bytes:
    environment = {key: value for key, value in (env or os.environ).items() if not key.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    return subprocess.run(
        ["git", "-C", str(root), *args], env=environment, input=input_data, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    ).stdout


def file_snapshot(path: pathlib.Path) -> tuple[object, ...]:
    metadata = path.lstat()
    content = (
        os.readlink(path) if stat.S_ISLNK(metadata.st_mode)
        else hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(metadata.st_mode)
        else None
    )
    # Reading can change atime; inode, mode, size, mtime and ctime must not change.
    return (
        metadata.st_ino, metadata.st_mode, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns, content,
    )


def tree_snapshot(root: pathlib.Path) -> dict[str, tuple[object, ...]]:
    return {str(path.relative_to(root)): file_snapshot(path) for path in sorted(root.rglob("*"))}


def git_snapshot(root: pathlib.Path, env: dict[str, str] | None = None) -> dict[str, object]:
    index = pathlib.Path(os.fsdecode(git(root, "rev-parse", "--git-path", "index", env=env)).strip())
    if not index.is_absolute():
        index = root / index
    return {
        "head": git(root, "rev-parse", "HEAD", env=env),
        "index": file_snapshot(index),
        "index_entries": git(root, "ls-files", "--stage", "-z", env=env),
        "status": git(root, "status", "--porcelain=v1", "--untracked-files=all", "-z", env=env),
        "worktrees": git(root, "worktree", "list", "--porcelain", env=env),
    }


class GitConsumerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="vegavisuals-workspace-")
        self.addCleanup(temporary.cleanup)
        self.temporary = pathlib.Path(temporary.name)
        self.root = self.temporary / "consumer with spaces"
        self.root.mkdir()
        home = self.temporary / "home"
        home.mkdir()
        self.environment = {
            **os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        }
        git(self.root, "init", "--initial-branch=consumer", env=self.environment)
        write(self.root / ".gitignore", ".cache/\n")
        write(self.root / "notes.txt", "consumer source\n")
        self.stage(".gitignore", "notes.txt")
        git(
            self.root, "-c", "user.name=Workspace Test", "-c", "user.email=workspace@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Initialize consumer fixture", env=self.environment,
        )
        runner = DockerMock()
        self.registry = Registry(self.root, runner=runner)
        self.addCleanup(self.registry.close)
        runner.renderer_contract = self.registry._renderer_contract(
            self.registry.assets / "compat/vl-convert-1.9.0.json"
        )

    def stage(self, *paths: str, force: bool = False) -> None:
        git(self.root, "add", *(["--force"] if force else []), "--", *paths, env=self.environment)

    def initialize(self) -> None:
        self.registry.initialize_project()
        self.stage(MANIFEST_NAME)

    def render_fixture(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        write(self.root / MANIFEST_NAME, safe_dump({
            "version": 1, "profile": "vl-convert-1.9.0", "family": "benizar",
            "visualizations": [{"name": "chart", "source": "chart.vl.json", "output": "public/chart.svg"}],
        }))
        self.stage(MANIFEST_NAME, "chart.vl.json")
        self.assertTrue(self.registry.render_visualizations()["ok"])
        self.assertTrue(self.registry.visualization_check()["ok"])


class WorkspacePolicyTest(GitConsumerTestCase):
    def test_checkout_package_and_dynamic_manifests_have_exact_literal_policies(self) -> None:
        manifests = [safe_load((REPO_ROOT / name).read_text(encoding="utf-8")) for name in (
            "mcp-factory.yml", "src/vegavisuals/factory/mcp-factory.yml",
        )]
        manifests.append(self.registry.factory_manifest())
        with patch("vegavisuals.registry.source_checkout", return_value=None):
            manifests.append(self.registry.factory_manifest())
        for manifest in manifests:
            with self.subTest(discovery=manifest["discovery"]):
                self.assertEqual(manifest["schema_version"], 1)
                workspace = manifest["workspace_rule"]
                self.assertEqual(workspace["binding"], "consumer")
                self.assertEqual(workspace["consumer_root"], ".")
                self.assertEqual(workspace["path_policies"], EXPECTED_POLICIES)
                for policy in workspace["path_policies"]:
                    self.assertNotRegex(policy["path"], r"[\*?\[\]${}]")

    def test_factory_check_rejects_missing_or_drifted_policies(self) -> None:
        original = safe_load((REPO_ROOT / "mcp-factory.yml").read_text(encoding="utf-8"))
        original["factory_assets"] = str(self.registry.assets)
        metadata = self.temporary / "metadata"
        metadata.mkdir()
        for index in range(len(EXPECTED_POLICIES) + 1):
            with self.subTest(index=index):
                manifest = copy.deepcopy(original)
                policies = manifest["workspace_rule"]["path_policies"]
                if index == len(policies):
                    policies.clear()
                else:
                    policies[index]["cleanup"] = "disposable"
                write(metadata / "mcp-factory.yml", safe_dump(manifest))
                with patch("vegavisuals.registry.factory_metadata_root", return_value=metadata):
                    result = self.registry.factory_check()
                self.assertFalse(result["ok"])
                self.assertIn("static factory manifest does not match dynamic workspace_rule", result["issues"])

    def test_checkout_ignore_rules_and_tracked_fixtures(self) -> None:
        for path, pattern in ((CACHE_PATH + "/", ".cache/"), (RECEIPT_PATH, RECEIPT_PATH)):
            with self.subTest(path=path):
                match = git(
                    REPO_ROOT, "check-ignore", "--no-index", "-v", "-z", "--stdin",
                    input_data=os.fsencode(path) + b"\0",
                ).split(b"\0")
                self.assertEqual(match[0], b".gitignore")
                self.assertEqual(os.fsdecode(match[2]), pattern)
                self.assertEqual(git(REPO_ROOT, "ls-files", "--", path), b"")
        self.assertEqual(
            git(REPO_ROOT, "ls-files", "--", MANIFEST_NAME, LOCK_NAME).decode().splitlines(),
            [LOCK_NAME, MANIFEST_NAME],
        )

    def test_init_preserves_ignore_and_index_and_requires_explicit_manifest_staging(self) -> None:
        before = git_snapshot(self.root, self.environment)
        ignore = file_snapshot(self.root / ".gitignore")
        first = self.registry.initialize_project()
        self.assertEqual(first["created"], [MANIFEST_NAME])
        self.assertEqual(self.registry.initialize_project()["preserved"], [MANIFEST_NAME])
        self.assertTrue((self.root / CACHE_PATH).is_dir())
        self.assertFalse((self.root / LOCK_NAME).exists())
        self.assertFalse((self.root / RECEIPT_PATH).exists())
        self.assertEqual(file_snapshot(self.root / ".gitignore"), ignore)
        after = git_snapshot(self.root, self.environment)
        for key in ("head", "index", "index_entries", "worktrees"):
            self.assertEqual(after[key], before[key])
        self.assertIn(b"?? .vegavisuals.yml\0", after["status"])

    def test_cache_preserves_source_recovery_and_down_preserves_every_consumer_path(self) -> None:
        self.render_fixture()
        authored = (self.root / MANIFEST_NAME).read_bytes()
        self.registry.initialize_project(force=True)
        archives = list((self.root / CACHE_PATH / "replaced").iterdir())
        self.assertIn(authored, [path.read_bytes() for path in archives])
        before_git = git_snapshot(self.root, self.environment)
        before_tree = tree_snapshot(self.root)
        with patch("vegavisuals.registry.shutil.which", return_value="/usr/bin/docker"), patch(
            "vegavisuals.registry._run_command", side_effect=[
                {"returncode": 0, "stdout": "a" * 12 + "\n", "stderr": ""},
                {"returncode": 0, "stdout": "", "stderr": ""},
            ],
        ) as runner:
            self.assertTrue(self.registry.down()["ok"])
        self.assertEqual(runner.call_args_list[1].args[0], ["/usr/bin/docker", "container", "rm", "--force", "a" * 12])
        self.assertEqual(git_snapshot(self.root, self.environment), before_git)
        self.assertEqual(tree_snapshot(self.root), before_tree)

    def test_lock_and_receipt_work_with_consumer_git_choices(self) -> None:
        self.render_fixture()
        for state in ("untracked", "ignored", "tracked"):
            with self.subTest(state=state):
                if state == "ignored":
                    write(self.root / ".gitignore", f".cache/\n{LOCK_NAME}\n{RECEIPT_PATH}\n")
                elif state == "tracked":
                    write(self.root / ".gitignore", ".cache/\n")
                    self.stage(LOCK_NAME, RECEIPT_PATH)
                before = git_snapshot(self.root, self.environment)
                self.assertEqual(self.registry.visualization_status()["counts"], {"fresh": 1})
                self.assertTrue(self.registry.visualization_check()["ok"])
                self.assertEqual(git_snapshot(self.root, self.environment), before)
        (self.root / LOCK_NAME).unlink()
        self.assertEqual(self.registry.visualization_status()["counts"], {"unmanaged": 1})
        output = (self.root / "public/chart.svg").read_bytes()
        with self.assertRaisesRegex(PolicyError, "unmanaged"):
            self.registry.render_visualization("chart.vl.json", "public/chart.svg", lock_name="chart")
        self.assertEqual((self.root / "public/chart.svg").read_bytes(), output)
        self.assertFalse(self.registry.visualization_check()["ok"])
        self.assertFalse(json.loads((self.root / RECEIPT_PATH).read_text())["ok"])
        self.assertTrue(self.registry.render_visualizations(confirm_replace=True)["ok"])
        self.assertTrue(self.registry.visualization_check()["ok"])
        self.assertTrue(json.loads((self.root / RECEIPT_PATH).read_text())["ok"])


@unittest.skipUnless(os.environ.get("VEGAVISUALS_FACTORY_MANAGER"), "set VEGAVISUALS_FACTORY_MANAGER for central manager integration")
class WorkspaceManagerIntegrationTest(GitConsumerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = pathlib.Path(os.environ["VEGAVISUALS_FACTORY_MANAGER"]).resolve(strict=True)
        self.catalogue = self.temporary / "catalogue"
        self.provider = self.catalogue / "vegavisuals"
        self.provider.mkdir(parents=True)
        self.marker = self.provider / "provider-command-executed"
        sentinel = self.provider / "sentinel.py"
        write(sentinel, "from pathlib import Path\nPath(__file__).with_name('provider-command-executed').touch()\nraise SystemExit(97)\n")
        text = (REPO_ROOT / "mcp-factory.yml").read_text(encoding="utf-8")
        manifest = safe_load(text)
        command = [sys.executable, str(sentinel)]
        # Keep the real manifest's indentation: the manager uses a bounded YAML
        # subset, whereas PyYAML emits indentless sequences and shared aliases.
        before, rest = text.split("\ncommands:\n", 1)
        _, after = rest.split("\nmcp:\n", 1)
        before = "\n".join(
            "  command: " + json.dumps(command) if line.startswith("  command:") else line
            for line in before.splitlines()
        )
        commands = "\n".join(f"  {name}: {json.dumps(command)}" for name in manifest["commands"])
        write(self.provider / "mcp-factory.yml", before + "\ncommands:\n" + commands + "\nmcp:\n" + after)

    def check_workspace(self, *, actual_factory: bool = False) -> dict[str, object]:
        before_git = git_snapshot(self.root, self.environment)
        before_tree = tree_snapshot(self.root)
        before_provider = tree_snapshot(self.provider)
        before_factory = git_snapshot(REPO_ROOT, self.environment)
        command = [
            sys.executable, str(self.manager), "workspace-check", "--dir",
            str(REPO_ROOT.parent if actual_factory else self.catalogue),
            "--factory", "vegavisuals", "--workspace", str(self.root), "--json",
        ]
        completed = subprocess.run(
            command, cwd=REPO_ROOT, env=self.environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
        self.assertEqual(git_snapshot(self.root, self.environment), before_git)
        self.assertEqual(tree_snapshot(self.root), before_tree)
        self.assertEqual(tree_snapshot(self.provider), before_provider)
        self.assertEqual(git_snapshot(REPO_ROOT, self.environment), before_factory)
        self.assertFalse(self.marker.exists(), "workspace-check executed a provider command")
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["inspection_ok"], payload)
        self.assertEqual(completed.returncode, 0 if payload["ok"] else 1, completed.stderr)
        self.assertEqual(payload["summary"]["declared_path_policy_count"], len(EXPECTED_POLICIES), payload)
        self.assertEqual(payload["summary"]["path_count"], len(EXPECTED_POLICIES), payload)
        return payload

    def observation(self, payload: dict[str, object], path: str) -> dict[str, object]:
        return next(item for item in payload["resolved_paths"] if item["declared_path"] == path)

    def test_direct_and_inherited_cache_ignores_with_absent_and_present_paths(self) -> None:
        for rule in (".cache/vegavisuals/", ".cache/"):
            write(self.root / ".gitignore", rule + "\n")
            for present in (False, True):
                with self.subTest(rule=rule, present=present):
                    if present:
                        self.initialize()
                    result = self.check_workspace()
                    self.assertTrue(result["ok"], result)
                    cache = self.observation(result, CACHE_PATH)
                    self.assertEqual(cache["exists"], present)
                    self.assertEqual(cache["git_state"], "ignored")
                    self.assertEqual(cache["ignore_match"]["pattern"], rule)
            # Only test setup removes fixtures; the manager is always read-only.
            (self.root / CACHE_PATH / "project.lock").unlink()
            (self.root / CACHE_PATH).rmdir()

    def test_cache_requires_ignore_even_when_absent_and_rejects_tracked_content(self) -> None:
        write(self.root / ".gitignore", "")
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    self.initialize()
                result = self.check_workspace()
                self.assertEqual(self.observation(result, CACHE_PATH)["finding_codes"], ["path-not-ignored"])
                self.assertFalse(result["ok"])
        write(self.root / ".gitignore", ".cache/\n")
        self.stage(CACHE_PATH + "/project.lock", force=True)
        result = self.check_workspace()
        self.assertFalse(result["ok"])
        cache = self.observation(result, CACHE_PATH)
        self.assertEqual(cache["finding_codes"], ["ignored-path-versioned"])
        self.assertEqual(cache["tracked_paths"], [CACHE_PATH + "/project.lock"])

    def test_versioned_manifest_absent_untracked_indexed_and_ignored(self) -> None:
        self.assertTrue(self.check_workspace()["ok"])
        self.registry.initialize_project()
        result = self.check_workspace()
        self.assertFalse(result["ok"])
        self.assertEqual(self.observation(result, MANIFEST_NAME)["finding_codes"], ["versioned-path-untracked"])
        self.stage(MANIFEST_NAME)
        result = self.check_workspace()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.observation(result, MANIFEST_NAME)["git_state"], "versioned")
        write(self.root / ".gitignore", f".cache/\n{MANIFEST_NAME}\n")
        result = self.check_workspace()
        self.assertEqual(self.observation(result, MANIFEST_NAME)["finding_codes"], ["versioned-path-ignored"])
        git(self.root, "rm", "--cached", "--", MANIFEST_NAME, env=self.environment)
        result = self.check_workspace()
        self.assertEqual(self.observation(result, MANIFEST_NAME)["finding_codes"], ["versioned-path-ignored"])

    def test_consumer_lock_and_receipt_are_informational_in_every_git_state(self) -> None:
        self.initialize()
        for state in ("absent", "untracked", "ignored", "versioned-and-ignore-matched", "versioned"):
            with self.subTest(state=state):
                if state == "untracked":
                    self.render_fixture()
                elif state == "ignored":
                    write(self.root / ".gitignore", f".cache/\n{LOCK_NAME}\n{RECEIPT_PATH}\n")
                elif state == "versioned-and-ignore-matched":
                    self.stage(LOCK_NAME, RECEIPT_PATH, force=True)
                elif state == "versioned":
                    write(self.root / ".gitignore", ".cache/\n")
                result = self.check_workspace()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["summary"]["consumer_policy_count"], 2)
                for path in (LOCK_NAME, RECEIPT_PATH):
                    observed = self.observation(result, path)
                    self.assertEqual(observed["git_state"], state)
                    self.assertEqual(observed["finding_codes"], [])

    def test_every_policy_enforces_its_declared_type(self) -> None:
        for policy in EXPECTED_POLICIES:
            with self.subTest(path=policy["path"]):
                path = self.root / policy["path"]
                if policy["type"] == "directory":
                    write(path, "wrong type")
                else:
                    path.mkdir(parents=True)
                result = self.check_workspace()
                self.assertFalse(result["ok"])
                self.assertIn("path-type-mismatch", self.observation(result, policy["path"])["finding_codes"])
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_real_checkout_workspace_check_is_read_only_with_dirty_consumer(self) -> None:
        self.render_fixture()
        write(self.root / "notes.txt", "unstaged consumer edit\n")
        write(self.root / "untracked.txt", "untracked consumer edit\n")
        result = self.check_workspace(actual_factory=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["summary"]["compliant_path_count"], 4)


if __name__ == "__main__":
    unittest.main()
