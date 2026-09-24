from __future__ import annotations

import copy
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import patch

from vegavisuals import Registry
from vegavisuals.cli import build_parser, dispatch
from vegavisuals.errors import PolicyError, RenderError, ValidationError
from vegavisuals.handoff import STAGING, Reader, digest, encode, parse, paths_unique, safe_path
from vegavisuals.mcp_server import create_server
from vegavisuals.registry import LOCK_NAME, MANIFEST_NAME, RECEIPT_PATH
from tests.test_registry import CallbackDockerMock, DockerMock, FakeMCP, LockPublicationFailRegistry, vl_spec, vg_spec, write


REFERENCE_REVISION = "9167e3efb5968a64bb9100792163a179c1491860"
REFERENCE_PREFIX = "src/bash/mcp_factories/"


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vegavisuals-handoff-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.root = self.base / "producer"
        self.root.mkdir()
        self.runner = DockerMock()
        self.registry = Registry(self.root, runner=self.runner)
        self.addCleanup(self.registry.close)

    def render(self, engine="vega-lite", output_format="svg", manifest=False):
        source = "charts/quarter.vl.json" if engine == "vega-lite" else "charts/quarter.vg.json"
        spec = json.loads(vl_spec("data/àrea.csv")) if engine == "vega-lite" else json.loads(vg_spec())
        if engine == "vega":
            spec["data"] = [{"name": "table", "url": "data/àrea.csv"}]
        write(self.root / source, json.dumps(spec, ensure_ascii=False))
        write(self.root / "data/àrea.csv", "x,y\nA,1\nB,2\n")
        write(self.root / "inputs/review.txt", "reviewed input\n")
        output = f"out/{engine}.{output_format}"
        if manifest:
            write(self.root / ".vegavisuals.yml", encode({
                "version": 1, "profile": "vl-convert-1.9.0", "family": "benizar",
                "inputs": ["inputs/review.txt"],
                "visualizations": [{"name": "quarter", "source": source, "output": output}],
            }))
            self.registry.render_visualizations()
            self.registry.visualization_check()
        else:
            self.registry.render_visualization(source, output, inputs=["inputs/review.txt"])
        return source, output

    def export(self, output, target="bundles/quarter", **kwargs):
        result = self.registry.export_visualization_bundle(output, target, **kwargs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], target + "/bundle.json")
        self.assertEqual(result["sha256"], digest((self.root / result["path"]).read_bytes()))
        return result

    def test_both_engines_all_formats_manifest_local_data_and_relocation(self):
        for engine in ("vega-lite", "vega"):
            for fmt in ("svg", "png", "pdf"):
                with self.subTest(engine=engine, format=fmt):
                    source, output = self.render(engine, fmt, manifest=True)
                    native = {path: (self.root / path).read_bytes() for path in (LOCK_NAME, RECEIPT_PATH)}
                    result = self.export(output, f"bundles/{engine}-{fmt}")
                    for path, before in native.items():
                        self.assertEqual((self.root / path).read_bytes(), before)
                    evidence = (self.root / result["path"]).parent / "payload/evidence"
                    self.assertEqual((evidence / "native-lock.json").read_bytes(), native[LOCK_NAME])
                    self.assertEqual((evidence / "native-receipt.json").read_bytes(), native[RECEIPT_PATH])
                    doc = json.loads((self.root / result["path"]).read_bytes())
                    request = json.loads((self.root / result["path"]).parent.joinpath("payload/request.json").read_bytes())
                    self.assertEqual(request["inputs"], ["data/àrea.csv", "inputs/review.txt"])
                    self.assertEqual(doc["producer"]["runtimes"][0]["revision"], self.runner.image_id)
                    self.assertEqual((self.root / result["path"]).parent.joinpath("payload/project", source).read_bytes(), (self.root / source).read_bytes())
                    self.assertTrue(self.registry.check_visualization_bundle(result["path"], result["sha256"])["ok"])
        final = self.base / "final-consumer"
        shutil.copytree(self.root / "bundles", final / "retained")
        self.registry.close()
        shutil.rmtree(self.root)
        relocated = self.base / "relocated"
        final.rename(relocated)
        registry = Registry(relocated, runner=lambda *a, **kw: self.fail("relocation checker invoked Docker"))
        self.addCleanup(registry.close)
        for manifest in (relocated / "retained").glob("*/bundle.json"):
            result = registry.check_visualization_bundle(manifest.relative_to(relocated).as_posix(), digest(manifest.read_bytes()))
            self.assertTrue(result["ok"])

    def test_inline_exact_bytes_cache_and_explicit_output(self):
        for engine, spec in (("vega-lite", vl_spec()), ("vega", vg_spec())):
            for explicit in (False, True):
                with self.subTest(engine=engine, explicit=explicit):
                    text = " \n" + spec + "\n"
                    rendered = self.registry.render_visualization_text(text, output_path=f"{engine}.svg" if explicit else None)
                    output = rendered["artifact"]["path"]
                    result = self.export(output, f"bundles/{engine}-{explicit}", visualization_text=text)
                    bundle = (self.root / result["path"]).parent
                    request = json.loads((bundle / "payload/request.json").read_bytes())
                    self.assertEqual((bundle / "payload/project" / request["source"]).read_bytes(), text.encode())
                    self.assertEqual(json.loads((bundle / "bundle.json").read_bytes())["dependencies"], [])
                    with self.assertRaisesRegex(ValidationError, "original specification"):
                        self.registry.export_visualization_bundle(output, "missing-text")
                    with self.assertRaises(ValidationError):
                        self.registry.export_visualization_bundle(output, "wrong-text", visualization_text=spec)

    def test_actual_renderer_from_lock_not_current_local_image(self):
        _, output = self.render()
        self.runner.image_id = "sha256:" + "2" * 64
        self.runner.calls.clear()
        result = self.export(output)
        self.assertEqual(self.runner.calls, [])
        doc = json.loads((self.root / result["path"]).read_bytes())
        self.assertEqual(doc["producer"]["runtimes"][0]["revision"], "sha256:" + "1" * 64)

    def test_explicit_author_variant_retains_original(self):
        _, output = self.render()
        edited = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Reviewed</text></svg>'
        write(self.root / "review.edited.svg", edited)
        result = self.export(output, edited_output_path="review.edited.svg")
        bundle = (self.root / result["path"]).parent
        doc = json.loads((bundle / "bundle.json").read_bytes())
        variant = next(item for item in doc["files"] if item["id"] == "edited-output")
        self.assertEqual(variant["variant_of"], "output")
        self.assertEqual(variant["ownership"], "author")
        self.assertEqual((bundle / variant["path"]).read_bytes(), edited)
        self.assertEqual((bundle / "payload/output/visualization.svg").read_bytes(), (self.root / output).read_bytes())

    def test_stale_missing_modified_and_unmanaged_fail_without_export(self):
        source, output = self.render()
        for path, replacement in ((source, vl_spec(value=8).encode()), ("data/àrea.csv", b"changed"),
                                  ("inputs/review.txt", b"changed"), (output, b"author edit")):
            original = (self.root / path).read_bytes()
            for missing in (False, True):
                with self.subTest(path=path, missing=missing):
                    if missing:
                        (self.root / path).unlink()
                    else:
                        (self.root / path).write_bytes(replacement)
                    with self.assertRaises((ValidationError, OSError)):
                        self.registry.export_visualization_bundle(output, "bundles/rejected")
                    self.assertFalse((self.root / "bundles/rejected").exists())
                    write(self.root / path, original)
        shutil.copyfile(self.root / output, self.root / "moved.svg")
        with self.assertRaisesRegex(ValidationError, "integration records"):
            self.registry.export_visualization_bundle("moved.svg", "bundles/rejected")

    def test_missing_modified_or_undeclared_retained_files_rejected(self):
        _, output = self.render()
        result = self.export(output)
        bundle = (self.root / result["path"]).parent
        data = bundle / "payload/project/data/àrea.csv"
        raw = data.read_bytes()
        data.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "hash/size"):
            self.registry.check_visualization_bundle(result["path"], result["sha256"])
        data.unlink()
        with self.assertRaisesRegex(ValidationError, "tree"):
            self.registry.check_visualization_bundle(result["path"], result["sha256"])
        data.write_bytes(raw)
        (bundle / "empty").mkdir()
        with self.assertRaisesRegex(ValidationError, "tree"):
            self.registry.check_visualization_bundle(result["path"], result["sha256"])

    def test_resealed_incomplete_inventory_fails_domain_checks(self):
        _, output = self.render()
        result = self.export(output)
        manifest = self.root / result["path"]
        doc = json.loads(manifest.read_bytes())
        removed = next(item for item in doc["files"] if item["role"] == "local-data")
        doc["files"].remove(removed)
        path = manifest.parent / removed["path"]
        path.unlink()
        path.parent.rmdir()
        manifest.write_bytes(encode(doc))
        with self.assertRaisesRegex(ValidationError, "unretained local reference"):
            self.registry.check_visualization_bundle(result["path"], digest(manifest.read_bytes()))

    def test_malformed_manifest_strict_tokens_fields_and_provenance(self):
        _, output = self.render()
        result = self.export(output)
        manifest = self.root / result["path"]
        original = json.loads(manifest.read_bytes())
        changes = [lambda doc: doc.update(schema_version=True), lambda doc: doc.update(schema_version=1.0),
                   lambda doc: doc.update(unreviewed=True), lambda doc: doc["producer"].update(revision="v0.3.1"),
                   lambda doc: doc["files"][0].update(bytes=True), lambda doc: doc.update(request="absent"),
                   lambda doc: doc.update(dependencies=[{"id": "child", "sha256": "0" * 64}])]
        for change in changes:
            doc = copy.deepcopy(original)
            change(doc)
            raw = encode(doc)
            manifest.write_bytes(raw)
            with self.assertRaises(ValidationError):
                self.registry.check_visualization_bundle(result["path"], digest(raw))
        for raw in (b'{"kind":1,"kind":2}', b'{"x":NaN}', b'{"x":1e999}', b'{}', b'[]'):
            manifest.write_bytes(raw)
            with self.assertRaises(ValidationError):
                self.registry.check_visualization_bundle(result["path"], digest(raw))

    def test_confinement_aliases_links_and_special_files(self):
        _, output = self.render()
        for path in ("../escape", "/outside", "foo//bar", "foo/./bar", "a\\b", "CON", "a\u0300", "a%20b", "a:b", "a."):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.registry.export_visualization_bundle(output, path)
        with self.assertRaises(ValidationError):
            paths_unique(["payload/Data/a", "payload/data/b"])
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.registry.export_visualization_bundle(output, "linked/bundle")
        self.assertEqual(list(outside.iterdir()), [])
        os.link(self.root / "data/àrea.csv", self.root / "hardlink")
        with self.assertRaisesRegex(ValidationError, "linked"):
            self.registry.export_visualization_bundle(output, "bundles/hardlink")
        (self.root / "hardlink").unlink()
        result = self.export(output)
        bundle = (self.root / result["path"]).parent
        data = bundle / "payload/project/data/àrea.csv"
        data.unlink()
        os.mkfifo(data)
        with self.assertRaisesRegex(ValidationError, "special"):
            self.registry.check_visualization_bundle(result["path"], result["sha256"])

    def test_symlinked_workspace_is_rejected(self):
        _, output = self.render()
        link = self.base / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        registry = Registry(link, runner=self.runner)
        self.addCleanup(registry.close)
        with self.assertRaises(OSError):
            registry.export_visualization_bundle(output, "bundle")

    def test_no_remote_or_inline_url_dependency(self):
        for engine, spec in (("vega-lite", vl_spec("https://example.org/a.csv")),
                             ("vega", json.dumps({"marks": [], "data": [{"name": "d", "url": "local.csv"}]}))):
            with self.subTest(engine=engine), self.assertRaises(PolicyError):
                self.registry.render_visualization_text(spec, engine=engine)
        self.assertEqual(self.runner.calls, [])

    def test_destination_collision_and_two_concurrent_exports(self):
        _, output = self.render()
        def perform():
            registry = Registry(self.root, runner=self.runner)
            try:
                return registry.export_visualization_bundle(output, "bundles/one")
            except PolicyError:
                return None
            finally:
                registry.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: perform(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        before = (self.root / "bundles/one/bundle.json").read_bytes()
        with self.assertRaises(PolicyError):
            self.registry.export_visualization_bundle(output, "bundles/one")
        self.assertEqual((self.root / "bundles/one/bundle.json").read_bytes(), before)

    def test_publication_interruption_retains_stage_and_native_bytes(self):
        _, output = self.render(manifest=True)
        native = {path: (self.root / path).read_bytes() for path in (LOCK_NAME, RECEIPT_PATH, output)}
        import vegavisuals.handoff as handoff
        original = handoff.check
        def interrupt(registry, path, sha):
            if path.startswith("bundles/"):
                raise KeyboardInterrupt()
            return original(registry, path, sha)
        with patch.object(handoff, "check", side_effect=interrupt), self.assertRaisesRegex(RenderError, "retained recovery"):
            self.registry.export_visualization_bundle(output, "bundles/failure")
        self.assertFalse((self.root / "bundles/failure").exists())
        stages = list((self.root / STAGING).glob("*.pending"))
        self.assertEqual(len(stages), 1)
        self.assertTrue((stages[0] / "bundle.json").is_file())
        for path, data in native.items():
            self.assertEqual((self.root / path).read_bytes(), data)
        recovered = self.root / "bundles/recovered"
        shutil.copytree(stages[0], recovered)
        self.assertTrue(self.registry.check_visualization_bundle("bundles/recovered/bundle.json", digest((recovered / "bundle.json").read_bytes()))["ok"])

    def test_concurrent_source_edit_aborts_and_preserves_edit(self):
        _, output = self.render()
        import vegavisuals.handoff as handoff
        original = handoff.check
        def edit(registry, path, sha):
            result = original(registry, path, sha)
            write(self.root / "data/àrea.csv", "late edit")
            return result
        with patch.object(handoff, "check", side_effect=edit), self.assertRaises(RenderError):
            self.registry.export_visualization_bundle(output, "bundles/failure")
        self.assertEqual((self.root / "data/àrea.csv").read_text(), "late edit")
        self.assertFalse((self.root / "bundles/failure").exists())
        self.assertTrue(list((self.root / STAGING).glob("*.pending")))

    def test_inline_lock_failure_rolls_back_cache_output_lock_together(self):
        self.registry.render_visualization_text(vl_spec(), output_path="output.svg")
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        registry = LockPublicationFailRegistry(self.root, runner=self.runner)
        self.addCleanup(registry.close)
        with self.assertRaises(OSError):
            registry.render_visualization_text(vl_spec(value=8), output_path="output.svg")
        after = {path.relative_to(self.root).as_posix(): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file() and "replaced" not in path.parts}
        self.assertEqual(before, after)
        self.assertTrue(list((self.root / ".cache/vegavisuals/replaced").iterdir()))

    def test_inline_transaction_failure_at_every_publication_boundary(self):
        self.registry.render_visualization_text(vl_spec(), output_path="output.svg")
        def managed():
            return {path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in self.root.rglob("*") if path.is_file() and "replaced" not in path.parts}
        before = managed()
        original = self.registry._replace_project_bytes
        for boundary in ("cache", "metadata", "output", "lock"):
            for interrupt in (False, True):
                def fail(path, raw, **kwargs):
                    selected = {"cache": path.startswith(".cache/vegavisuals/text/") and path.endswith(".svg"),
                                "metadata": path.startswith(".cache/vegavisuals/text/") and path.endswith(".json"),
                                "output": path == "output.svg", "lock": path == LOCK_NAME}[boundary]
                    publication = original(path, raw, **kwargs)
                    if selected:
                        if interrupt:
                            raise KeyboardInterrupt()
                        raise OSError("boundary failure")
                    return publication
                with self.subTest(boundary=boundary, interrupt=interrupt):
                    with patch.object(self.registry, "_replace_project_bytes", side_effect=fail), self.assertRaises((OSError, KeyboardInterrupt)):
                        self.registry.render_visualization_text(vl_spec(value=8), output_path="output.svg")
                    self.assertEqual(managed(), before)

    def test_concurrent_inline_cache_edit_is_not_overwritten(self):
        first = self.registry.render_visualization_text(vl_spec(), output_path="output.svg")
        cache = self.root / first["cache"]
        before = (self.root / LOCK_NAME).read_bytes()
        runner = CallbackDockerMock(lambda: cache.write_bytes(b"concurrent cache edit"))
        runner.renderer_contract = self.runner.renderer_contract
        registry = Registry(self.root, runner=runner)
        self.addCleanup(registry.close)
        with self.assertRaisesRegex(RenderError, "cache changed"):
            registry.render_visualization_text(vl_spec(), output_path="output.svg", force=True)
        self.assertEqual(cache.read_bytes(), b"concurrent cache edit")
        self.assertEqual((self.root / LOCK_NAME).read_bytes(), before)

    def test_bundle_publication_race_never_replaces_destination(self):
        _, output = self.render()
        import vegavisuals.registry as module
        original = module._renameat2
        def collide(source_fd, source, target_fd, target, flags):
            if source.endswith(".pending"):
                os.mkdir(target, dir_fd=target_fd)
                write(self.root / "bundles/race/author.txt", "keep this")
            return original(source_fd, source, target_fd, target, flags)
        with patch.object(module, "_renameat2", side_effect=collide), self.assertRaisesRegex(RenderError, "recovery tree"):
            self.registry.export_visualization_bundle(output, "bundles/race")
        self.assertEqual((self.root / "bundles/race/author.txt").read_text(), "keep this")
        self.assertTrue(list((self.root / STAGING).glob("*.pending/bundle.json")))

    def test_rollback_directory_race_restores_concurrent_author_tree(self):
        _, output = self.render()
        import vegavisuals.handoff as handoff
        import vegavisuals.registry as module
        original_check = handoff.check
        original_rename = module._renameat2
        def failed_check(registry, path, sha):
            if path.startswith("bundles/"):
                raise OSError("post-publication failure")
            return original_check(registry, path, sha)
        def replace_before_rollback(source_fd, source, target_fd, target, flags):
            if source == "race" and target.endswith(".pending"):
                os.rename(source, "displaced-by-author", src_dir_fd=source_fd, dst_dir_fd=source_fd)
                os.mkdir(source, dir_fd=source_fd)
                write(self.root / "bundles/race/author.txt", "late author tree")
            return original_rename(source_fd, source, target_fd, target, flags)
        with patch.object(handoff, "check", side_effect=failed_check), patch.object(module, "_renameat2", side_effect=replace_before_rollback):
            with self.assertRaises(RenderError):
                self.registry.export_visualization_bundle(output, "bundles/race")
        self.assertEqual((self.root / "bundles/race/author.txt").read_text(), "late author tree")
        self.assertTrue((self.root / "bundles/displaced-by-author/bundle.json").is_file())

    def test_bounds_and_malformed_domain_fail_closed(self):
        import vegavisuals.handoff as handoff
        for raw in (b'{"a":' + b'[' * 33 + b'0' + b']' * 33 + b'}', b'{"a":1,"a":2}'):
            with self.assertRaises(ValidationError):
                parse(raw)
        with self.assertRaises(ValidationError):
            safe_path("/".join(["a"] * 65))
        with self.assertRaises(ValidationError):
            Reader(pathlib.Path("/"))
        write(self.root / "file", b"12345")
        reader = Reader(self.root)
        try:
            with self.assertRaisesRegex(ValidationError, "too large"):
                reader.read("file", 4)
            with patch.object(handoff, "MAX_TOTAL", 4), self.assertRaisesRegex(ValidationError, "byte limit"):
                reader.read("file")
        finally:
            reader.close()
        _, output = self.render()
        result = self.export(output)
        manifest = self.root / result["path"]
        doc = json.loads(manifest.read_bytes())
        item = next(item for item in doc["files"] if item["id"] == "request")
        raw = b'{"version":1,"operation":"export_visualization_bundle"}'
        (manifest.parent / item["path"]).write_bytes(raw)
        item.update(sha256=digest(raw), bytes=len(raw))
        manifest.write_bytes(encode(doc))
        server = create_server(self.registry, FakeMCP)
        checked = server.tools["check_visualization_bundle"](result["path"], digest(manifest.read_bytes()))
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["error"]["type"], "ValidationError")

    def test_opt_in_ignore_customizations_and_dry_run(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        write(self.root / ".gitignore", "# author custom rules\n*.bak\n")
        _, output = self.render()
        before = (self.root / ".gitignore").read_bytes()
        result = self.registry.export_visualization_bundle(output, "bundles/quarter", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual((self.root / ".gitignore").read_bytes(), before)
        self.assertFalse((self.root / STAGING).exists())
        self.export(output)
        self.assertTrue((self.root / ".gitignore").read_bytes().startswith(before))
        subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", f"{STAGING}/future"], check=True)
        ignored = (self.root / ".gitignore").read_bytes()
        self.export(output, "bundles/second")
        self.assertEqual((self.root / ".gitignore").read_bytes(), ignored)
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".cache/vegavisuals/project.lock"], check=True)
        with self.assertRaisesRegex(ValidationError, "tracked cache"):
            self.registry.export_visualization_bundle(output, "bundles/blocked")

    def test_git_free_export_and_reserved_native_destinations(self):
        text = vl_spec()
        result = self.registry.render_visualization_text(text)
        output = result["artifact"]["path"]
        for target in (LOCK_NAME, MANIFEST_NAME, ".gitignore", ".cache/nested", ".unaltraweb/bundle"):
            with self.subTest(target=target), self.assertRaisesRegex(ValidationError, "native control"):
                self.registry.export_visualization_bundle(output, target, visualization_text=text)
        with patch("vegavisuals.handoff.shutil.which", return_value=None):
            self.export(output, visualization_text=text)

    def test_cli_mcp_parity_and_typed_failure(self):
        _, output = self.render()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(dispatch(build_parser().parse_args(["export-bundle", output, "bundle"]), self.registry), 0)
        result = json.loads(stdout.getvalue())
        server = create_server(self.registry, FakeMCP)
        self.assertTrue(server.tools["check_visualization_bundle"](result["path"], result["sha256"])["ok"])
        blocked = server.tools["export_visualization_bundle"](output, "bundle")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["type"], "PolicyError")


@unittest.skipUnless(os.environ.get("VEGAVISUALS_HANDOFF_REFERENCE"), "set VEGAVISUALS_HANDOFF_REFERENCE to the explicit central checkout")
class ReferenceTest(unittest.TestCase):
    """Execute reviewed Git blobs, never the mutable sibling working tree."""

    setUp = HandoffTest.setUp
    render = HandoffTest.render
    export = HandoffTest.export

    def test_pinned_reference_and_fixtures(self):
        checkout = os.environ["VEGAVISUALS_HANDOFF_REFERENCE"]
        self.assertTrue(pathlib.Path(checkout).is_absolute())
        reference = self.base / "reference"
        reference.mkdir()
        def blob(path):
            return subprocess.check_output(["git", "-C", checkout, "show", f"{REFERENCE_REVISION}:{path}"])
        for name in ("mcp-artifact-handoff.py", "artifact-handoff-v1.schema.json"):
            (reference / name).write_bytes(blob(REFERENCE_PREFIX + name))
        names = subprocess.check_output(["git", "-C", checkout, "ls-tree", "-r", "--name-only", REFERENCE_REVISION,
                                         "tests/fixtures/mcp-artifact-handoff"]).decode().splitlines()
        for name in names:
            write(reference / name, blob(name))
        import sys
        def verify(root, path, sha):
            completed = subprocess.run([sys.executable, str(reference / "mcp-artifact-handoff.py"), "bundle",
                                        "--workspace", str(root), "--path", path, "--sha256", sha, "--json"],
                                       capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for fixture, sha in (("figure", "d7b298c857f748fb113bd09826239966e32aa301d0df5a3f341f313b167ea784"),
                             ("deck", "ab602be252e02571f4f61b6fdce35c331049d5acdc7592dda5c5ad40c68aca00")):
            verify(reference, f"tests/fixtures/mcp-artifact-handoff/{fixture}/bundle.json", sha)
        for engine in ("vega-lite", "vega"):
            _, output = self.render(engine)
            result = self.export(output, f"bundles/{engine}")
            verify(self.root, result["path"], result["sha256"])
        retained = self.base / "retained"
        shutil.copytree(self.root / "bundles", retained / "archive")
        self.registry.close()
        shutil.rmtree(self.root)
        for path in (retained / "archive").glob("*/bundle.json"):
            verify(retained, path.relative_to(retained).as_posix(), digest(path.read_bytes()))
            data = path.parent / "payload/project/data/àrea.csv"
            data.unlink()
            completed = subprocess.run([sys.executable, str(reference / "mcp-artifact-handoff.py"), "bundle",
                                        "--workspace", str(retained), "--path", path.relative_to(retained).as_posix(),
                                        "--sha256", digest(path.read_bytes()), "--json"], capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
