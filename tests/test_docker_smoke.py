from __future__ import annotations

import csv
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vegavisuals.registry import CONTAINER_LABEL, CONTAINER_WORKSPACE_LABEL, Registry, workspace_id


def docker_mount_source(command: list[str], target: str) -> pathlib.Path:
    for index, value in enumerate(command):
        if value != "--mount":
            continue
        fields = next(csv.reader([command[index + 1]]))
        options = dict(field.split("=", 1) for field in fields if "=" in field)
        if options.get("target") == target:
            return pathlib.Path(options["source"])
    raise AssertionError(f"mount target not found: {target}")


def docker_available() -> bool:
    if os.environ.get("VEGAVISUALS_DOCKER_SMOKE") != "1" or shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


@unittest.skipUnless(docker_available(), "set VEGAVISUALS_DOCKER_SMOKE=1 with a reachable Docker daemon")
class DockerSmokeTest(unittest.TestCase):
    def test_down_removes_only_the_selected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            projects = [base / "first", base / "second"]
            for project in projects:
                project.mkdir()
            registries = [Registry(project) for project in projects]
            names = [f"vegavisuals-cleanup-{os.getpid()}-{index}" for index in range(2)]
            try:
                image = registries[0].ensure_renderer()
                self.assertTrue(image["ok"], image)
                for name, project in zip(names, projects, strict=True):
                    created = subprocess.run(
                        [
                            "docker",
                            "create",
                            "--name",
                            name,
                            "--label",
                            CONTAINER_LABEL,
                            "--label",
                            f"{CONTAINER_WORKSPACE_LABEL}={workspace_id(project)}",
                            "--entrypoint",
                            "/bin/sh",
                            image["image_id"],
                            "-c",
                            "exit 0",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(created.returncode, 0, created.stderr)

                removed = registries[0].down()
                self.assertTrue(removed["ok"], removed)
                self.assertEqual(len(removed["containers"]), 1)
                first = subprocess.run(
                    ["docker", "container", "inspect", names[0]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                second = subprocess.run(
                    ["docker", "container", "inspect", names[1]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                self.assertNotEqual(first.returncode, 0)
                self.assertEqual(second.returncode, 0)
            finally:
                subprocess.run(
                    ["docker", "container", "rm", "--force", *names],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                for registry in registries:
                    registry.close()

    def test_real_render_for_both_engines_and_all_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "project"
            tmpdir = base / 'tmp,with"quote'
            root.mkdir()
            tmpdir.mkdir()
            shutil.copytree(REPO_ROOT / "examples", root / "examples")

            previous_tempdir = tempfile.tempdir
            with mock.patch.dict(os.environ, {"TMPDIR": str(tmpdir)}):
                tempfile.tempdir = None
                registry: Registry | None = None
                try:
                    self.assertEqual(pathlib.Path(tempfile.gettempdir()), tmpdir)
                    registry = Registry(root)
                    sources = {
                        "vega-lite": "examples/vega-lite/bar.vl.json",
                        "vega": "examples/vega/raw.vg.json",
                    }
                    signatures = {
                        "svg": b"<svg",
                        "png": b"\x89PNG\r\n\x1a\n",
                        "pdf": b"%PDF-",
                    }
                    for engine, source in sources.items():
                        for output_format, signature in signatures.items():
                            with self.subTest(engine=engine, output_format=output_format):
                                output = root / "out" / f"{engine}.{output_format}"
                                result = registry.render_visualization(
                                    source,
                                    str(output.relative_to(root)),
                                    output_format=output_format,
                                )
                                self.assertTrue(result["ok"], result)
                                self.assertEqual(result["engine"], engine)
                                self.assertEqual(docker_mount_source(result["command"], "/output").parent, tmpdir)
                                self.assertIn(signature, output.read_bytes()[:4096])
                                if engine == "vega-lite" and output_format == "svg":
                                    self.assertIn(b"Q1", output.read_bytes())
                                if output_format == "pdf":
                                    first = output.read_bytes()
                                    time.sleep(1.1)
                                    registry.render_visualization(
                                        source,
                                        str(output.relative_to(root)),
                                        output_format=output_format,
                                        force=True,
                                    )
                                    self.assertEqual(output.read_bytes(), first)
                finally:
                    if registry is not None:
                        registry.close()
                    tempfile.tempdir = previous_tempdir


if __name__ == "__main__":
    unittest.main()
