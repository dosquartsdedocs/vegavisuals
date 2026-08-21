from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vegavisuals.registry import Registry


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
    def test_real_render_for_both_engines_and_all_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            shutil.copytree(REPO_ROOT / "examples", root / "examples")
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


if __name__ == "__main__":
    unittest.main()
