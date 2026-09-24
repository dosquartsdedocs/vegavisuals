"""Opt-in, leaf-product MCP artifact handoff v1.

The central verifier is an independent test reference, never a runtime import.
Export observes a native managed render under its coordination lock. It does not
render, relocate native outputs, or grant ownership at an import destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import subprocess
import unicodedata
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ._version import __version__
from .errors import PolicyError, RenderError, ValidationError

if TYPE_CHECKING:
    from .registry import Registry

MAX_MANIFEST = 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 10000
STAGING = ".cache/vegavisuals/handoff"
ID = re.compile(r"[a-z][a-z0-9._-]{0,79}")
HASH = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"(?:git:(?:[0-9a-f]{40}|[0-9a-f]{64})|sha256:[0-9a-f]{64})")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(f"artifact handoff: {message}")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def parse(raw: bytes) -> dict[str, Any]:
    from .registry import _load_json_text

    require(len(raw) <= MAX_MANIFEST, "manifest/evidence exceeds 1 MiB")
    try:
        value = _load_json_text(raw.decode("utf-8"), "artifact handoff")
    except UnicodeError as exc:
        raise ValidationError("artifact handoff requires UTF-8 JSON") from exc
    require(isinstance(value, dict), "expected a JSON object")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        require(depth <= 32 and nodes <= 100000, "JSON depth/node limit exceeded")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def safe_path(value: str) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 1024, "invalid relative path")
    require(unicodedata.normalize("NFC", value) == value, "paths must be NFC")
    require(not any(unicodedata.category(c).startswith("C") or c in '\\:%$*?[]{}<>"|' for c in value),
            "reserved path character")
    parts = value.split("/")
    require(len(parts) <= 64, "path exceeds 64 components")
    for part in parts:
        require(part not in ("", ".", "..") and part == part.strip() and not part.endswith("."),
                "path must be normalized and relative")
        require(part.casefold() not in {".git", ".hg", ".svn"} and not part.startswith("~"), "reserved path")
        require(re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", part) is None,
                "reserved device name")
    return value


def paths_unique(paths: list[str]) -> set[str]:
    require(len(paths) == len(set(paths)), "duplicate path")
    aliases: dict[str, str] = {}
    parents: set[str] = set()
    files = set(paths)
    for path in paths:
        parts = safe_path(path).split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = prefix.casefold()
            require(key not in aliases or aliases[key] == prefix, "case-aliased path")
            aliases[key] = prefix
            if length < len(parts):
                require(prefix not in files, "file/directory collision")
                parents.add(prefix)
    require(len(parents) + len(paths) <= MAX_ENTRIES, "tree entry limit exceeded")
    return parents


def signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Reader:
    """Bounded no-follow reads, including workspace ancestors and hardlinks."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def __init__(self, root: pathlib.Path) -> None:
        require(root.is_absolute() and root != pathlib.Path("/")
                and not any(unicodedata.category(char).startswith("C") for char in str(root)),
                "workspace must be an explicit absolute directory below the filesystem root")
        self.fd = os.open("/", self.flags)
        self.snapshots: dict[str, tuple[int, ...]] = {}
        self.directories: dict[str, tuple[int, ...]] = {}
        self.total = 0
        try:
            for part in root.parts[1:]:
                child = os.open(part, self.flags, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = child
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        os.close(self.fd)

    @contextmanager
    def directory(self, path: str):
        fd = os.dup(self.fd)
        try:
            for part in safe_path(path).split("/") if path else []:
                child = os.open(part, self.flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def read(self, path: str, maximum: int = MAX_FILE) -> bytes:
        safe_path(path)
        require(path in self.snapshots or len(self.snapshots) < MAX_ENTRIES, "file count limit exceeded")
        parent, _, name = path.rpartition("/")
        with self.directory(parent) as fd:
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            try:
                before = os.fstat(child)
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "file is linked or special")
                require(before.st_size <= maximum, f"file too large: {path}")
                chunks = []
                size = 0
                while True:
                    chunk = os.read(child, min(1024 * 1024, maximum - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    self.total += len(chunk)
                    require(size <= maximum and self.total <= MAX_TOTAL, "byte limit exceeded")
                    chunks.append(chunk)
                after = os.fstat(child)
                linked = os.stat(name, dir_fd=fd, follow_symlinks=False)
                require(signature(before) == signature(after) == signature(linked) and size == after.st_size,
                        "file changed during read")
                require(path not in self.snapshots or self.snapshots[path] == signature(after), "file changed")
                self.snapshots[path] = signature(after)
                return b"".join(chunks)
            finally:
                os.close(child)

    def inventory(self, base: str) -> tuple[set[str], set[str]]:
        files: set[str] = set()
        directories: set[str] = set()

        def walk(fd: int, prefix: str) -> None:
            before = signature(os.fstat(fd))
            with os.scandir(fd) as entries:
                for entry in entries:
                    name = entry.name
                    path = safe_path(f"{prefix}/{name}" if prefix else name)
                    require(len(files) + len(directories) < MAX_ENTRIES, "tree entry limit exceeded")
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        directories.add(path)
                        child = os.open(name, self.flags, dir_fd=fd)
                        try:
                            require(signature(os.fstat(child)) == signature(info), "directory changed")
                            walk(child, path)
                        finally:
                            os.close(child)
                    else:
                        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "tree contains a link or special file")
                        files.add(path)
            require(signature(os.fstat(fd)) == before, "directory changed during inventory")
            self.directories[f"{base}/{prefix}" if prefix else base] = before

        with self.directory(base) as fd:
            walk(fd, "")
        return files, directories

    def recheck(self) -> None:
        for path, expected in self.snapshots.items():
            parent, _, name = path.rpartition("/")
            with self.directory(parent) as fd:
                require(signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) == expected,
                        f"file changed before publication/verification: {path}")
        for path, expected in self.directories.items():
            with self.directory(path) as fd:
                require(signature(os.fstat(fd)) == expected, "directory changed before verification")


def _shape(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    require(set(doc) == {"schema_version", "kind", "producer", "request", "files", "dependencies"},
            "unknown or missing bundle fields")
    require(type(doc["schema_version"]) is int and doc["schema_version"] == 1
            and doc["kind"] == "mcp-artifact-bundle", "unsupported bundle version/kind")
    actor = doc["producer"]
    require(isinstance(actor, dict) and set(actor) == {"name", "version", "revision", "runtimes"}, "invalid producer")
    require(actor["name"] == "vegavisuals", "this domain checker supports vegavisuals leaf bundles only")
    require(isinstance(actor["version"], str) and len(actor["version"]) <= 80
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", actor["version"]) is not None, "invalid producer version")
    require(isinstance(actor["revision"], str) and REVISION.fullmatch(actor["revision"]) is not None, "mutable producer revision")
    runtimes = actor["runtimes"]
    require(isinstance(runtimes, list) and len(runtimes) <= 32, "invalid runtimes")
    names = set()
    for runtime in runtimes:
        require(isinstance(runtime, dict) and set(runtime) == {"name", "revision"}, "invalid runtime")
        require(isinstance(runtime["name"], str) and ID.fullmatch(runtime["name"]) is not None
                and runtime["name"] not in names, "invalid/duplicate runtime name")
        names.add(runtime["name"])
        require(isinstance(runtime["revision"], str) and REVISION.fullmatch(runtime["revision"]) is not None,
                "mutable runtime revision")
    require(doc["dependencies"] == [], "composite bundles require a composite integrator; unsupported here")
    require(isinstance(doc["files"], list) and 2 <= len(doc["files"]) <= MAX_ENTRIES, "invalid files")
    files = {}
    for item in doc["files"]:
        fields = {"id", "path", "kind", "role", "ownership", "sha256", "bytes"}
        require(isinstance(item, dict) and fields <= item.keys() and item.keys() <= fields | {"variant_of"},
                "invalid file fields (upstream inputs unsupported by this leaf producer)")
        for key in ("id", "role"):
            require(isinstance(item[key], str) and ID.fullmatch(item[key]) is not None, f"invalid {key}")
        require(item["id"] not in files, "duplicate file id")
        require(isinstance(item["path"], str) and safe_path(item["path"]).startswith("payload/"), "invalid payload path")
        require(item["kind"] in ("input", "output", "evidence") and item["ownership"] in ("author", "producer"), "invalid file kind/owner")
        require(isinstance(item["sha256"], str) and HASH.fullmatch(item["sha256"]) is not None, "invalid hash")
        require(type(item["bytes"]) is int and 0 <= item["bytes"] <= MAX_FILE, "invalid byte count")
        files[item["id"]] = item
    require(isinstance(doc["request"], str) and doc["request"] in files, "missing request")
    require(files[doc["request"]]["kind"] == "input" and files[doc["request"]]["role"] == "request", "invalid request")
    require(any(item["kind"] == "output" for item in files.values()), "missing output")
    for item in files.values():
        if "variant_of" in item:
            require(isinstance(item["variant_of"], str), "invalid variant")
            original = files.get(item["variant_of"], {})
            require(item["ownership"] == "author" and original.get("kind") == item["kind"]
                    and original is not item and "variant_of" not in original, "invalid variant relationship")
    paths_unique(["bundle.json", *(item["path"] for item in files.values())])
    return files


def _domain(registry: Registry, doc: dict[str, Any], files: dict[str, dict[str, Any]],
            contents: dict[str, bytes]) -> None:
    from .registry import Registry, _json_bytes, _load_json_text

    def content(identity: str, kind: str, role: str) -> bytes:
        require(identity in files and files[identity]["kind"] == kind and files[identity]["role"] == role,
                f"missing domain input: {identity}")
        return contents[identity]

    request = parse(content(doc["request"], "input", "request"))
    require(request.get("version") == 1 and request.get("operation") == "export_visualization_bundle", "unknown request")
    require(request.get("project_root") == "payload/project", "unsupported retained project layout")
    require(request.get("engine") in ("vega", "vega-lite") and request.get("format") in ("svg", "png", "pdf"), "invalid render options")
    source = content("source", "input", "visualization-source")
    require(files["theme"]["path"] == f"payload/resources/themes/{safe_path(request['family'])}.json"
            and files["profile"]["path"] == f"payload/resources/compat/{safe_path(request['profile'])}.json",
            "selected theme/profile resource mismatch")
    parse(content("tokens", "input", "design-tokens"))
    source_path = safe_path(request["source"])
    require(files["source"]["path"] == f"payload/project/{source_path}", "source layout mismatch")
    require(type(request.get("inline")) is bool, "invalid inline mode")
    spec = _load_json_text(source.decode("utf-8"), "retained visualization")
    require(isinstance(spec, dict), "source must be an object")

    class Retained(Registry):
        def _read_project_bytes(self, relative, **kwargs):
            path = f"payload/project/{safe_path(relative)}"
            matches = [key for key, item in files.items() if item["path"] == path and item["kind"] == "input"]
            require(len(matches) == 1, f"unretained local reference: {relative}")
            raw = contents[matches[0]]
            require(len(raw) <= kwargs.get("max_bytes", MAX_FILE), "dependency exceeds render limit")
            return raw

    # Domain validation uses the retained project layout, not the old job root.
    retained = Retained(registry.project_root)
    try:
        profile = parse(content("profile", "input", "compatibility-profile"))
        theme = parse(content("theme", "input", "theme"))
        require(profile.get("id") == request["profile"] and isinstance(theme, dict), "profile mismatch")
        engine = retained._normalize_engine(request["engine"], source_name=source_path, spec=spec)
        require(retained._select_vega_lite_version(spec, engine, profile) == request["vega_lite_version"], "Vega-Lite version mismatch")
        retained._validate_semantic_basics(spec, engine)
        prepared, dependencies = retained._prepare_data_urls(
            spec, source=source_path, inline=request["inline"], max_dependency_bytes=MAX_FILE,
            max_prepared_spec_bytes=MAX_FILE, base_spec_bytes=len(source),
        )
        require(isinstance(request["inputs"], list) and all(isinstance(p, str) for p in request["inputs"]), "invalid inputs")
        require(not request["inline"] or not request["inputs"], "inline dependencies forbidden")
        dependencies = retained._add_explicit_inputs(dependencies, request["inputs"], max_dependency_bytes=MAX_FILE)
        require(sorted(item["path"] for item in dependencies) == request["inputs"], "incomplete dependency inventory")
        require(_json_bytes(prepared) == content("prepared", "evidence", "prepared-specification"), "prepared source differs from retained inputs")
    finally:
        retained.close()
    renderer = parse(content("renderer", "evidence", "renderer-provenance"))
    require(renderer == request["renderer"], "renderer request mismatch")
    require(doc["producer"]["runtimes"] == [{"name": "vl-convert", "revision": renderer["image_id"]}], "runtime identity mismatch")
    producer = content("producer", "evidence", "producer-inventory")
    require(doc["producer"]["revision"] == "sha256:" + digest(producer), "producer inventory mismatch")
    inventory = parse(producer)
    require(inventory.get("package") == "vegavisuals" and inventory.get("version") == doc["producer"]["version"], "producer inventory identity mismatch")
    indexed = {item["path"]: item for item in inventory["files"]}
    require({"registry.py", "handoff.py", "cli.py", "mcp_server.py"} <= indexed.keys(), "incomplete producer inventory")
    for identity, item in files.items():
        if item["path"].startswith("payload/resources/"):
            resource = indexed.get("assets/" + item["path"].removeprefix("payload/resources/"), {})
            require(resource.get("sha256") == item["sha256"] and resource.get("bytes") == item["bytes"], "resource differs from producer inventory")
    contract = hashlib.sha256()
    resources = [(files["profile"]["path"].removeprefix("payload/resources/"), contents["profile"]),
                 ("Dockerfile", content("dockerfile", "input", "renderer-resource")),
                 ("docker/worker.py", content("worker", "input", "renderer-resource"))]
    resources += sorted((item["path"].removeprefix("payload/resources/"), contents[key])
                        for key, item in files.items() if item["role"] == "theme")
    for path, raw in resources:
        contract.update(path.encode() + b"\0" + raw + b"\0")
    require(contract.hexdigest() == renderer["renderer_contract"], "retained renderer contract mismatch")
    for key, item in files.items():
        if item["kind"] == "output":
            require(item["path"].endswith("." + request["format"]), "output suffix mismatch")
            registry._validate_artifact_data(contents[key], request["format"], max_bytes=MAX_FILE)
    require("output" in files and files["output"]["kind"] == "output", "missing visualization output")
    if request["inline"]:
        metadata = parse(content("native-cache", "evidence", "native-cache-metadata"))
        require(type(metadata.get("version")) is int and metadata["version"] == 2,
                "invalid retained inline cache version")
        require(all(metadata.get(key) == request[key] for key in
                    ("engine", "format", "profile", "family", "vega_lite_version", "fingerprint", "renderer"))
                and metadata.get("output_sha256") == files["output"]["sha256"]
                and metadata.get("output") == f".cache/vegavisuals/text/{request['fingerprint']}.{request['format']}",
                "retained inline cache evidence differs from the managed render")
    if "manifest" in files:
        effective = parse(content("manifest", "input", "visualization-manifest"))
        require(effective == request["manifest"], "manifest projection mismatch")
        require(effective["profile"] == request["profile"] and effective["family"] == request["family"], "manifest options mismatch")
        selected = effective["visualizations"]
        require(len(selected) == 1 and selected[0]["source"] == request["source"]
                and selected[0]["inputs"] == request["inputs"] and selected[0]["engine"] == request["engine"]
                and selected[0]["format"] == request["format"], "manifest references mismatch")


def check(registry: Registry, bundle_path: str, sha256: str) -> dict[str, Any]:
    path = safe_path(bundle_path)
    require(path.endswith("/bundle.json"), "expected a relative path to bundle.json")
    require(isinstance(sha256, str) and HASH.fullmatch(sha256) is not None, "sender SHA-256 is required")
    reader = Reader(registry._startup_project_root)
    try:
        require(os.path.samestat(os.fstat(reader.fd), os.fstat(registry._project_root_fd)), "startup root changed")
        raw = reader.read(path, MAX_MANIFEST)
        require(digest(raw) == sha256, "manifest SHA-256 mismatch")
        doc = parse(raw)
        files = _shape(doc)
        base = path.rpartition("/")[0]
        expected = ["bundle.json", *(item["path"] for item in files.values())]
        parents = paths_unique(expected)
        actual_files, actual_dirs = reader.inventory(base)
        require(actual_files == set(expected) and actual_dirs == parents, "incomplete/undeclared bundle tree")
        contents = {}
        for identity, item in files.items():
            data = reader.read(f"{base}/{item['path']}")
            require(len(data) == item["bytes"] and digest(data) == item["sha256"], f"payload hash/size mismatch: {item['path']}")
            contents[identity] = data
        try:
            _domain(registry, doc, files, contents)
        except (KeyError, TypeError, AttributeError, IndexError, UnicodeError) as exc:
            raise ValidationError("artifact handoff: malformed domain request or resources") from exc
        reader.recheck()
        return {"ok": True, "schema_version": 1, "path": path, "sha256": sha256,
                "verified_files": len(files), "domain": "vegavisuals-leaf-v1"}
    finally:
        reader.close()


def _producer_inventory() -> bytes:
    root = pathlib.Path(__file__).parent
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".json", ".yml", ".md", ""}:
            raw = path.read_bytes()
            files.append({"path": path.relative_to(root).as_posix(), "sha256": digest(raw), "bytes": len(raw)})
    return encode({"algorithm": "sha256-of-exact-inventory-bytes", "package": "vegavisuals", "version": __version__, "files": files})


def _verify_project_root(registry: Registry) -> None:
    current = Reader(registry._startup_project_root)
    try:
        require(os.path.samestat(os.fstat(current.fd), os.fstat(registry._project_root_fd)), "startup root changed")
    finally:
        current.close()


def _git_marker_at_root(registry: Registry) -> bool:
    fd = os.dup(registry._project_root_fd)
    try:
        while True:
            try:
                os.stat(".git", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                return True
            parent = os.open("..", Reader.flags, dir_fd=fd)
            if os.path.samestat(os.fstat(parent), os.fstat(fd)):
                os.close(parent)
                return False
            os.close(fd)
            fd = parent
    finally:
        os.close(fd)


def _prepare_workspace(registry: Registry) -> None:
    """Explicit export enablement: ignore private staging, preserving custom rules."""
    _verify_project_root(registry)

    def git(*args):
        _verify_project_root(registry)
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}}
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            result = subprocess.run(
                ["git", *args], cwd=f"/proc/self/fd/{registry._project_root_fd}",
                pass_fds=(registry._project_root_fd,), check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("artifact handoff: Git ignore inspection timed out") from exc
        _verify_project_root(registry)
        return result

    git_available = shutil.which("git") is not None
    git_marker = _git_marker_at_root(registry)
    _verify_project_root(registry)
    if not git_available:
        require(not git_marker, "Git is required to prepare effective ignore coverage in a Git consumer")
    git_consumer = git_available and git("rev-parse", "--show-toplevel").returncode == 0
    require(not git_marker or git_consumer, "cannot safely inspect this Git consumer")
    if git_consumer:
        tracked = git("ls-files", "-z", "--", ".cache/vegavisuals")
        require(tracked.returncode == 0 and not tracked.stdout, "tracked cache/recovery files must be reviewed before export")
        if git("check-ignore", "-q", "--", ".cache/vegavisuals/").returncode != 0:
            snapshot = registry._project_file_snapshot(".gitignore", max_bytes=MAX_MANIFEST)
            raw = registry._read_project_bytes(".gitignore", max_bytes=MAX_MANIFEST, description="Git ignore rules") if snapshot.exists else b""
            raw += (b"\n" if raw and not raw.endswith(b"\n") else b"") + b"\n# vegavisuals private staging and publication recovery\n/.cache/vegavisuals/\n"
            with registry._publication_transaction() as publications:
                _verify_project_root(registry)
                registry._replace_project_bytes(".gitignore", raw, expected=snapshot, transaction_log=publications)
                _verify_project_root(registry)
        require(git("check-ignore", "-q", "--", f"{STAGING}/probe").returncode == 0, "staging is not effectively ignored")
    _verify_project_root(registry)
    with registry._open_project_parent(f"{STAGING}/probe", create=True) as (fd, _):
        os.fchmod(fd, 0o700)
    _verify_project_root(registry)


def _publish(registry, target: str, payload: dict[str, bytes], source_reader: Reader, recheck) -> dict[str, Any]:
    from .registry import RENAME_NOREPLACE, _renameat2

    _prepare_workspace(registry)
    stage = f"{STAGING}/{secrets.token_hex(16)}.pending"
    installed = None
    moved = False
    try:
        with registry._open_project_parent(stage, create=True) as (fd, name):
            os.mkdir(name, mode=0o700, dir_fd=fd)
        for path, raw in payload.items():
            with registry._open_project_parent(f"{stage}/{path}", create=True) as (fd, name):
                child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
                try:
                    view = memoryview(raw)
                    while view:
                        view = view[os.write(child, view):]
                    os.fsync(child)
                finally:
                    os.close(child)
                os.fsync(fd)
        for parent in sorted(paths_unique(list(payload)), key=lambda path: path.count("/"), reverse=True):
            with registry._open_project_parent(f"{stage}/{parent}/probe", create=False) as (fd, _):
                os.fsync(fd)
        with registry._open_project_parent(f"{stage}/probe", create=False) as (fd, _):
            os.fsync(fd)
        sha256 = digest(payload["bundle.json"])
        check(registry, f"{stage}/bundle.json", sha256)
        source_reader.recheck()
        recheck()
        with registry._open_project_parent(stage, create=False) as (stage_fd, stage_name):
            with registry._open_project_parent(target, create=True) as (target_fd, target_name):
                installed = os.stat(stage_name, dir_fd=stage_fd, follow_symlinks=False)
                require(installed.st_dev == os.fstat(target_fd).st_dev, "bundle and staging must share a filesystem")
                _renameat2(stage_fd, stage_name, target_fd, target_name, RENAME_NOREPLACE)
                moved = True
                os.fsync(stage_fd)
                os.fsync(target_fd)
        result = check(registry, f"{target}/bundle.json", sha256)
        source_reader.recheck()
        recheck()
        return result
    except BaseException as exc:
        # Never delete partial or displaced trees. Return them to private staging
        # only while the published directory still has the installed identity.
        recovery = stage
        if moved:
            recovery = target
            try:
                with registry._open_project_parent(target, create=False) as (fd, name):
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if installed is not None and (current.st_dev, current.st_ino) == (installed.st_dev, installed.st_ino):
                        with registry._open_project_parent(stage, create=False) as (stage_fd, stage_name):
                            _renameat2(fd, name, stage_fd, stage_name, RENAME_NOREPLACE)
                            displaced = os.stat(stage_name, dir_fd=stage_fd, follow_symlinks=False)
                            if (displaced.st_dev, displaced.st_ino) != (installed.st_dev, installed.st_ino):
                                # A non-cooperating writer exchanged the directory
                                # after our stat. Restore it without clobbering a
                                # newly appeared destination; otherwise retain it.
                                recovery = stage
                                _renameat2(stage_fd, stage_name, fd, name, RENAME_NOREPLACE)
                                recovery = target
                            else:
                                recovery = stage
                            os.fsync(fd)
                            os.fsync(stage_fd)
            except (OSError, ValidationError):
                pass
        raise RenderError(f"bundle publication failed; retained recovery tree at {recovery}: {exc}") from exc


def export(registry: Registry, output_path: str, bundle_path: str, *, visualization_text: str | None = None,
           manifest_path: str | None = None, edited_output_path: str | None = None,
           dry_run: bool = False) -> dict[str, Any]:
    from .registry import CACHE_VERSION, LOCK_NAME, MANIFEST_NAME, RECEIPT_PATH

    output = safe_path(output_path)
    target = safe_path(bundle_path)
    require(target.split("/")[0].casefold() not in {".cache", ".unaltraweb", LOCK_NAME, MANIFEST_NAME, ".gitignore"},
            "sealed bundles need a durable destination outside cache, receipts and native control paths")
    reader = Reader(registry._startup_project_root)
    try:
        require(os.path.samestat(os.fstat(reader.fd), os.fstat(registry._project_root_fd)), "startup root changed")
        with registry._project_lock():
            try:
                with reader.directory(target.rpartition("/")[0]) as fd:
                    os.stat(target.rpartition("/")[2], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PolicyError("bundle destination already exists; choose a new immutable bundle path")
            lock_bytes = None
            if registry._project_file_snapshot(LOCK_NAME).exists:
                lock_bytes = reader.read(LOCK_NAME, 4 * MAX_MANIFEST)
            lock = registry._load_lock()
            matches = [(name, item) for name, item in lock["visualizations"].items() if item["output"] == output]
            native: dict[str, tuple[str, bytes]] = {}
            if matches:
                name, entry = matches[0]
                require(lock_bytes is not None, "native lock disappeared")
                native["native-lock"] = ("native-lock", lock_bytes)
            else:
                require(re.fullmatch(r"\.cache/vegavisuals/text/[0-9a-f]{64}\.(svg|png|pdf)", output) is not None,
                        "output is unmanaged; native locks are not relocation/integration records")
                metadata_path = output.rsplit(".", 1)[0] + ".json"
                raw = reader.read(metadata_path, 65536)
                entry = parse(raw)
                require(type(entry.get("version")) is int and entry["version"] == CACHE_VERSION and entry.get("output") == output,
                        "invalid inline cache metadata")
                require(visualization_text is not None, "inline export requires the original specification text")
                entry = {**entry, "source": "inline:" + digest(visualization_text.encode("utf-8")), "inputs": []}
                name = "inline"
                native["native-cache"] = ("native-cache-metadata", raw)
            inline = entry["source"].startswith("inline:")
            options = {key: entry[key] for key in ("engine", "profile", "family")}
            if inline:
                require(visualization_text is not None, "inline export requires the original specification text")
                validation = registry._inline_validation(visualization_text, **options)
                require(validation["source"] == entry["source"], "inline source identity mismatch")
                source_bytes = visualization_text.encode("utf-8")
                source = "inline.vl.json" if entry["engine"] == "vega-lite" else "inline.vg.json"
            else:
                require(visualization_text is None, "text must only be supplied for an inline render")
                source = safe_path(entry["source"])
                source_bytes = reader.read(source)
                validation = registry._validate_file(source, inputs=entry["inputs"], **options)
            fingerprint = registry._fingerprint(validation, output_format=entry["format"])
            require(fingerprint == entry["fingerprint"], "output is stale; render with current sources/options before export")
            require(digest(source_bytes) == validation["source_sha256"], "source changed during export")
            renderer = entry["renderer"]
            require(isinstance(renderer, dict) and isinstance(renderer.get("image_id"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", renderer["image_id"]) is not None, "invalid native renderer provenance")
            require(registry._renderer_contract(validation["_profile_path"]) == renderer["renderer_contract"], "stale renderer contract")
            if inline:
                cache_path = f".cache/vegavisuals/text/{fingerprint}.{entry['format']}"
                metadata_path = f".cache/vegavisuals/text/{fingerprint}.json"
                cache_bytes = reader.read(cache_path)
                metadata_bytes = reader.read(metadata_path, 65536)
                metadata = parse(metadata_bytes)
                require(digest(cache_bytes) == entry["output_sha256"]
                        and all(metadata.get(key) == entry[key] for key in
                                ("engine", "format", "profile", "family", "vega_lite_version", "fingerprint", "renderer", "output_sha256"))
                        and registry._cache_is_valid(cache_path, metadata_path, fingerprint=fingerprint,
                                                     output_format=entry["format"], max_bytes=MAX_FILE, renderer=renderer)
                        and (bool(matches) or output == cache_path),
                        "invalid inline cache contract")
                native["native-cache"] = ("native-cache-metadata", metadata_bytes)
            artifact = reader.read(output)
            require(digest(artifact) == entry["output_sha256"], "managed output was modified")
            registry._validate_artifact_data(artifact, entry["format"], max_bytes=MAX_FILE)
            payload: dict[str, bytes] = {}
            files = []

            def add(identity, path, raw, kind, role, owner="producer", variant=None):
                safe_path(path)
                require(path not in payload, f"payload collision: {path}")
                require(len(raw) <= MAX_FILE, "payload exceeds file limit")
                payload[path] = raw
                item = {"id": identity, "path": path, "kind": kind, "role": role, "ownership": owner,
                        "sha256": digest(raw), "bytes": len(raw)}
                if variant:
                    item["variant_of"] = variant
                files.append(item)

            add("source", f"payload/project/{source}", source_bytes, "input", "visualization-source", "author")
            dependencies = validation["dependencies"]
            for index, item in enumerate(dependencies):
                raw = reader.read(safe_path(item["path"]))
                require(digest(raw) == item["sha256"] and len(raw) == item["bytes"], "dependency changed during export")
                if item["path"] != source:
                    add(f"input-{index}", f"payload/project/{item['path']}", raw, "input", "local-data" if item["kind"] == "data" else "declared-input", "author")
            request = {"version": 1, "operation": "export_visualization_bundle", "source": source, "inline": inline,
                       "project_root": "payload/project",
                       "output": output, **options, "format": entry["format"], "vega_lite_version": validation["vega_lite_version"],
                       "inputs": sorted(item["path"] for item in dependencies), "fingerprint": fingerprint,
                       "renderer": renderer, "manifest": None, "edited_output": edited_output_path}
            selected_manifest = manifest_path
            if selected_manifest is None and not inline and registry._project_file_snapshot(MANIFEST_NAME).exists:
                selected_manifest = MANIFEST_NAME
            if selected_manifest:
                require(not inline, "inline exports do not use a file manifest")
                raw = reader.read(safe_path(selected_manifest), MAX_MANIFEST)
                manifest = registry._load_manifest(selected_manifest)
                require(digest(raw) == manifest["sha256"], "manifest changed")
                selected = [item for item in manifest["visualizations"] if item["output"] == output]
                require(bool(selected) or manifest_path is None, "output is not in the selected manifest")
                if selected:
                    item = selected[0]
                    effective_validation = registry._validate_file(item["source"], engine=item["engine"], profile=manifest["profile"], family=manifest["family"], inputs=item["inputs"])
                    require(registry._fingerprint(effective_validation, output_format=item["format"]) == fingerprint, "manifest options differ from native render")
                    projection = {"version": 1, "profile": entry["profile"], "family": entry["family"],
                                  "visualizations": [{"name": item["name"], "source": source, "output": output,
                                                      "engine": entry["engine"], "format": entry["format"], "inputs": request["inputs"]}]}
                    request["manifest"] = projection
                    request["manifest_origin"] = {"path": selected_manifest, "selection": item["name"], "original": "manifest-original", "projection": "manifest"}
                    add("manifest-original", "payload/evidence/manifest.yml", raw, "evidence", "source-manifest", "author")
                    add("manifest", "payload/effective-manifest.json", encode(projection), "input", "visualization-manifest")
            if not name.startswith("direct-") and not inline:
                require(request["manifest"] is not None, "named render requires its original manifest selection; pass manifest_path")
            add("prepared", "payload/evidence/prepared.json", validation["_prepared_spec_bytes"], "evidence", "prepared-specification")
            add("output", f"payload/output/visualization.{entry['format']}", artifact, "output", "visualization")
            if edited_output_path:
                edit = reader.read(safe_path(edited_output_path))
                require(edited_output_path != output and edited_output_path.endswith("." + entry["format"]), "variant must be a distinct same-format output")
                registry._validate_artifact_data(edit, entry["format"], max_bytes=MAX_FILE)
                add("edited-output", f"payload/output/visualization.edited.{entry['format']}", edit, "output", "visualization", "author", "output")
            for identity, (role, raw) in native.items():
                add(identity, f"payload/evidence/{identity}.json", raw, "evidence", role)
            if registry._project_file_snapshot(RECEIPT_PATH).exists:
                add("native-receipt", "payload/evidence/native-receipt.json", reader.read(RECEIPT_PATH, 256 * 1024), "evidence", "native-receipt")
            resources = [("profile", validation["_profile_path"], "compatibility-profile"),
                         ("tokens", registry.assets / "tokens" / f"{entry['family']}.json", "design-tokens"),
                         ("dockerfile", registry.assets / "Dockerfile", "renderer-resource"),
                         ("worker", registry.assets / "docker/worker.py", "renderer-resource")]
            resources += [("theme" if path == validation["_theme_path"] else f"theme-{index}", path, "theme")
                          for index, path in enumerate(sorted((registry.assets / "themes").glob("*.json")))]
            for identity, path, role in resources:
                add(identity, f"payload/resources/{path.relative_to(registry.assets).as_posix()}", path.read_bytes(), "input", role)
            inventory = _producer_inventory()
            add("producer", "payload/evidence/producer.json", inventory, "evidence", "producer-inventory")
            add("renderer", "payload/evidence/renderer.json", encode(renderer), "evidence", "renderer-provenance")
            add("request", "payload/request.json", encode(request), "input", "request")
            doc = {"schema_version": 1, "kind": "mcp-artifact-bundle", "producer": {
                "name": "vegavisuals", "version": __version__, "revision": "sha256:" + digest(inventory),
                "runtimes": [{"name": "vl-convert", "revision": renderer["image_id"]}]},
                "request": "request", "files": files, "dependencies": []}
            payload["bundle.json"] = encode(doc)
            require(len(payload["bundle.json"]) <= MAX_MANIFEST and sum(map(len, payload.values())) <= MAX_TOTAL, "bundle byte limit exceeded")
            _shape(doc)
            # Account for destination prefix as well as bundle-relative paths.
            paths_unique([f"{target}/{path}" for path in payload] + list(reader.snapshots))
            _domain(registry, doc, {item["id"]: item for item in files}, {item["id"]: payload[item["path"]] for item in files})

            def recheck():
                _verify_project_root(registry)
                require(_producer_inventory() == inventory, "producer/package resources changed during export")
                require(registry._fingerprint(validation, output_format=entry["format"]) == fingerprint, "render contract changed during export")

            reader.recheck()
            recheck()
            if dry_run:
                return {"ok": True, "dry_run": True, "path": f"{target}/bundle.json", "sha256": digest(payload["bundle.json"]), "files": len(files)}
            return _publish(registry, target, payload, reader, recheck)
    finally:
        reader.close()
