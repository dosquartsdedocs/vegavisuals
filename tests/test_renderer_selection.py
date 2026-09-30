from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

from vegavisuals import Registry
from vegavisuals.cli import main
from vegavisuals.errors import PolicyError, RenderError, ValidationError
from vegavisuals.mcp_server import create_server
from vegavisuals.registry import LOCK_NAME, RECEIPT_PATH, RENDERER_IMAGE_ID_ENV
from tests.test_registry import DockerMock, FakeMCP, TemporaryProject, vg_spec, vl_spec, write


IMAGE_A = "sha256:" + "1" * 64
IMAGE_B = "sha256:" + "2" * 64


class RendererSelectionTest(TemporaryProject):
    def selected(self, image=IMAGE_A):
        runner = DockerMock()
        runner.image_id = image
        runner.renderer_contract = self.runner.renderer_contract
        registry = Registry(self.root, runner=runner, renderer_image_id=image)
        self.addCleanup(registry.close)
        return registry, runner

    def manifest(self):
        write(self.root / "chart.vl.json", vl_spec("data.csv"))
        write(self.root / "data.csv", "x,y\nA,1\nB,2\n")
        raw = json.loads(vg_spec())
        raw["data"] = [{"name": "table", "url": "data.csv"}]
        write(self.root / "chart.vg.json", json.dumps(raw))
        write(self.root / ".vegavisuals.yml", json.dumps({
            "version": 1, "profile": "vl-convert-1.9.0", "family": "benizar",
            "visualizations": [
                {"name": "lite", "source": "chart.vl.json", "output": "lite.svg"},
                {"name": "raw", "source": "chart.vg.json", "output": "raw.svg"},
            ],
        }))

    def test_reject_mutable_abbreviated_or_malformed_selections_before_docker(self):
        for value in ("", "latest", "image:tag", "sha256:123", "1" * 64, IMAGE_A + "\n",
                      "sha256:" + "A" * 64, "repo@" + IMAGE_A, 123):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Registry(self.root, runner=self.runner, renderer_image_id=value)
        with patch.dict(os.environ, {RENDERER_IMAGE_ID_ENV: ""}), self.assertRaises(ValidationError):
            Registry(self.root, runner=self.runner)
        self.assertEqual(self.runner.calls, [])

    def test_startup_binding_api_and_cli_precedence_and_client_configuration(self):
        with patch.dict(os.environ, {RENDERER_IMAGE_ID_ENV: IMAGE_A}):
            bound = Registry(self.root, runner=self.runner)
            self.addCleanup(bound.close)
            override, _ = self.selected(IMAGE_B)
            self.assertEqual(override.renderer_image_id, IMAGE_B)
            with patch("vegavisuals.cli._print") as output:
                self.assertEqual(main(["--renderer-image-id", IMAGE_B, "mcp", "client-config"]), 0)
                self.assertEqual(output.call_args.args[0]["mcpServers"]["vegavisuals"]["env"][RENDERER_IMAGE_ID_ENV], IMAGE_B)
        with patch.dict(os.environ, {RENDERER_IMAGE_ID_ENV: IMAGE_B}):
            self.assertEqual(bound.ensure_renderer()["image_id"], IMAGE_A)
            self.assertFalse(bound.ensure_renderer()["built"])
            for vscode in (False, True):
                config = bound.client_config(vscode=vscode)
                server = config["servers" if vscode else "mcpServers"]["vegavisuals"]
                self.assertEqual(server["command"], sys.executable)
                self.assertEqual(server["env"][RENDERER_IMAGE_ID_ENV], IMAGE_A)

    def test_missing_wrong_identity_and_wrong_contract_cannot_reuse_or_mutate_outputs(self):
        self.manifest()
        registry, runner = self.selected()
        registry.render_visualizations()
        inline = registry.render_visualization_text(vl_spec(), output_path="inline.svg")
        protected = [LOCK_NAME, "lite.svg", "raw.svg", "inline.svg", inline["cache"],
                     inline["cache"].rsplit(".", 1)[0] + ".json"]
        originals = {path: (self.root / path).read_bytes() for path in protected}
        for image, contract in ((IMAGE_A, ""), (IMAGE_B, self.runner.renderer_contract), (IMAGE_A, "0" * 64)):
            with self.subTest(image=image, contract=contract):
                runner.image_id, runner.renderer_contract = IMAGE_A, self.runner.renderer_contract
                registry.visualization_check()
                runner.image_id, runner.renderer_contract = image, contract
                runner.calls.clear()
                for action in (
                    registry.ensure_renderer,
                    registry.visualization_status,
                    registry.visualization_check,
                    lambda: registry.validate_visualization("chart.vl.json"),
                    lambda: registry.render_visualization("chart.vl.json", "lite.svg"),
                    lambda: registry.render_visualization("chart.vl.json", "lite.svg", dry_run=True),
                    lambda: registry.render_visualization_text(vl_spec(), output_path="inline.svg"),
                    lambda: registry.render_visualization_text(vl_spec(), dry_run=True),
                    lambda: registry.export_visualization_bundle("lite.svg", "unavailable-bundle"),
                ):
                    with self.assertRaisesRegex(RenderError, "explicit renderer image"):
                        action()
                self.assertFalse(registry.factory_check()["ok"])
                server = create_server(registry, FakeMCP)
                failure = server.tools["render_visualization_text"](vl_spec())
                self.assertFalse(failure["ok"])
                self.assertEqual(failure["error"]["type"], "RenderError")
                self.assertTrue(all(call[:3] == ["docker", "image", "inspect"] and call[-1] == IMAGE_A
                                    for call in runner.calls))
                self.assertEqual({path: (self.root / path).read_bytes() for path in protected}, originals)
                self.assertFalse(json.loads((self.root / RECEIPT_PATH).read_bytes())["ok"])

    def test_explicit_build_is_disabled_including_dry_run(self):
        registry, runner = self.selected()
        for dry_run in (False, True):
            with self.assertRaises(PolicyError):
                registry.build_renderer(dry_run=dry_run)
        self.assertEqual(runner.calls, [])

    def test_two_runtimes_change_freshness_receipts_and_bundle_provenance(self):
        self.manifest()
        first, runner_a = self.selected()
        second, runner_b = self.selected(IMAGE_B)
        bundles = []
        for index, (registry, other, image) in enumerate(((first, second, IMAGE_A), (second, first, IMAGE_B))):
            self.assertEqual(registry.render_visualizations()["rendered"], 2)
            self.assertTrue(registry.visualization_check()["ok"])
            self.assertEqual(registry.render_visualizations()["skipped"], 2)
            lock = json.loads((self.root / LOCK_NAME).read_bytes())
            for name in ("lite", "raw"):
                self.assertEqual(lock["visualizations"][name]["renderer"]["image"], image)
                self.assertEqual(lock["visualizations"][name]["renderer"]["image_id"], image)
                bundle = registry.export_visualization_bundle(name + ".svg", f"bundles/{index}-{name}")
                doc = json.loads((self.root / bundle["path"]).read_bytes())
                self.assertEqual(doc["producer"]["runtimes"][0]["revision"], image)
                bundles.append(bundle)
                with self.assertRaisesRegex(ValidationError, "stale"):
                    other.export_visualization_bundle(name + ".svg", "wrong-runtime")
            self.assertEqual(other.visualization_status()["counts"], {"stale": 2})
            self.assertFalse(other.visualization_check()["ok"])
            self.assertFalse(json.loads((self.root / RECEIPT_PATH).read_bytes())["ok"])
        for runner, image in ((runner_a, IMAGE_A), (runner_b, IMAGE_B)):
            self.assertTrue(all(image in command for command in runner.render_commands))
            self.assertFalse(any(command[:2] == ["docker", "build"] for command in runner.calls))
            runner.renderer_contract = ""
            runner.calls.clear()
        for bundle in bundles:
            self.assertTrue(second.check_visualization_bundle(bundle["path"], bundle["sha256"])["ok"])
        self.assertEqual(runner_b.calls, [])

    def test_inline_caches_survive_a_b_a_switch_without_cross_runtime_reuse(self):
        first, runner_a = self.selected()
        second, _ = self.selected(IMAGE_B)
        for engine, spec in (("vega-lite", vl_spec()), ("vega", vg_spec())):
            with self.subTest(engine=engine):
                a = first.render_visualization_text(spec)
                before = (self.root / a["cache"]).read_bytes()
                b = second.render_visualization_text(spec)
                self.assertNotEqual(a["fingerprint"], b["fingerprint"])
                self.assertNotEqual(a["cache"], b["cache"])
                self.assertEqual((self.root / a["cache"]).read_bytes(), before)
                render_count = len(runner_a.render_commands)
                again = first.render_visualization_text(spec)
                self.assertTrue(again["cached"])
                self.assertFalse(again["rendered"])
                self.assertEqual(len(runner_a.render_commands), render_count)
                for registry, result, image in ((first, a, IMAGE_A), (second, b, IMAGE_B)):
                    bundle = registry.export_visualization_bundle(result["cache"], f"inline/{engine}-{image[-1]}", visualization_text=spec)
                    request = json.loads((self.root / bundle["path"]).parent.joinpath("payload/request.json").read_bytes())
                    self.assertEqual(request["renderer"]["image_id"], image)

    def test_selection_change_preserves_modified_outputs_and_profile_contract(self):
        self.manifest()
        first, _ = self.selected()
        second, runner = self.selected(IMAGE_B)
        before = {path: path.read_bytes() for path in first.assets.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
        first.render_visualizations()
        write(self.root / "lite.svg", "author edit")
        with self.assertRaisesRegex(PolicyError, "modified"):
            second.render_visualization("chart.vl.json", "lite.svg")
        self.assertEqual((self.root / "lite.svg").read_text(), "author edit")
        self.assertEqual(runner.render_commands, [])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(first._renderer_contract(first.assets / "compat/vl-convert-1.9.0.json"),
                         "28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800")

    def test_fresh_fingerprint_does_not_hide_conflicting_provenance(self):
        self.manifest()
        registry, _ = self.selected()
        registry.render_visualizations()
        lock = json.loads((self.root / LOCK_NAME).read_bytes())
        lock["visualizations"]["lite"]["renderer"]["image_id"] = IMAGE_B
        write(self.root / LOCK_NAME, json.dumps(lock))
        self.assertEqual(registry.visualization_status()["counts"], {"stale": 1, "fresh": 1})
        with self.assertRaisesRegex(ValidationError, "explicit selection"):
            registry.export_visualization_bundle("lite.svg", "conflicting-runtime")

    def test_dynamic_descriptors_keep_selection_for_every_lifecycle_command(self):
        registry, _ = self.selected()
        for installed in (False, True):
            with self.subTest(installed=installed):
                with patch("vegavisuals.registry.source_checkout", return_value=None) if installed else patch.dict(os.environ, {}):
                    manifest = registry.factory_manifest()
                    check = registry.factory_check()
                self.assertTrue(check["ok"], check)
                self.assertEqual(manifest["transport"]["env"][RENDERER_IMAGE_ID_ENV], IMAGE_A)
                for command in [manifest["transport"]["command"], *manifest["commands"].values()]:
                    if installed:
                        self.assertEqual(command[:5], [sys.executable, "-m", "vegavisuals.cli", "--renderer-image-id", IMAGE_A])
                    else:
                        self.assertEqual(command[:2], ["env", f"{RENDERER_IMAGE_ID_ENV}={IMAGE_A}"])
