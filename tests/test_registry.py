from __future__ import annotations

import copy
import csv
import errno
import gc
import hashlib
import inspect
import json
import multiprocessing
import os
import pathlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from yaml import safe_dump, safe_load

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import vegavisuals.registry as registry_module
from vegavisuals import __version__
from vegavisuals.cli import build_parser, dispatch, main
from vegavisuals.errors import ManifestError, PolicyError, RenderError, ValidationError
from vegavisuals.mcp_server import RESOURCE_URIS, TOOL_NAMES, create_server
from vegavisuals.registry import (
    CONTAINER_LABEL,
    CONTAINER_WORKSPACE_LABEL,
    DEFAULT_RELEASE,
    LOCK_NAME,
    LOCK_VERSION,
    MAX_LOCK_BYTES,
    RECEIPT_PATH,
    Registry,
    workspace_id,
)


def write(path: pathlib.Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def docker_mount_source(command: list[str], target: str) -> pathlib.Path:
    for index, value in enumerate(command):
        if value != "--mount":
            continue
        fields = next(csv.reader([command[index + 1]]))
        options = dict(field.split("=", 1) for field in fields if "=" in field)
        if options.get("target") == target:
            return pathlib.Path(options["source"])
    raise AssertionError(f"mount target not found: {target}")


def vl_spec(url: str | None = None, value: int = 1) -> str:
    data = {"url": url} if url is not None else {"values": [{"x": "A", "y": value}]}
    return json.dumps(
        {
            "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
            "data": data,
            "mark": "bar",
            "encoding": {
                "x": {"field": "x", "type": "nominal"},
                "y": {"field": "y", "type": "quantitative"},
            },
        }
    )


def vg_spec(value: int = 1) -> str:
    return json.dumps(
        {
            "$schema": "https://vega.github.io/schema/vega/v6.json",
            "width": 100,
            "height": 80,
            "data": [{"name": "table", "values": [{"x": value}]}],
            "marks": [{"type": "rect"}],
        }
    )


def png_artifact() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    scanline = b"\x00\x00\x00\x00\xff"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline))
        + chunk(b"IEND", b"")
    )


def pdf_artifact() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


class DockerMock:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.render_commands: list[list[str]] = []
        self.rendered_specs: list[dict[str, object]] = []
        self.renderer_contract = ""
        self.image_id = "sha256:" + "1" * 64

    def __call__(self, command: list[str], *, cwd: pathlib.Path, timeout: int) -> dict[str, object]:
        self.calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            if not self.renderer_contract:
                return {"command": command, "returncode": 1, "stdout": "", "stderr": "missing"}
            return {
                "command": command,
                "returncode": 0,
                "stdout": f"{self.image_id}\t{self.renderer_contract}\n",
                "stderr": "",
            }
        if command[:2] == ["docker", "build"]:
            contract_prefix = "io.vegavisuals.renderer-contract="
            self.renderer_contract = next(
                value.removeprefix(contract_prefix) for value in command if value.startswith(contract_prefix)
            )
            return {"command": command, "returncode": 0, "stdout": "built", "stderr": ""}
        if command[:4] == ["docker", "container", "rm", "--force"]:
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if command[:2] != ["docker", "run"]:
            raise AssertionError(f"unexpected command: {command}")
        self.render_commands.append(command)
        staging = docker_mount_source(command, "/output")
        source_name = pathlib.Path(command[command.index("--input") + 1]).name
        output_name = pathlib.Path(command[command.index("--output") + 1]).name
        output_format = command[command.index("--format") + 1]
        self.rendered_specs.append(json.loads((staging / source_name).read_text(encoding="utf-8")))
        if output_format == "svg":
            data = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        elif output_format == "png":
            data = png_artifact()
        else:
            data = pdf_artifact()
        (staging / output_name).write_bytes(data)
        return {"command": command, "returncode": 0, "stdout": '{"ok":true}', "stderr": ""}


class FailingDockerMock(DockerMock):
    def __call__(self, command: list[str], *, cwd: pathlib.Path, timeout: int) -> dict[str, object]:
        if command[:2] == ["docker", "run"]:
            self.calls.append(command)
            self.render_commands.append(command)
            return {"command": command, "returncode": 1, "stdout": "", "stderr": "render failed"}
        return super().__call__(command, cwd=cwd, timeout=timeout)


class BarrierDockerMock(DockerMock):
    def __init__(self, barrier: object, renderer_contract: str) -> None:
        super().__init__()
        self.barrier = barrier
        self.renderer_contract = renderer_contract

    def __call__(self, command: list[str], *, cwd: pathlib.Path, timeout: int) -> dict[str, object]:
        if command[:2] == ["docker", "run"]:
            self.barrier.wait(timeout=10)  # type: ignore[attr-defined]
        return super().__call__(command, cwd=cwd, timeout=timeout)


class SwapParentDockerMock(DockerMock):
    def __init__(self, project: pathlib.Path, outside: pathlib.Path) -> None:
        super().__init__()
        self.project = project
        self.outside = outside

    def __call__(self, command: list[str], *, cwd: pathlib.Path, timeout: int) -> dict[str, object]:
        result = super().__call__(command, cwd=cwd, timeout=timeout)
        if command[:2] == ["docker", "run"]:
            parent = self.project / "out"
            parent.rmdir()
            parent.symlink_to(self.outside, target_is_directory=True)
        return result


class CallbackDockerMock(DockerMock):
    def __init__(self, callback):  # type: ignore[no-untyped-def]
        super().__init__()
        self.callback = callback

    def __call__(self, command: list[str], *, cwd: pathlib.Path, timeout: int) -> dict[str, object]:
        result = super().__call__(command, cwd=cwd, timeout=timeout)
        if command[:2] == ["docker", "run"]:
            self.callback()
        return result


class StagedOutputSwapDockerMock(DockerMock):
    def __init__(self, replacement: pathlib.Path | None) -> None:
        super().__init__()
        self.replacement = replacement

    def __call__(self, command: list[str], *, cwd: pathlib.Path, timeout: int) -> dict[str, object]:
        result = super().__call__(command, cwd=cwd, timeout=timeout)
        if command[:2] == ["docker", "run"]:
            staging = docker_mount_source(command, "/output")
            output = staging / pathlib.Path(command[command.index("--output") + 1]).name
            output.unlink()
            if self.replacement is None:
                os.mkfifo(output)
            else:
                output.symlink_to(self.replacement)
        return result


class PublicationCallbackRegistry(Registry):
    def __init__(self, project_root: pathlib.Path, runner: DockerMock, callback) -> None:  # type: ignore[no-untyped-def]
        super().__init__(project_root, runner=runner)
        self.callback = callback

    def _publish_artifact_and_lock(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.callback()
        return super()._publish_artifact_and_lock(*args, **kwargs)


class LockPublicationFailRegistry(Registry):
    def _replace_project_bytes(  # type: ignore[no-untyped-def]
        self, relative: str, data: bytes, *, mode: int = 0o644, **kwargs
    ):
        if relative == LOCK_NAME:
            raise OSError("simulated lock publication failure")
        return super()._replace_project_bytes(relative, data, mode=mode, **kwargs)


class LockPublicationCallbackFailRegistry(Registry):
    def __init__(self, project_root: pathlib.Path, runner: DockerMock, callback) -> None:  # type: ignore[no-untyped-def]
        super().__init__(project_root, runner=runner)
        self.callback = callback

    def _replace_project_bytes(  # type: ignore[no-untyped-def]
        self, relative: str, data: bytes, *, mode: int = 0o644, **kwargs
    ):
        if relative == LOCK_NAME:
            self.callback()
            raise OSError("simulated lock publication failure")
        return super()._replace_project_bytes(relative, data, mode=mode, **kwargs)


class LockPublicationInterruptRegistry(Registry):
    def _replace_project_bytes(  # type: ignore[no-untyped-def]
        self, relative: str, data: bytes, *, mode: int = 0o644, **kwargs
    ):
        if relative == LOCK_NAME:
            raise KeyboardInterrupt()
        return super()._replace_project_bytes(relative, data, mode=mode, **kwargs)


class OutputPublicationInterruptRegistry(Registry):
    def _replace_project_bytes(  # type: ignore[no-untyped-def]
        self, relative: str, data: bytes, *, mode: int = 0o644, **kwargs
    ):
        publication = super()._replace_project_bytes(relative, data, mode=mode, **kwargs)
        if relative != LOCK_NAME:
            raise KeyboardInterrupt()
        return publication


class CacheMetadataFailRegistry(Registry):
    def _replace_project_bytes(  # type: ignore[no-untyped-def]
        self, relative: str, data: bytes, *, mode: int = 0o644, **kwargs
    ):
        if relative.startswith(".cache/vegavisuals/text/") and relative.endswith(".json"):
            raise OSError("simulated cache metadata publication failure")
        return super()._replace_project_bytes(relative, data, mode=mode, **kwargs)


def concurrent_render_process(
    root: str,
    name: str,
    barrier: object,
    renderer_contract: str,
    results: object,
) -> None:
    try:
        runner = BarrierDockerMock(barrier, renderer_contract)
        Registry(root, runner=runner).render_visualization(f"{name}.vl.json", f"out/{name}.svg")
        results.put(None)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - asserted in parent process
        results.put(f"{type(exc).__name__}: {exc}")  # type: ignore[attr-defined]


def concurrent_inline_process(
    root: str,
    name: str,
    value: int,
    barrier: object,
    renderer_contract: str,
    results: object,
) -> None:
    try:
        runner = BarrierDockerMock(barrier, renderer_contract)
        Registry(root, runner=runner).render_visualization_text(vl_spec(value=value), output_path=f"out/{name}.svg")
        results.put(None)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - asserted in parent process
        results.put(f"{type(exc).__name__}: {exc}")  # type: ignore[attr-defined]


class TemporaryProject(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.runner = DockerMock()
        self.registry = Registry(self.root, runner=self.runner)
        self.runner.renderer_contract = self.registry._renderer_contract(
            self.registry.assets / "compat" / "vl-convert-1.9.0.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()


class InventoryTest(TemporaryProject):
    def test_profiles_themes_and_factory_assets_are_data(self) -> None:
        compatibility = self.registry.compatibility_status()
        self.assertTrue(compatibility["ok"])
        profile = compatibility["profile"]
        self.assertEqual(profile["vl_convert_python"], "1.9.0.post1")
        self.assertEqual(profile["vega_runtime"], "6.2.0")
        self.assertEqual(profile["vega_lite"]["default"], "6.4")
        self.assertEqual(profile["network_policy"]["runtime"], "none")
        self.assertEqual(profile["formats"], ["svg", "png", "pdf"])
        self.assertEqual(self.registry.theme_inventory()["families"][0]["family"], "benizar")
        self.assertTrue(self.registry.factory_check()["ok"])

    def test_factory_manifest_exposes_exact_mcp_contract(self) -> None:
        manifest = self.registry.factory_manifest()
        self.assertEqual(manifest["name"], "vegavisuals")
        self.assertEqual(manifest["license"], "GPL-3.0-only")
        self.assertEqual(manifest["repository"], "https://github.com/dosquartsdedocs/vegavisuals")
        self.assertEqual(manifest["workspace_rule"]["binding"], "consumer")
        self.assertTrue(manifest["mcp"]["consumer_root_fixed_at_startup"])
        self.assertEqual(tuple(manifest["mcp"]["tools"]), TOOL_NAMES)
        self.assertEqual(tuple(manifest["mcp"]["resources"]), RESOURCE_URIS)

        static = safe_load((REPO_ROOT / "mcp-factory.yml").read_text(encoding="utf-8"))
        self.assertEqual(static["schema_version"], manifest["schema_version"])
        self.assertEqual(static["version"], __version__)
        self.assertEqual(static["kind"], manifest["kind"])
        self.assertEqual(static["description"], manifest["description"])
        self.assertEqual(static["license"], manifest["license"])
        self.assertEqual(static["repository"], manifest["repository"])
        self.assertEqual(tuple(static["mcp"]["required_tools"]), TOOL_NAMES)
        self.assertEqual(tuple(static["mcp"]["resources"]), RESOURCE_URIS)
        self.assertEqual(static["contracts"], manifest["contracts"])
        self.assertEqual(static["runtime"], manifest["runtime"])
        self.assertEqual(static["transport"], manifest["transport"])
        self.assertEqual(static["workspace_rule"], manifest["workspace_rule"])
        self.assertEqual(static["commands"], manifest["commands"])
        self.assertEqual(static["discovery"], manifest["discovery"])
        self.assertEqual(static["release"], manifest["release"])
        self.assertEqual(static["defaults"], manifest["defaults"])
        for key in ("server_name", "transport", "consumer_root_fixed_at_startup"):
            self.assertEqual(static["mcp"][key], manifest["mcp"][key])
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        for variable in ("PROJECT", "PROFILE", "FAMILY", "ENGINE", "FORMAT", "INPUT", "OUTPUT", "CURDIR"):
            self.assertNotIn(f"$({variable})", makefile)

    def test_installed_manifest_uses_the_current_python_without_a_factory_makefile(self) -> None:
        with patch("vegavisuals.registry.source_checkout", return_value=None):
            manifest = self.registry.factory_manifest()
            check = self.registry.factory_check()
        command = manifest["transport"]["command"]
        self.assertEqual(command[:3], [sys.executable, "-m", "vegavisuals.cli"])
        self.assertEqual(command[3:], ["mcp", "serve"])
        self.assertEqual(
            manifest["transport"]["env"],
            {"MCP_CONSUMER_WORKSPACE": "${workspaceFolder}"},
        )
        self.assertNotIn("make", command)
        self.assertEqual(manifest["commands"]["init"][-1], "init")
        self.assertEqual(manifest["commands"]["check"][-1], "factory-lifecycle-check")
        self.assertEqual(manifest["commands"]["tests"][-1], "self-test")
        self.assertEqual(manifest["commands"]["smoke"][-1], "mcp-smoke")
        self.assertEqual(manifest["commands"]["down"][-1], "down")
        self.assertNotIn("down_all", manifest["commands"])
        for name in ("build", "check", "tests", "smoke", "client_config", "manifest"):
            self.assertNotIn("${workspaceFolder}", manifest["commands"][name])
        for name in ("init", "down", "serve", "render", "render_all"):
            self.assertIn("${workspaceFolder}", manifest["commands"][name])
        self.assertFalse(manifest["discovery"]["checkout_required_for_make_lifecycle"])
        package_manifest = safe_load(
            (REPO_ROOT / "src/vegavisuals/factory/mcp-factory.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(package_manifest["commands"]), set(manifest["commands"]))
        self.assertFalse(package_manifest["discovery"]["checkout_required_for_make_lifecycle"])
        serialized = json.dumps(package_manifest)
        for forbidden in ("${factoryRoot}", "scripts/factory-launcher", "src/vegavisuals/assets", '"make"'):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(check["ok"], check)

    def test_checkout_manifest_scopes_only_project_operations(self) -> None:
        manifest = self.registry.factory_manifest()
        launcher = ["bash", "${factoryRoot}/scripts/factory-launcher"]

        self.assertEqual(manifest["commands"]["build"], ["make", "mcp-build"])
        self.assertEqual(manifest["commands"]["check"], ["make", "mcp-check"])
        self.assertEqual(manifest["commands"]["smoke"], ["make", "mcp-smoke"])
        self.assertEqual(manifest["commands"]["manifest"], [*launcher, "manifest"])
        self.assertNotIn("client_config", manifest["commands"])
        self.assertNotIn("down_all", manifest["commands"])
        self.assertEqual(
            manifest["commands"]["down"],
            [*launcher, "down", "${workspaceFolder}"],
        )
        self.assertEqual(
            manifest["transport"]["command"],
            ["make", "--no-print-directory", "-C", "${factoryRoot}", "mcp-stdio"],
        )
        self.assertEqual(
            manifest["transport"]["env"],
            {"MCP_CONSUMER_WORKSPACE": "${workspaceFolder}"},
        )

    def test_registry_pins_an_absolute_consumer_root_before_cwd_changes(self) -> None:
        with tempfile.TemporaryDirectory() as other_tmp:
            previous = pathlib.Path.cwd()
            pinned: Registry | None = None
            try:
                os.chdir(self.root.parent)
                pinned = Registry(self.root.name, runner=DockerMock())
                os.chdir(other_tmp)
                pinned.initialize_project()
            finally:
                os.chdir(previous)

            assert pinned is not None
            self.assertEqual(pinned.project_root, self.root.resolve())
            self.assertTrue(pinned.project_root.is_absolute())
            self.assertTrue((self.root / ".vegavisuals.yml").is_file())
            self.assertFalse((pathlib.Path(other_tmp) / ".vegavisuals.yml").exists())
            pinned.close()

    def test_manifest_and_client_config_ignore_a_nonexistent_workspace_placeholder(self) -> None:
        placeholder = "${workspaceFolder}"
        self.assertFalse(pathlib.Path(placeholder).exists())

        with patch("vegavisuals.cli._print") as output:
            self.assertEqual(main(["--project", placeholder, "factory-manifest"]), 0)
            self.assertTrue(output.call_args.args[0]["ok"])
        with patch("vegavisuals.cli._print") as output:
            self.assertEqual(main(["--project", placeholder, "mcp", "client-config"]), 0)
            server = output.call_args.args[0]["mcpServers"]["vegavisuals"]
            self.assertNotIn(placeholder, server["args"])
            self.assertEqual(server["env"], {"MCP_CONSUMER_WORKSPACE": placeholder})

    def test_down_removes_only_valid_factory_container_ids(self) -> None:
        listed = {
            "command": ["docker"],
            "returncode": 0,
            "stdout": "a" * 12 + "\n" + "b" * 64 + "\n",
            "stderr": "",
        }
        removed = {"command": ["docker"], "returncode": 0, "stdout": "", "stderr": ""}
        with patch("vegavisuals.registry.shutil.which", return_value="/usr/bin/docker"), patch(
            "vegavisuals.registry._run_command", side_effect=[listed, removed]
        ) as runner:
            result = self.registry.down()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["containers"], ["a" * 12, "b" * 64])
        self.assertEqual(
            runner.call_args_list[0].args[0],
            [
                "/usr/bin/docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                "label=io.context.mcp-factory=vegavisuals",
                "--filter",
                f"label=io.context.mcp-factory.workspace={workspace_id(self.root)}",
            ],
        )
        self.assertEqual(
            runner.call_args_list[1].args[0],
            ["/usr/bin/docker", "container", "rm", "--force", "a" * 12, "b" * 64],
        )
        self.assertEqual(result["workspace"], workspace_id(self.root))
        self.assertFalse(result["all_workspaces"])

    def test_down_all_requires_explicit_factory_wide_cleanup(self) -> None:
        listed = {
            "command": ["docker"],
            "returncode": 0,
            "stdout": "a" * 12 + "\n",
            "stderr": "",
        }
        removed = {"command": ["docker"], "returncode": 0, "stdout": "", "stderr": ""}
        with patch("vegavisuals.registry.shutil.which", return_value="/usr/bin/docker"), patch(
            "vegavisuals.registry._run_command", side_effect=[listed, removed]
        ) as runner:
            result = self.registry.down_all()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["all_workspaces"])
        self.assertIsNone(result["workspace"])
        self.assertEqual(
            runner.call_args_list[0].args[0],
            [
                "/usr/bin/docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                "label=io.context.mcp-factory=vegavisuals",
            ],
        )

    def test_down_rejects_malformed_container_ids_without_removing(self) -> None:
        listed = {
            "command": ["docker"],
            "returncode": 0,
            "stdout": "not-a-container\n",
            "stderr": "",
        }
        with patch("vegavisuals.registry.shutil.which", return_value="/usr/bin/docker"), patch(
            "vegavisuals.registry._run_command", return_value=listed
        ) as runner:
            result = self.registry.down()

        self.assertFalse(result["ok"])
        self.assertEqual(runner.call_count, 1)

    def test_mcp_smoke_reports_an_unavailable_optional_dependency_as_json(self) -> None:
        arguments = build_parser().parse_args(["mcp-smoke"])
        real_import = __import__

        for error in (
            ModuleNotFoundError("No module named 'mcp'"),
            ImportError("cannot import name 'ClientSession' from 'mcp'"),
        ):
            with self.subTest(error=type(error).__name__):

                def import_unavailable_mcp(name, *args, **kwargs):  # type: ignore[no-untyped-def]
                    if name == "mcp" or name.startswith("mcp."):
                        raise error
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=import_unavailable_mcp), patch(
                    "vegavisuals.cli._print"
                ) as output:
                    status = dispatch(arguments, self.registry)

                self.assertEqual(status, 1)
                payload = output.call_args.args[0]
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["type"], type(error).__name__)

    def test_initialize_is_idempotent_and_force_is_explicit(self) -> None:
        first = self.registry.initialize_project()
        manifest = self.root / ".vegavisuals.yml"
        self.assertEqual(first["created"], [".vegavisuals.yml"])
        self.assertEqual(safe_load(manifest.read_text(encoding="utf-8"))["visualizations"], [])

        write(manifest, "consumer owned\n")
        preserved = self.registry.initialize_project()
        self.assertEqual(preserved["preserved"], [".vegavisuals.yml"])
        self.assertEqual(manifest.read_text(encoding="utf-8"), "consumer owned\n")

        replaced = self.registry.initialize_project(force=True)
        self.assertTrue(replaced["force"])
        self.assertEqual(safe_load(manifest.read_text(encoding="utf-8"))["version"], 1)

    def test_initialize_rejects_a_symlink_manifest(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.yml"
        write(outside, "outside\n")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.root / ".vegavisuals.yml").symlink_to(outside)

        with self.assertRaisesRegex(PolicyError, "must not be a symlink"):
            self.registry.initialize_project(force=True)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_default_client_config_uses_the_installed_interpreter(self) -> None:
        server = self.registry.client_config()["mcpServers"]["vegavisuals"]
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"][:2], ["-m", "vegavisuals.cli"])
        self.assertNotIn(str("${workspaceFolder}"), server["args"])
        self.assertEqual(server["env"], {"MCP_CONSUMER_WORKSPACE": "${workspaceFolder}"})

    def test_cli_uses_the_literal_consumer_environment_as_its_default_root(self) -> None:
        project = "/tmp/consumer $value $(touch never) `touch never-either`"
        with patch.dict(os.environ, {"MCP_CONSUMER_WORKSPACE": project}):
            args = build_parser().parse_args(["mcp", "serve"])
        self.assertEqual(args.project, project)

    def test_package_release_and_update_are_non_mutating(self) -> None:
        with patch("vegavisuals.registry.source_checkout", return_value=None):
            release = self.registry.release_status(f"v{__version__}")
            update = self.registry.update_factory()
        self.assertTrue(release["ok"])
        self.assertEqual(release["source"], "package")
        self.assertTrue(update["dry_run"])
        self.assertIn("pip", update["command"])

    def test_checkout_update_refuses_a_dirty_tree(self) -> None:
        dirty = {"command": ["git"], "returncode": 0, "stdout": " M README.md\n", "stderr": ""}
        with (
            patch("vegavisuals.registry.source_checkout", return_value=REPO_ROOT),
            patch("vegavisuals.registry._run_command", return_value=dirty),
        ):
            result = self.registry.update_factory()
        self.assertFalse(result["ok"])
        self.assertIn("Refusing", result["message"])

    def test_checkout_release_requires_exact_clean_head(self) -> None:
        results = [
            {"command": ["git"], "returncode": 0, "stdout": "a" * 40 + "\n", "stderr": ""},
            {"command": ["git"], "returncode": 0, "stdout": "b" * 40 + "\n", "stderr": ""},
            {"command": ["git"], "returncode": 0, "stdout": f"v{__version__}\n", "stderr": ""},
            {"command": ["git"], "returncode": 0, "stdout": "", "stderr": ""},
        ]
        with (
            patch("vegavisuals.registry.source_checkout", return_value=REPO_ROOT),
            patch("vegavisuals.registry._run_command", side_effect=results),
        ):
            release = self.registry.release_status(f"v{__version__}")
        self.assertFalse(release["ok"])
        self.assertFalse(release["current_matches_release"])

    def test_install_check_rejects_expected_version_text_from_a_failing_cli(self) -> None:
        results = [
            {
                "command": ["vegavisuals"],
                "returncode": 1,
                "stdout": f"vegavisuals {__version__}\n",
                "stderr": "failed",
            },
            {
                "command": [sys.executable],
                "returncode": 0,
                "stdout": "1.29.0\n",
                "stderr": "",
            },
        ]
        with (
            patch("vegavisuals.registry.shutil.which", return_value="/tmp/vegavisuals"),
            patch("vegavisuals.registry._entrypoint_python", return_value=sys.executable),
            patch("vegavisuals.registry._run_command", side_effect=results),
        ):
            result = self.registry.install_check("vegavisuals")
        self.assertFalse(result["ok"])
        self.assertFalse(result["cli_version_matches"])

    def test_codex_install_dry_run_uses_startup_fixed_project(self) -> None:
        result = self.registry.install_codex_mcp(dry_run=True, codex_bin="codex-test")
        self.assertTrue(result["ok"])
        self.assertIn(f"MCP_CONSUMER_WORKSPACE={self.root}", result["add"])
        self.assertIn("-m", result["add"])
        self.assertNotIn("--project", result["add"])

    def test_codex_install_preserves_a_different_registration(self) -> None:
        existing = {
            "command": ["codex", "mcp", "list"],
            "returncode": 0,
            "stdout": json.dumps(
                [
                    {
                        "name": "vegavisuals",
                        "enabled": False,
                        "transport": {
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "vegavisuals.cli",
                                "--project",
                                str(self.root),
                                "mcp",
                                "serve",
                            ],
                            "env": {},
                        },
                    }
                ]
            ),
            "stderr": "",
        }
        with (
            patch("vegavisuals.registry.shutil.which", return_value="/usr/bin/codex"),
            patch("vegavisuals.registry._run_command", return_value=existing) as run,
        ):
            result = self.registry.install_codex_mcp()
        self.assertFalse(result["ok"])
        self.assertIn("refusing", result["message"])
        run.assert_called_once()

    def test_codex_install_recognizes_the_same_environment_registration(self) -> None:
        existing = {
            "command": ["codex", "mcp", "list"],
            "returncode": 0,
            "stdout": json.dumps(
                [
                    {
                        "name": "vegavisuals",
                        "enabled": True,
                        "transport": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "vegavisuals.cli", "mcp", "serve"],
                            "env": {"MCP_CONSUMER_WORKSPACE": str(self.root)},
                        },
                    }
                ]
            ),
            "stderr": "",
        }
        with (
            patch("vegavisuals.registry.shutil.which", return_value="/usr/bin/codex"),
            patch("vegavisuals.registry._run_command", return_value=existing) as run,
        ):
            result = self.registry.install_codex_mcp()
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_configured"])
        run.assert_called_once()

    def test_root_and_packaged_dockerfiles_are_semantically_synchronized(self) -> None:
        root = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        packaged = (REPO_ROOT / "src/vegavisuals/assets/Dockerfile").read_text(encoding="utf-8")
        self.assertIn('org.opencontainers.image.licenses="GPL-3.0-only"', root)
        self.assertIn('org.opencontainers.image.source="https://github.com/dosquartsdedocs/vegavisuals"', root)
        root = root.replace(
            "COPY src/vegavisuals/assets/docker/worker.py /opt/vegavisuals/worker.py",
            "COPY docker/worker.py /opt/vegavisuals/worker.py",
        ).replace(
            "COPY src/vegavisuals/assets/themes/ /opt/vegavisuals/themes/",
            "COPY themes/ /opt/vegavisuals/themes/",
        )
        self.assertEqual(root, packaged)

    def test_factory_check_validates_every_profile_theme_and_token(self) -> None:
        assets = self.root / "assets"
        shutil.copytree(self.registry.assets, assets)
        write(assets / "compat/broken.json", '{"id":"broken"}')
        registry = Registry(self.root)
        registry.assets = assets
        result = registry.factory_check()
        self.assertFalse(result["ok"])
        self.assertTrue(any("broken" in issue for issue in result["issues"]))


class PathAndPolicyTest(TemporaryProject):
    def test_source_and_output_must_stay_in_project(self) -> None:
        outside = pathlib.Path(self.temporary.name).parent / "outside.vl.json"
        write(outside, vl_spec())
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        with self.assertRaises(PolicyError):
            self.registry.validate_visualization(str(outside))
        write(self.root / "chart.vl.json", vl_spec())
        with self.assertRaises(PolicyError):
            self.registry.render_visualization("chart.vl.json", "../chart.svg", dry_run=True)

    def test_local_data_is_staged_inline_and_fingerprinted(self) -> None:
        write(self.root / "charts" / "chart.vl.json", vl_spec("charts/data/table.csv"))
        write(self.root / "charts" / "data" / "table.csv", "x,y\nA,1\n")
        result = self.registry.render_visualization("charts/chart.vl.json", "out/chart.svg")
        self.assertTrue(result["ok"])
        self.assertEqual(result["dependencies"][0]["path"], "charts/data/table.csv")
        staged_data = self.runner.rendered_specs[0]["data"]
        self.assertNotIn("url", staged_data)
        self.assertEqual(staged_data["format"], {"type": "csv"})
        self.assertEqual(staged_data["values"], "x,y\nA,1\n")

    def test_repeated_local_data_expansion_is_bounded_before_serialization(self) -> None:
        assets = self.root / "assets"
        shutil.copytree(self.registry.assets, assets)
        profile_path = assets / "compat/vl-convert-1.9.0.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["runtime_limits"]["max_prepared_spec_bytes"] = 1024
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        spec = json.loads(vg_spec())
        spec["data"] = [{"name": f"table-{index}", "url": "table.csv"} for index in range(12)]
        write(self.root / "chart.vg.json", json.dumps(spec))
        write(self.root / "table.csv", "x,y\n" + "A,1\n" * 30)
        registry = Registry(self.root, runner=self.runner)
        registry.assets = assets
        self.runner.renderer_contract = registry._renderer_contract(profile_path)

        with self.assertRaisesRegex(ValidationError, "after local data inlining"):
            registry.validate_visualization("chart.vg.json")

    def test_project_hash_rejects_oversized_files_before_reading(self) -> None:
        write(self.root / "large.bin", b"x" * 1024)

        with self.assertRaisesRegex(ValidationError, "exceeds 16 bytes"):
            self.registry._project_file_hash("large.bin", max_bytes=16)

    def test_directory_fsync_failure_removes_new_publication(self) -> None:
        real_fsync = os.fsync

        def fail_directory_fsync(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("simulated directory fsync failure")
            real_fsync(descriptor)

        with patch("vegavisuals.registry.os.fsync", side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(RenderError, "simulated directory fsync failure"):
                self.registry._replace_project_bytes("new.txt", b"new")

        self.assertFalse((self.root / "new.txt").exists())

    def test_temporary_name_failure_does_not_leak_transaction_descriptor(self) -> None:
        gc.collect()
        before = len(list(pathlib.Path("/proc/self/fd").iterdir()))

        with patch("vegavisuals.registry.secrets.token_hex", side_effect=RuntimeError("token failure")):
            with self.assertRaisesRegex(RuntimeError, "token failure"):
                self.registry._replace_project_bytes("new.txt", b"new")

        gc.collect()
        self.assertEqual(len(list(pathlib.Path("/proc/self/fd").iterdir())), before)

    def test_recovery_filesystem_mismatch_fails_before_publication(self) -> None:
        class Device:
            def __init__(self, value: int) -> None:
                self.st_dev = value

        with patch("vegavisuals.registry.os.fstat", side_effect=[Device(1), Device(1), Device(2)]):
            with self.assertRaisesRegex(PolicyError, "must share one filesystem"):
                self.registry._require_recovery_filesystem("out/chart.svg")

        self.assertFalse((self.root / "out/chart.svg").exists())

    def test_raw_vega_local_data_uses_the_same_staging_policy(self) -> None:
        spec = json.loads(vg_spec())
        spec["data"] = [{"name": "table", "url": "charts/data/table.json"}]
        write(self.root / "charts" / "chart.vg.json", json.dumps(spec))
        write(self.root / "charts" / "data" / "table.json", '[{"x": 1}]\n')
        result = self.registry.render_visualization("charts/chart.vg.json", "out/chart.svg")
        self.assertTrue(result["ok"])
        staged_data = self.runner.rendered_specs[0]["data"][0]
        self.assertNotIn("url", staged_data)
        self.assertEqual(staged_data["format"], {"type": "json"})
        self.assertEqual(staged_data["values"], '[{"x": 1}]\n')

    def test_data_escape_remote_and_file_urls_are_rejected(self) -> None:
        cases = [
            "../../outside.csv",
            "https://example.invalid/data.csv",
            "http://example.invalid/data.csv",
            "file:///etc/passwd",
            "data:text/csv,x%2Cy",
            "//example.invalid/data.csv",
        ]
        for index, url in enumerate(cases):
            write(self.root / f"chart-{index}.vl.json", vl_spec(url))
            with self.subTest(url=url), self.assertRaises(PolicyError):
                self.registry.validate_visualization(f"chart-{index}.vl.json")

    def test_symlinked_data_escape_is_rejected(self) -> None:
        outside = pathlib.Path(self.temporary.name).parent / "outside.csv"
        write(outside, "x,y\nA,1\n")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        write(self.root / "chart.vl.json", vl_spec("escape.csv"))
        (self.root / "escape.csv").symlink_to(outside)
        with self.assertRaises(PolicyError):
            self.registry.validate_visualization("chart.vl.json")

    def test_symlinked_source_and_input_are_rejected_even_when_the_target_is_inside(self) -> None:
        write(self.root / "real.vl.json", vl_spec())
        (self.root / "linked.vl.json").symlink_to(self.root / "real.vl.json")
        with self.assertRaises(PolicyError):
            self.registry.validate_visualization("linked.vl.json")

        write(self.root / "chart.vl.json", vl_spec())
        write(self.root / "real.txt", "input\n")
        (self.root / "linked.txt").symlink_to(self.root / "real.txt")
        with self.assertRaises(PolicyError):
            self.registry.validate_visualization("chart.vl.json", inputs=["linked.txt"])

    def test_inline_render_rejects_every_url_dependency(self) -> None:
        for url in ("table.csv", "https://example.invalid/table.csv"):
            with self.subTest(url=url), self.assertRaises(PolicyError):
                self.registry.render_visualization_text(vl_spec(url), dry_run=True)

        image_spec = json.dumps(
            {
                "data": {"values": [{"image": "https://example.invalid/image.png"}]},
                "mark": "image",
                "encoding": {"url": {"field": "image"}},
            }
        )
        with self.assertRaises(PolicyError):
            self.registry.render_visualization_text(image_spec, engine="vega-lite", dry_run=True)

    def test_inline_values_may_have_a_field_named_url_when_it_is_not_a_dependency(self) -> None:
        spec = json.dumps(
            {
                "data": {"values": [{"url": "shown as text"}]},
                "mark": "text",
                "encoding": {"text": {"field": "url"}},
            }
        )
        result = self.registry.render_visualization_text(spec, engine="vega-lite", dry_run=True)
        self.assertTrue(result["ok"])

    def test_file_render_rejects_remote_image_and_href_channels(self) -> None:
        image = json.dumps(
            {
                "data": {"values": [{"image": "https://example.invalid/image.png"}]},
                "mark": "image",
                "encoding": {"url": {"field": "image"}},
            }
        )
        write(self.root / "image.vl.json", image)
        with self.assertRaises(PolicyError):
            self.registry.validate_visualization("image.vl.json")
        linked = json.loads(vg_spec())
        linked["marks"][0]["encode"] = {"enter": {"href": {"value": "https://example.invalid"}}}
        write(self.root / "linked.vg.json", json.dumps(linked))
        with self.assertRaises(PolicyError):
            self.registry.validate_visualization("linked.vg.json")

    def test_output_parent_symlink_swap_cannot_escape_project(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        write(self.root / "chart.vl.json", vl_spec())
        (self.root / "out").mkdir()
        runner = SwapParentDockerMock(self.root, outside)
        registry = Registry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        with self.assertRaises(PolicyError):
            registry.render_visualization("chart.vl.json", "out/chart.svg")
        self.assertFalse((outside / "chart.svg").exists())
        (self.root / "out").unlink()

    def test_project_root_descriptor_survives_ancestor_path_replacement(self) -> None:
        container = self.root / "container"
        project = container / "project"
        original = container / "original"
        outside = self.root / "outside"
        project.mkdir(parents=True)
        outside.mkdir()
        write(project / "chart.vl.json", vl_spec())
        runner = DockerMock()
        registry = Registry(project, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        project.rename(original)
        project.symlink_to(outside, target_is_directory=True)

        result = registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertTrue(result["ok"])
        self.assertTrue((original / "chart.svg").is_file())
        self.assertFalse((outside / "chart.svg").exists())
        registry.close()

    def test_cache_and_lock_symlink_swaps_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as outside_name:
            outside = pathlib.Path(outside_name)

            def swap_cache() -> None:
                (self.root / ".cache/vegavisuals/text").symlink_to(outside, target_is_directory=True)

            cache_runner = CallbackDockerMock(swap_cache)
            cache_registry = Registry(self.root, runner=cache_runner)
            cache_runner.renderer_contract = cache_registry._renderer_contract(
                cache_registry.assets / "compat/vl-convert-1.9.0.json"
            )
            with self.assertRaises(PolicyError):
                cache_registry.render_visualization_text(vl_spec())
            self.assertEqual(list(outside.iterdir()), [])
            (self.root / ".cache/vegavisuals/text").unlink()

            outside_lock = outside / "outside-lock.json"
            outside_lock.write_text("sentinel", encoding="utf-8")

            def swap_lock() -> None:
                (self.root / LOCK_NAME).symlink_to(outside_lock)

            write(self.root / "chart.vl.json", vl_spec())
            lock_runner = CallbackDockerMock(swap_lock)
            lock_registry = Registry(self.root, runner=lock_runner)
            lock_runner.renderer_contract = lock_registry._renderer_contract(
                lock_registry.assets / "compat/vl-convert-1.9.0.json"
            )
            with self.assertRaises(PolicyError):
                lock_registry.render_visualization("chart.vl.json", "chart.svg")
            self.assertEqual(outside_lock.read_text(encoding="utf-8"), "sentinel")
            self.assertFalse((self.root / "chart.svg").exists())
            (self.root / LOCK_NAME).unlink()

    def test_engine_and_suffix_agreement(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        with self.assertRaises(ValidationError):
            self.registry.validate_visualization("chart.vl.json", engine="vega")
        with self.assertRaises(ValidationError):
            self.registry.render_visualization(
                "chart.vl.json", "chart.png", output_format="svg", dry_run=True
            )
        write(self.root / "chart.vg.json", vg_spec())
        self.assertEqual(self.registry.validate_visualization("chart.vg.json")["engine"], "vega")


class FingerprintTest(TemporaryProject):
    def test_local_data_and_explicit_inputs_change_fingerprint(self) -> None:
        write(self.root / "chart.vl.json", vl_spec("table.csv"))
        write(self.root / "table.csv", "x,y\nA,1\n")
        write(self.root / "notes.txt", "one\n")
        first = self.registry.validate_visualization("chart.vl.json", inputs=["notes.txt"])
        write(self.root / "table.csv", "x,y\nA,2\n")
        second = self.registry.validate_visualization("chart.vl.json", inputs=["notes.txt"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        write(self.root / "notes.txt", "two\n")
        third = self.registry.validate_visualization("chart.vl.json", inputs=["notes.txt"])
        self.assertNotEqual(second["fingerprint"], third["fingerprint"])

    def test_inventory_only_tokens_do_not_invalidate_render_fingerprints(self) -> None:
        assets = self.root / "assets"
        shutil.copytree(self.registry.assets, assets)
        self.registry.assets = assets
        self.runner.renderer_contract = self.registry._renderer_contract(assets / "compat/vl-convert-1.9.0.json")
        write(self.root / "chart.vl.json", vl_spec())
        first = self.registry.validate_visualization("chart.vl.json")
        token_path = assets / "tokens/benizar.json"
        tokens = json.loads(token_path.read_text(encoding="utf-8"))
        tokens["colors"]["blue_800"] = "#123456"
        token_path.write_text(json.dumps(tokens), encoding="utf-8")
        second = self.registry.validate_visualization("chart.vl.json")
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_rebuilt_image_id_is_provenance_not_a_portable_fingerprint_input(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        first = self.registry.validate_visualization("chart.vl.json")
        self.runner.image_id = "sha256:" + "2" * 64
        second = self.registry.validate_visualization("chart.vl.json")
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["renderer"]["image_id"], second["renderer"]["image_id"])


class ValidationContractTest(TemporaryProject):
    def test_non_finite_overflow_and_excessive_nesting_are_rejected(self) -> None:
        write(self.root / "overflow.vl.json", '{"mark":"bar","data":{"values":[{"x":1e9999}]}}')
        with self.assertRaises(ValidationError):
            self.registry.validate_visualization("overflow.vl.json")
        deep = '{"mark":"bar","x":' + "[" * 2000 + "0" + "]" * 2000 + "}"
        write(self.root / "deep.vl.json", deep)
        with self.assertRaises(ValidationError):
            self.registry.validate_visualization("deep.vl.json")

    def test_semantic_basics_and_schema_versions_are_enforced(self) -> None:
        write(self.root / "bad-mark.vl.json", '{"mark":{"type":"not-a-mark"}}')
        with self.assertRaises(ValidationError):
            self.registry.validate_visualization("bad-mark.vl.json")
        write(self.root / "bad-marks.vg.json", '{"marks":"not-a-list"}')
        with self.assertRaises(ValidationError):
            self.registry.validate_visualization("bad-marks.vg.json")
        write(
            self.root / "future.vl.json",
            '{"$schema":"https://vega.github.io/schema/vega-lite/v999.json","mark":"bar"}',
        )
        with self.assertRaises(ValidationError):
            self.registry.validate_visualization("future.vl.json")

    def test_source_schema_selects_an_installed_vega_lite_version(self) -> None:
        write(
            self.root / "legacy.vl.json",
            '{"$schema":"https://vega.github.io/schema/vega-lite/v5.15.json","mark":"bar"}',
        )
        result = self.registry.render_visualization("legacy.vl.json", "legacy.svg")
        self.assertEqual(result["vega_lite_version"], "5.15")
        command = result["command"]
        self.assertEqual(command[command.index("--vl-version") + 1], "5.15")


class DockerCommandTest(TemporaryProject):
    def test_renderer_lock_name_scopes_docker_endpoint_context_and_image(self) -> None:
        with patch.dict(
            os.environ,
            {"DOCKER_CONTEXT": "", "DOCKER_HOST": "unix:///daemon-one.sock", "XDG_CACHE_HOME": "/tmp/cache-one"},
        ):
            first = registry_module._renderer_lock_name("vegavisuals/render:test")
        with patch.dict(
            os.environ,
            {"DOCKER_CONTEXT": "", "DOCKER_HOST": "unix:///daemon-one.sock", "XDG_CACHE_HOME": "/tmp/cache-two"},
        ):
            same_daemon = registry_module._renderer_lock_name("vegavisuals/render:test")
            other_image = registry_module._renderer_lock_name("vegavisuals/render:other")
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "unix:///daemon-one.sock"}):
            other_context = registry_module._renderer_lock_name("vegavisuals/render:test")

        self.assertEqual(first, same_daemon)
        self.assertNotEqual(first, other_image)
        self.assertNotEqual(first, other_context)

    def test_renderer_build_lock_does_not_mask_body_oserror(self) -> None:
        with self.assertRaisesRegex(OSError, "renderer body failed"):
            with self.registry._renderer_build_lock("vegavisuals/render:test-body-error"):
                raise OSError("renderer body failed")

    def test_runtime_command_is_hardened_without_exposing_the_project(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        result = self.registry.render_visualization("chart.vl.json", "out/chart.svg")
        command = result["command"]
        required_pairs = {
            "--network": "none",
            "--cap-drop": "ALL",
            "--security-opt": "no-new-privileges",
            "--pids-limit": "64",
            "--memory": "768m",
        }
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--pull") + 1], "never")
        self.assertTrue(command[command.index("--name") + 1].startswith("vegavisuals-"))
        for option, value in required_pairs.items():
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertNotEqual(command[command.index("--user") + 1].split(":")[0], "0")
        mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
        self.assertEqual(len(mounts), 1)
        self.assertIn("target=/output", mounts[0])
        self.assertNotIn(str(self.root), command)
        self.assertEqual(command[command.index("--workdir") + 1], "/output")
        self.assertEqual(command[command.index("--expected-vega") + 1], "6.2.0")
        self.assertEqual(command[command.index("--max-output-bytes") + 1], "52428800")
        self.assertRegex(command[command.index("--workdir") + 2], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("bash", command)
        self.assertNotIn("sh", command)
        labels = [command[index + 1] for index, item in enumerate(command) if item == "--label"]
        self.assertIn(CONTAINER_LABEL, labels)
        self.assertIn(f"{CONTAINER_WORKSPACE_LABEL}={workspace_id(self.root)}", labels)

    def test_project_path_with_commas_is_not_interpolated_into_mount_syntax(self) -> None:
        root = self.root / "consumer,with,commas"
        root.mkdir()
        write(root / "chart.vl.json", vl_spec())
        runner = DockerMock()
        registry = Registry(root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        result = registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertTrue(result["ok"])
        self.assertNotIn(str(root), result["command"])

    def test_staging_mount_csv_encodes_tmpdir_with_commas_and_quotes(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        with tempfile.TemporaryDirectory() as temporary:
            tmpdir = pathlib.Path(temporary) / 'tmp,with"quote'
            tmpdir.mkdir()
            previous_tempdir = tempfile.tempdir
            with patch.dict(os.environ, {"TMPDIR": str(tmpdir)}):
                tempfile.tempdir = None
                try:
                    result = self.registry.render_visualization("chart.vl.json", "chart.svg")
                finally:
                    tempfile.tempdir = previous_tempdir

        mount = next(
            result["command"][index + 1]
            for index, value in enumerate(result["command"])
            if value == "--mount"
        )
        self.assertEqual(docker_mount_source(result["command"], "/output").parent, tmpdir)
        self.assertIn('""', mount)

    def test_build_command_uses_packaged_context_and_exact_version(self) -> None:
        result = self.registry.build_renderer(dry_run=True)
        self.assertIn("VL_CONVERT_VERSION=1.9.0.post1", result["command"])
        self.assertIn(
            "BASE_IMAGE=python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1",
            result["command"],
        )
        self.assertIn("QPDF_VERSION=11.3.0-1+deb12u1", result["command"])
        self.assertEqual(pathlib.Path(result["context"]), self.registry.assets)
        self.assertTrue(pathlib.Path(result["dockerfile"]).is_file())
        dockerfile = pathlib.Path(result["dockerfile"]).read_text(encoding="utf-8")
        self.assertIn("fonts-dejavu-core=2.37-6", dockerfile)
        self.assertIn('"qpdf=${QPDF_VERSION}"', dockerfile)
        self.assertIn("FROM ${BASE_IMAGE}", dockerfile)
        self.assertNotIn("nodejs", dockerfile)
        self.assertNotIn("chromium", dockerfile)


class LockAndManifestTest(TemporaryProject):
    def test_first_render_creates_then_fresh_render_skips(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        first = self.registry.render_visualization("chart.vl.json", "chart.svg")
        self.assertTrue(first["rendered"])
        render_count = len(self.runner.render_commands)
        second = self.registry.render_visualization("chart.vl.json", "chart.svg")
        self.assertTrue(second["skipped"])
        self.assertEqual(len(self.runner.render_commands), render_count)
        lock = json.loads((self.root / ".vegavisuals.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["version"], LOCK_VERSION)
        self.assertEqual(len(lock["visualizations"]), 1)
        entry = next(iter(lock["visualizations"].values()))
        self.assertRegex(entry["renderer"]["image_id"], r"^sha256:[0-9a-f]{64}$")

    def test_unmanaged_and_modified_outputs_need_confirmation(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        write(self.root / "chart.svg", '<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with self.assertRaises(PolicyError):
            self.registry.render_visualization("chart.vl.json", "chart.svg")
        self.registry.render_visualization("chart.vl.json", "chart.svg", confirm_replace=True)
        write(self.root / "chart.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>edited</text></svg>')
        with self.assertRaises(PolicyError):
            self.registry.render_visualization("chart.vl.json", "chart.svg", force=True)
        replaced = self.registry.render_visualization(
            "chart.vl.json", "chart.svg", force=True, confirm_replace=True
        )
        self.assertTrue(replaced["rendered"])

    def test_output_created_at_publication_boundary_is_not_overwritten(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        user_output = b'<svg xmlns="http://www.w3.org/2000/svg"><text>user</text></svg>'
        runner = DockerMock()
        registry = PublicationCallbackRegistry(
            self.root,
            runner,
            lambda: write(self.root / "chart.svg", user_output),
        )
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "appeared while it was being published"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertEqual((self.root / "chart.svg").read_bytes(), user_output)
        self.assertFalse((self.root / LOCK_NAME).exists())

    def test_output_edited_at_publication_boundary_is_not_overwritten(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        lock_before = (self.root / LOCK_NAME).read_bytes()
        write(self.root / "chart.vl.json", vl_spec(value=2))
        user_output = b'<svg xmlns="http://www.w3.org/2000/svg"><text>edited</text></svg>'
        runner = DockerMock()
        registry = PublicationCallbackRegistry(
            self.root,
            runner,
            lambda: write(self.root / "chart.svg", user_output),
        )
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "changed while it was being published"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertEqual((self.root / "chart.svg").read_bytes(), user_output)
        self.assertEqual((self.root / LOCK_NAME).read_bytes(), lock_before)

    def test_source_changed_at_publication_boundary_aborts_before_writing(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        runner = DockerMock()
        registry = PublicationCallbackRegistry(
            self.root,
            runner,
            lambda: write(self.root / "chart.vl.json", vl_spec(value=2)),
        )
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "source changed while rendering"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertFalse((self.root / "chart.svg").exists())
        self.assertFalse((self.root / LOCK_NAME).exists())

    def test_lock_changed_during_publication_rolls_back_the_output(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        output_before = (self.root / "chart.svg").read_bytes()
        write(self.root / "chart.vl.json", vl_spec(value=2))
        changed_lock = (self.root / LOCK_NAME).read_bytes() + b"\n"
        runner = DockerMock()
        registry = PublicationCallbackRegistry(
            self.root,
            runner,
            lambda: write(self.root / LOCK_NAME, changed_lock),
        )
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "changed while it was being published"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertEqual((self.root / "chart.svg").read_bytes(), output_before)
        self.assertEqual((self.root / LOCK_NAME).read_bytes(), changed_lock)

    def test_failed_reverse_exchange_preserves_the_displaced_user_output(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        write(self.root / "chart.vl.json", vl_spec(value=2))
        user_output = b'<svg xmlns="http://www.w3.org/2000/svg"><text>edited</text></svg>'
        runner = DockerMock()
        registry = PublicationCallbackRegistry(
            self.root,
            runner,
            lambda: write(self.root / "chart.svg", user_output),
        )
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        from vegavisuals import registry as registry_module

        real_renameat2 = registry_module._renameat2
        calls = 0

        def fail_second_exchange(*args):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "simulated reverse exchange failure")
            return real_renameat2(*args)

        with patch("vegavisuals.registry._renameat2", side_effect=fail_second_exchange):
            with self.assertRaisesRegex(RenderError, "preserved displaced file"):
                registry.render_visualization("chart.vl.json", "chart.svg")

        preserved = list(self.root.glob(".chart.svg.*.tmp"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), user_output)

    def test_rollback_leaves_a_concurrent_oversized_edit_and_original_backup_intact(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        output_before = (self.root / "chart.svg").read_bytes()
        write(self.root / "chart.vl.json", vl_spec(value=2))
        concurrent = b"x" * 4096
        runner = DockerMock()
        registry = LockPublicationCallbackFailRegistry(
            self.root,
            runner,
            lambda: write(self.root / "chart.svg", concurrent),
        )
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "preserved original"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertEqual((self.root / "chart.svg").read_bytes(), concurrent)
        preserved = list(self.root.glob(".chart.svg.*.tmp"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), output_before)

    def test_rollback_keeps_an_edit_made_after_the_restore_exchange_visible(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        write(self.root / "chart.vl.json", vl_spec(value=2))
        concurrent = b"z" * 4096
        runner = DockerMock()
        registry = LockPublicationFailRegistry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        from vegavisuals import registry as registry_module

        real_renameat2 = registry_module._renameat2
        calls = 0

        def edit_after_restore(*args):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            result = real_renameat2(*args)
            if calls == 2:
                write(self.root / "chart.svg", concurrent)
            return result

        with patch("vegavisuals.registry._renameat2", side_effect=edit_after_restore):
            with self.assertRaisesRegex(RenderError, "preserved a concurrent visible edit"):
                registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertEqual((self.root / "chart.svg").read_bytes(), concurrent)
        self.assertEqual(len(list(self.root.glob(".chart.svg.*.tmp"))), 1)

    def test_commit_preserves_an_original_modified_through_an_open_descriptor(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        write(self.root / "chart.vl.json", vl_spec(value=2))
        from vegavisuals import registry as registry_module

        real_commit = registry_module._ProjectPublication.commit
        with (self.root / "chart.svg").open("r+b") as original:
            def edit_before_commit(publication):  # type: ignore[no-untyped-def]
                if publication.target == "chart.svg":
                    original.seek(0)
                    original.truncate()
                    original.write(b"concurrent edit")
                    original.flush()
                    os.fsync(original.fileno())
                return real_commit(publication)

            with patch.object(registry_module._ProjectPublication, "commit", edit_before_commit):
                with self.assertRaisesRegex(RenderError, "preserved a concurrently changed original"):
                    self.registry.render_visualization("chart.vl.json", "chart.svg")

        preserved = list(self.root.glob(".chart.svg.*.tmp"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b"concurrent edit")
        lock = self.registry._load_lock()
        entry = next(iter(lock["visualizations"].values()))
        self.assertEqual(self.registry._project_file_hash("chart.svg")[0], entry["output_sha256"])

    def test_commit_archive_retains_an_edit_made_at_final_unlink(self) -> None:
        write(self.root / "chart.vl.json", vl_spec(value=1))
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        write(self.root / "chart.vl.json", vl_spec(value=2))
        from vegavisuals import registry as registry_module

        real_archive = registry_module.Registry._archive_publication_file
        with (self.root / "chart.svg").open("r+b") as original:
            def edit_after_archive(instance, source_fd, source_name, expected):  # type: ignore[no-untyped-def]
                archived = real_archive(instance, source_fd, source_name, expected)
                if source_name.startswith(".chart.svg.") and source_name.endswith(".tmp"):
                    original.seek(0)
                    original.truncate()
                    original.write(b"late concurrent edit")
                    original.flush()
                    os.fsync(original.fileno())
                return archived

            with patch.object(registry_module.Registry, "_archive_publication_file", edit_after_archive):
                result = self.registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertTrue(result["ok"])
        archive_root = self.root / ".cache/vegavisuals/replaced"
        self.assertEqual(stat.S_IMODE(archive_root.stat().st_mode), 0o700)
        archives = list(archive_root.glob("*.replaced"))
        self.assertTrue(any(path.read_bytes() == b"late concurrent edit" for path in archives))

    def test_keyboard_interrupt_during_lock_publication_rolls_back_output(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        runner = DockerMock()
        registry = LockPublicationInterruptRegistry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaises(KeyboardInterrupt):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertFalse((self.root / "chart.svg").exists())
        self.assertFalse((self.root / LOCK_NAME).exists())

    def test_keyboard_interrupt_after_output_publication_uses_transaction_log(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        runner = DockerMock()
        registry = OutputPublicationInterruptRegistry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaises(KeyboardInterrupt):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertFalse((self.root / "chart.svg").exists())
        self.assertFalse((self.root / LOCK_NAME).exists())

    def test_rollback_does_not_move_a_concurrent_symlink(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        outside = self.root / "outside.svg"
        write(outside, '<svg xmlns="http://www.w3.org/2000/svg"><text>outside</text></svg>')

        def replace_with_symlink() -> None:
            (self.root / "chart.svg").unlink()
            (self.root / "chart.svg").symlink_to(outside)

        runner = DockerMock()
        registry = LockPublicationCallbackFailRegistry(self.root, runner, replace_with_symlink)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "cannot safely read current published target"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertTrue((self.root / "chart.svg").is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), '<svg xmlns="http://www.w3.org/2000/svg"><text>outside</text></svg>')

    def test_safe_publication_fails_closed_without_renameat2(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        error = OSError(getattr(os, "ENOSYS", 38), "renameat2 unavailable")

        with patch("vegavisuals.registry._renameat2", side_effect=error):
            with self.assertRaisesRegex(PolicyError, "safe no-replace publication is unavailable"):
                self.registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertFalse((self.root / "chart.svg").exists())
        self.assertFalse((self.root / LOCK_NAME).exists())

    def test_failed_stale_render_leaves_managed_output_untouched(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        before = (self.root / "chart.svg").read_bytes()
        write(self.root / "chart.vl.json", vl_spec(value=2))
        failing_runner = FailingDockerMock()
        failing_registry = Registry(self.root, runner=failing_runner)
        failing_runner.renderer_contract = failing_registry._renderer_contract(
            failing_registry.assets / "compat/vl-convert-1.9.0.json"
        )
        with self.assertRaises(RenderError):
            failing_registry.render_visualization("chart.vl.json", "chart.svg")
        self.assertEqual((self.root / "chart.svg").read_bytes(), before)
        self.assertTrue(
            any(command[:4] == ["docker", "container", "rm", "--force"] for command in failing_runner.calls)
        )

    def test_lock_failure_rolls_back_output_and_lock(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        self.registry.render_visualization("chart.vl.json", "chart.svg")
        output_before = (self.root / "chart.svg").read_bytes()
        lock_before = (self.root / LOCK_NAME).read_bytes()
        write(self.root / "chart.vl.json", vl_spec(value=2))
        runner = DockerMock()
        registry = LockPublicationFailRegistry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        with self.assertRaisesRegex(OSError, "simulated lock publication failure"):
            registry.render_visualization("chart.vl.json", "chart.svg")
        self.assertEqual((self.root / "chart.svg").read_bytes(), output_before)
        self.assertEqual((self.root / LOCK_NAME).read_bytes(), lock_before)

    def test_concurrent_processes_merge_distinct_lock_entries(self) -> None:
        write(self.root / "one.vl.json", vl_spec(value=1))
        write(self.root / "two.vl.json", vl_spec(value=2))
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results = context.Queue()
        contract = self.registry._renderer_contract(self.registry.assets / "compat/vl-convert-1.9.0.json")
        processes = [
            context.Process(
                target=concurrent_render_process,
                args=(str(self.root), name, barrier, contract, results),
            )
            for name in ("one", "two")
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertFalse(process.is_alive(), "concurrent render process did not finish")
            self.assertEqual(process.exitcode, 0)
        errors = [results.get(timeout=2) for _ in processes]
        self.assertEqual(errors, [None, None])
        lock = json.loads((self.root / LOCK_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(lock["visualizations"]), 2)
        self.assertTrue((self.root / "out/one.svg").is_file())
        self.assertTrue((self.root / "out/two.svg").is_file())

    def _manifest(self) -> None:
        write(self.root / "one.vl.json", vl_spec())
        write(self.root / "two.vg.json", vg_spec())
        manifest = {
            "version": 1,
            "profile": "vl-convert-1.9.0",
            "family": "benizar",
            "visualizations": [
                {"name": "one", "source": "one.vl.json", "output": "out/one.svg"},
                {
                    "name": "two",
                    "source": "two.vg.json",
                    "output": "out/two.png",
                    "engine": "vega",
                    "format": "png",
                },
            ],
        }
        write(self.root / ".vegavisuals.yml", safe_dump(manifest, sort_keys=False))

    def _receipt_manifest(self) -> None:
        write(self.root / "charts/chart.vl.json", vl_spec("assets/data/table.csv"))
        write(self.root / "assets/charts/unlisted.vl.json", vl_spec("assets/data/unlisted.csv"))
        write(self.root / "assets/data/table.csv", "x,y\nA,1\n")
        write(self.root / "assets/data/unlisted.csv", "x,y\nB,2\n")
        write(self.root / "assets/data/global.csv", "global\n1\n")
        write(self.root / "assets/data/item.csv", "item\n1\n")
        manifest = {
            "version": 1,
            "profile": "vl-convert-1.9.0",
            "family": "benizar",
            "inputs": ["assets/data/global.csv"],
            "visualizations": [
                {
                    "name": "chart",
                    "source": "charts/chart.vl.json",
                    "output": "assets/charts/chart.svg",
                    "inputs": ["assets/data/item.csv"],
                }
            ],
        }
        write(self.root / ".vegavisuals.yml", safe_dump(manifest, sort_keys=False))

    def test_successful_check_publishes_exact_bounded_receipt(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()

        checked = self.registry.visualization_check()

        self.assertTrue(checked["ok"], checked)
        receipt = json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "provider",
                "provider_version",
                "release",
                "request_sha256",
                "ok",
                "inputs",
                "artifacts",
            },
        )
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["provider"], "vegavisuals")
        self.assertEqual(receipt["provider_version"], __version__)
        self.assertEqual(receipt["release"], DEFAULT_RELEASE)
        self.assertIs(receipt["ok"], True)
        self.assertEqual(
            {item["path"] for item in receipt["inputs"]},
            {
                "assets/data/global.csv",
                "assets/data/item.csv",
                "assets/data/table.csv",
                "assets/data/unlisted.csv",
            },
        )
        self.assertEqual([item["path"] for item in receipt["artifacts"]], ["assets/charts/chart.svg"])

        digest = hashlib.sha256()
        digest.update(b"unaltraweb-companion-receipt-v1\0vegavisuals\0")
        for relative in [
            ".vegavisuals.yml",
            "assets/charts/unlisted.vl.json",
            "charts/chart.vl.json",
        ]:
            path_bytes = relative.encode("utf-8")
            content = (self.root / relative).read_bytes()
            digest.update(len(path_bytes).to_bytes(8, "big"))
            digest.update(path_bytes)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        self.assertEqual(receipt["request_sha256"], digest.hexdigest())
        self.assertLess((self.root / RECEIPT_PATH).stat().st_size, 256 * 1024)

    def test_receipt_inventory_cannot_omit_explicit_or_discovered_dependencies(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()

        self.registry.visualization_check()
        receipt = json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))

        expected = {
            relative: hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
            for relative in [
                "assets/data/global.csv",
                "assets/data/item.csv",
                "assets/data/table.csv",
                "assets/data/unlisted.csv",
            ]
        }
        self.assertEqual({item["path"]: item["sha256"] for item in receipt["inputs"]}, expected)

    def test_dataset_change_invalidates_then_updates_receipt(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()
        self.registry.visualization_check()
        before = json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))
        before_inputs = {item["path"]: item["sha256"] for item in before["inputs"]}

        write(self.root / "assets/data/table.csv", "x,y\nA,2\n")
        failed = self.registry.visualization_check()

        self.assertFalse(failed["ok"])
        self.assertIs(json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))["ok"], False)
        self.registry.render_visualizations()
        self.registry.visualization_check()
        after = json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))
        after_inputs = {item["path"]: item["sha256"] for item in after["inputs"]}
        self.assertEqual(after["request_sha256"], before["request_sha256"])
        self.assertNotEqual(after_inputs["assets/data/table.csv"], before_inputs["assets/data/table.csv"])

    def test_failed_check_invalidates_stale_owned_receipt(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()
        self.registry.visualization_check()
        (self.root / "assets/charts/chart.svg").unlink()

        checked = self.registry.visualization_check()

        self.assertFalse(checked["ok"])
        invalid = json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))
        self.assertEqual(invalid, {"schema_version": 1, "provider": "vegavisuals", "ok": False})

    def test_source_race_cannot_publish_a_receipt_for_a_stale_artifact(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()
        self.registry.visualization_check()
        original = self.registry._receipt_payload

        def change_source_after_validation(manifest, status):
            source = self.root / "charts/chart.vl.json"
            write(source, source.read_text(encoding="utf-8") + "\n")
            return original(manifest, status)

        with patch.object(self.registry, "_receipt_payload", side_effect=change_source_after_validation):
            with self.assertRaisesRegex(RenderError, "source changed after validation"):
                self.registry.visualization_check()

        invalid = json.loads((self.root / RECEIPT_PATH).read_text(encoding="utf-8"))
        self.assertIs(invalid["ok"], False)

    def test_receipt_parent_symlink_cannot_escape_project(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()
        with tempfile.TemporaryDirectory(prefix="vegavisuals-receipt-outside-") as outside_raw:
            outside = pathlib.Path(outside_raw)
            (self.root / ".unaltraweb").mkdir()
            (self.root / ".unaltraweb/receipts").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(PolicyError, "symlinked parent"):
                self.registry.visualization_check()

            self.assertEqual(list(outside.iterdir()), [])

    def test_receipt_target_symlink_is_replaced_without_following_it(self) -> None:
        self._receipt_manifest()
        self.registry.render_visualizations()
        with tempfile.TemporaryDirectory(prefix="vegavisuals-receipt-target-") as outside_raw:
            outside = pathlib.Path(outside_raw) / "outside.json"
            outside.write_text('{"outside":true}\n', encoding="utf-8")
            (self.root / ".unaltraweb/receipts").mkdir(parents=True)
            (self.root / RECEIPT_PATH).symlink_to(outside)

            checked = self.registry.visualization_check()

            self.assertTrue(checked["ok"], checked)
            self.assertFalse((self.root / RECEIPT_PATH).is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"outside":true}\n')

    def test_manifest_status_render_all_and_check(self) -> None:
        self._manifest()
        status = self.registry.visualization_status()
        self.assertEqual(status["counts"], {"missing": 2})
        self.assertFalse(self.registry.visualization_check()["ok"])
        rendered = self.registry.render_visualizations()
        self.assertTrue(rendered["ok"], rendered)
        self.assertEqual(rendered["rendered"], 2)
        checked = self.registry.visualization_check()
        self.assertTrue(checked["ok"], checked)
        again = self.registry.render_visualizations()
        self.assertEqual(again["skipped"], 2)
        write(self.root / "one.vl.json", vl_spec(value=2))
        changed = self.registry.visualization_status()
        states = {item["name"]: item["state"] for item in changed["visualizations"]}
        self.assertEqual(states["one"], "stale")
        self.assertEqual(states["two"], "fresh")

    def test_freshness_check_uses_the_portable_contract_when_docker_is_unavailable(self) -> None:
        self._manifest()
        rendered = self.registry.render_visualizations()
        self.assertTrue(rendered["ok"], rendered)

        unavailable = DockerMock()
        offline_registry = Registry(self.root, runner=unavailable)
        status = offline_registry.visualization_status()
        self.assertFalse(status["renderer"]["available"])
        self.assertEqual(status["counts"], {"fresh": 2})
        self.assertTrue(offline_registry.visualization_check()["ok"])

    def test_rebuilt_image_with_the_same_contract_keeps_outputs_fresh(self) -> None:
        self._manifest()
        self.registry.render_visualizations()
        self.runner.image_id = "sha256:" + "2" * 64

        status = self.registry.visualization_status()

        self.assertEqual(status["counts"], {"fresh": 2})
        self.assertTrue(self.registry.visualization_check()["ok"])

    def test_renderer_contract_change_is_stale_and_can_be_rerendered(self) -> None:
        self._manifest()
        self.registry.render_visualizations()
        assets = self.root / "assets"
        shutil.copytree(self.registry.assets, assets)
        theme_path = assets / "themes/benizar.json"
        theme = json.loads(theme_path.read_text(encoding="utf-8"))
        theme["background"] = "#F2F7FD"
        theme_path.write_text(json.dumps(theme), encoding="utf-8")
        self.registry.assets = assets

        status = self.registry.visualization_status()
        rendered = self.registry.render_visualizations()

        self.assertEqual(status["counts"], {"stale": 2})
        self.assertEqual(rendered["rendered"], 2)
        self.assertTrue(self.registry.visualization_check()["ok"])

    def test_manifest_rejects_duplicates_and_yaml_aliases(self) -> None:
        write(self.root / "one.vl.json", vl_spec())
        duplicate = {
            "version": 1,
            "profile": "vl-convert-1.9.0",
            "family": "benizar",
            "visualizations": [
                {"name": "one", "source": "one.vl.json", "output": "one.svg"},
                {"name": "one", "source": "one.vl.json", "output": "two.svg"},
            ],
        }
        write(self.root / ".vegavisuals.yml", safe_dump(duplicate, sort_keys=False))
        with self.assertRaises(ManifestError):
            self.registry.visualization_status()
        write(
            self.root / ".vegavisuals.yml",
            "version: 1\nprofile: vl-convert-1.9.0\nfamily: benizar\nvisualizations: &v []\nother: *v\n",
        )
        with self.assertRaises(ManifestError):
            self.registry.visualization_status()

    def test_manifest_rejects_duplicate_yaml_keys_sources_and_boolean_version(self) -> None:
        write(self.root / "one.vl.json", vl_spec())
        duplicate_key = (
            "version: 1\nprofile: vl-convert-1.9.0\nprofile: vl-convert-1.9.0\n"
            "family: benizar\nvisualizations: []\n"
        )
        write(self.root / ".vegavisuals.yml", duplicate_key)
        with self.assertRaises(ManifestError):
            self.registry.visualization_status()
        write(
            self.root / ".vegavisuals.yml",
            "version: true\nprofile: vl-convert-1.9.0\nfamily: benizar\nvisualizations: []\n",
        )
        with self.assertRaises(ManifestError):
            self.registry.visualization_status()
        duplicate_source = {
            "version": 1,
            "profile": "vl-convert-1.9.0",
            "family": "benizar",
            "visualizations": [
                {"name": "one", "source": "one.vl.json", "output": "one.svg"},
                {"name": "two", "source": "one.vl.json", "output": "two.svg"},
            ],
        }
        write(self.root / ".vegavisuals.yml", safe_dump(duplicate_source, sort_keys=False))
        with self.assertRaises(ManifestError):
            self.registry.visualization_status()

    def test_manifest_requires_safe_project_relative_paths(self) -> None:
        write(self.root / "one.vl.json", vl_spec())
        write(self.root / "input.csv", "x,y\nA,1\n")
        base = {
            "version": 1,
            "profile": "vl-convert-1.9.0",
            "family": "benizar",
            "visualizations": [
                {
                    "name": "one",
                    "source": "one.vl.json",
                    "output": "out/one.svg",
                    "inputs": ["input.csv"],
                }
            ],
        }
        cases = [
            ("source", str(self.root / "one.vl.json")),
            ("source", "nested/../one.vl.json"),
            ("output", str(self.root / "out/one.svg")),
            ("output", "https://example.invalid/one.svg"),
            ("inputs", ["nested/../input.csv"]),
        ]
        for field, value in cases:
            manifest = copy.deepcopy(base)
            manifest["visualizations"][0][field] = value
            write(self.root / ".vegavisuals.yml", safe_dump(manifest, sort_keys=False))
            with self.subTest(field=field, value=value), self.assertRaises(ManifestError):
                self.registry.visualization_status()

    def test_lock_requires_exact_version_and_documented_fields(self) -> None:
        write(self.root / LOCK_NAME, "{not-json")
        with self.assertRaises(ManifestError):
            self.registry._load_lock()

        write(self.root / LOCK_NAME, '{"version":true,"visualizations":{}}')
        with self.assertRaises(ManifestError):
            self.registry._load_lock()

        with self.assertRaisesRegex(ManifestError, "exceeds"):
            self.registry._lock_bytes(
                {"version": LOCK_VERSION, "visualizations": {"oversized": "x" * MAX_LOCK_BYTES}}
            )
        write(
            self.root / LOCK_NAME,
            json.dumps(
                {
                    "version": LOCK_VERSION,
                    "visualizations": {
                        "broken": {"output": "broken.svg", "fingerprint": "0" * 64, "output_sha256": "0" * 64}
                    },
                }
            ),
        )
        with self.assertRaises(ManifestError):
            self.registry._load_lock()

        (self.root / LOCK_NAME).unlink()
        self.registry.render_visualization_text(vl_spec(), output_path="inline.svg")
        lock = json.loads((self.root / LOCK_NAME).read_text(encoding="utf-8"))
        entry = next(iter(lock["visualizations"].values()))
        entry["source"] = "inline:not-a-sha256"
        write(self.root / LOCK_NAME, json.dumps(lock))
        with self.assertRaises(ManifestError):
            self.registry._load_lock()

        for invalid_path in ("../escape.svg", "bad\0path.svg", "~missing-user/chart.svg", "bad\ud800.svg"):
            entry["source"] = "inline:" + "0" * 64
            entry["output"] = invalid_path
            write(self.root / LOCK_NAME, json.dumps(lock))
            with self.subTest(path=invalid_path), self.assertRaises(ManifestError):
                self.registry._load_lock()


class InlineCacheTest(TemporaryProject):
    def test_content_addressed_cache_and_opt_in_data(self) -> None:
        first = self.registry.render_visualization_text(vl_spec())
        self.assertTrue(first["rendered"])
        self.assertFalse(first["artifact"]["data_included"])
        render_count = len(self.runner.render_commands)
        second = self.registry.render_visualization_text(vl_spec(), include_data=True)
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.runner.render_commands), render_count)
        self.assertIn("svg", second["artifact"])
        self.assertTrue(second["artifact"]["data_included"])

    def test_binary_data_is_base64_only_when_requested(self) -> None:
        png = self.registry.render_visualization_text(vl_spec(), output_format="png", include_data=True)
        self.assertIn("data_base64", png["artifact"])
        self.assertNotIn("svg", png["artifact"])
        plain = self.registry.render_visualization_text(vl_spec(value=2), output_format="png")
        self.assertNotIn("data_base64", plain["artifact"])

    def test_cache_pair_rolls_back_when_metadata_publication_fails(self) -> None:
        runner = DockerMock()
        registry = CacheMetadataFailRegistry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        with self.assertRaisesRegex(OSError, "cache metadata publication failure"):
            registry.render_visualization_text(vl_spec())
        cache = self.root / ".cache/vegavisuals/text"
        self.assertEqual(list(cache.glob("*")) if cache.exists() else [], [])

    def test_explicit_inline_lock_failure_rolls_back_output_and_lock(self) -> None:
        self.registry.render_visualization_text(vl_spec(value=1), output_path="out/chart.svg")
        output_before = (self.root / "out/chart.svg").read_bytes()
        lock_before = (self.root / LOCK_NAME).read_bytes()
        runner = DockerMock()
        registry = LockPublicationFailRegistry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")
        with self.assertRaisesRegex(OSError, "lock publication failure"):
            registry.render_visualization_text(vl_spec(value=2), output_path="out/chart.svg")
        self.assertEqual((self.root / "out/chart.svg").read_bytes(), output_before)
        self.assertEqual((self.root / LOCK_NAME).read_bytes(), lock_before)

    def test_concurrent_explicit_inline_outputs_merge_lock_entries(self) -> None:
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results = context.Queue()
        contract = self.registry._renderer_contract(self.registry.assets / "compat/vl-convert-1.9.0.json")
        processes = [
            context.Process(
                target=concurrent_inline_process,
                args=(str(self.root), name, value, barrier, contract, results),
            )
            for name, value in (("one", 1), ("two", 2))
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertFalse(process.is_alive(), "concurrent inline render process did not finish")
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([results.get(timeout=2) for _ in processes], [None, None])
        lock = json.loads((self.root / LOCK_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(lock["visualizations"]), 2)


class ArtifactValidationTest(TemporaryProject):
    def test_png_pdf_and_svg_are_structurally_validated(self) -> None:
        self.registry._validate_artifact_data(png_artifact(), "png", max_bytes=1024 * 1024)
        self.registry._validate_artifact_data(pdf_artifact(), "pdf", max_bytes=1024 * 1024)
        with self.assertRaises(RenderError):
            self.registry._validate_artifact_data(b"\x89PNG\r\n\x1a\n", "png", max_bytes=1024)
        with self.assertRaises(RenderError):
            self.registry._validate_artifact_data(b"%PDF-1.4\n%%EOF\n", "pdf", max_bytes=1024)
        external = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.invalid/x.png"/></svg>'
        with self.assertRaises(RenderError):
            self.registry._validate_artifact_data(external, "svg", max_bytes=1024)
        imported = b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.invalid/x.css";</style></svg>'
        with self.assertRaises(RenderError):
            self.registry._validate_artifact_data(imported, "svg", max_bytes=1024)
        embedded = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/svg+xml,&lt;svg/&gt;"/></svg>'
        with self.assertRaises(RenderError):
            self.registry._validate_artifact_data(embedded, "svg", max_bytes=1024)
        safe = b'<svg xmlns="http://www.w3.org/2000/svg"><use href="#local"/></svg>'
        self.registry._validate_artifact_data(safe, "svg", max_bytes=1024)

    def test_renderer_created_symlink_is_rejected_without_reading_its_target(self) -> None:
        outside = self.root / "outside.svg"
        write(outside, '<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        write(self.root / "chart.vl.json", vl_spec())
        runner = StagedOutputSwapDockerMock(outside)
        registry = Registry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "safe regular staged output"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertFalse((self.root / "chart.svg").exists())

    def test_renderer_created_fifo_is_rejected_without_blocking(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        runner = StagedOutputSwapDockerMock(None)
        registry = Registry(self.root, runner=runner)
        runner.renderer_contract = registry._renderer_contract(registry.assets / "compat/vl-convert-1.9.0.json")

        with self.assertRaisesRegex(RenderError, "safe regular staged output"):
            registry.render_visualization("chart.vl.json", "chart.svg")

        self.assertFalse((self.root / "chart.svg").exists())


class FakeMCP:
    def __init__(self, name: str) -> None:
        self.name = name
        self.resources: dict[str, object] = {}
        self.tools: dict[str, object] = {}

    def resource(self, uri: str):  # type: ignore[no-untyped-def]
        def decorate(function):  # type: ignore[no-untyped-def]
            self.resources[uri] = function
            return function

        return decorate

    def tool(self):  # type: ignore[no-untyped-def]
        def decorate(function):  # type: ignore[no-untyped-def]
            self.tools[function.__name__] = function
            return function

        return decorate


class MCPContractTest(TemporaryProject):
    def test_adapter_registers_exact_tools_against_one_fixed_registry(self) -> None:
        write(self.root / "chart.vl.json", vl_spec())
        server = create_server(self.registry, FakeMCP)
        self.assertEqual(tuple(server.tools), TOOL_NAMES)
        self.assertEqual(tuple(server.resources), RESOURCE_URIS)
        for function in server.tools.values():
            self.assertNotIn("project", inspect.signature(function).parameters)
            self.assertNotIn("project_root", inspect.signature(function).parameters)
        validated = server.tools["validate_visualization"]("chart.vl.json")
        self.assertTrue(validated["ok"])
        blocked = server.tools["validate_visualization"]("../outside.vl.json")
        self.assertFalse(blocked["ok"])

        inferred = server.tools["render_visualization_text"](vl_spec(), output_path="inline.png")
        self.assertTrue(inferred["ok"])
        self.assertEqual(inferred["format"], "png")

    def test_two_fixed_server_roots_render_concurrently_without_crossing_state(self) -> None:
        with tempfile.TemporaryDirectory() as other_tmp:
            other_root = pathlib.Path(other_tmp)
            other_runner = DockerMock()
            other_registry = Registry(other_root, runner=other_runner)
            other_runner.renderer_contract = other_registry._renderer_contract(
                other_registry.assets / "compat/vl-convert-1.9.0.json"
            )
            first_server = create_server(self.registry, FakeMCP)
            second_server = create_server(other_registry, FakeMCP)
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            first_server.tools["render_visualization_text"],
                            vl_spec(value=1),
                            output_path="out/first.svg",
                        ),
                        executor.submit(
                            second_server.tools["render_visualization_text"],
                            vl_spec(value=2),
                            output_path="out/second.svg",
                        ),
                    ]
                    results = [future.result(timeout=15) for future in futures]

                self.assertTrue(all(result["ok"] for result in results), results)
                self.assertTrue((self.root / "out/first.svg").is_file())
                self.assertFalse((self.root / "out/second.svg").exists())
                self.assertTrue((other_root / "out/second.svg").is_file())
                self.assertFalse((other_root / "out/first.svg").exists())
                first_lock = json.loads((self.root / LOCK_NAME).read_text(encoding="utf-8"))
                second_lock = json.loads((other_root / LOCK_NAME).read_text(encoding="utf-8"))
                self.assertEqual(
                    {entry["output"] for entry in first_lock["visualizations"].values()},
                    {"out/first.svg"},
                )
                self.assertEqual(
                    {entry["output"] for entry in second_lock["visualizations"].values()},
                    {"out/second.svg"},
                )
                first_labels = [
                    command[index + 1]
                    for command in self.runner.render_commands
                    for index, value in enumerate(command)
                    if value == "--label"
                ]
                second_labels = [
                    command[index + 1]
                    for command in other_runner.render_commands
                    for index, value in enumerate(command)
                    if value == "--label"
                ]
                self.assertIn(f"{CONTAINER_WORKSPACE_LABEL}={workspace_id(self.root)}", first_labels)
                self.assertIn(f"{CONTAINER_WORKSPACE_LABEL}={workspace_id(other_root)}", second_labels)
                self.assertNotEqual(workspace_id(self.root), workspace_id(other_root))
            finally:
                other_registry.close()

    def test_cli_contract_commands_return_json(self) -> None:
        for arguments in (["version"], ["theme-inventory"], ["mcp", "list-tools"]):
            completed = subprocess.run(
                [sys.executable, "-m", "vegavisuals.cli", "--project", str(self.root), *arguments],
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["ok"])

    def test_cli_argument_and_stdin_errors_are_structured_json(self) -> None:
        invalid = subprocess.run(
            [sys.executable, "-m", "vegavisuals.cli", "validate", "chart.vl.json", "--engine", "invalid"],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["error"]["type"], "ArgumentError")
        oversized = subprocess.run(
            [sys.executable, "-m", "vegavisuals.cli", "--project", str(self.root), "render-text"],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            input="x" * (1024 * 1024 + 1),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(oversized.returncode, 1)
        self.assertEqual(json.loads(oversized.stdout)["error"]["type"], "ValidationError")

    def test_mcp_startup_error_does_not_write_to_protocol_stdout(self) -> None:
        missing = self.root / "missing-consumer"
        completed = subprocess.run(
            [sys.executable, "-m", "vegavisuals.cli", "mcp", "serve"],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "MCP_CONSUMER_WORKSPACE": str(missing),
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["error"]["type"], "PolicyError")


if __name__ == "__main__":
    unittest.main()
