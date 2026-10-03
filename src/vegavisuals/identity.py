"""Bounded, read-only observations of one already-created Registry instance.

The loaded version is a Python constant, not a re-import or metadata lookup.
File hashes describe observed disk bytes, not an attestation of Python memory.
"""
from __future__ import annotations

import ast
import copy
import datetime
import email.parser
import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import sysconfig
import uuid
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from ._version import __version__
from .errors import VegavisualsError

if TYPE_CHECKING:
    from .registry import Registry

SCHEMA_VERSION = 1
LOADED_VERSION = __version__
PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent
MAX_FILE_BYTES = 2 * 1024 * 1024
PACKAGE_FILES = (
    "__init__.py", "__main__.py", "_version.py", "cli.py", "errors.py", "handoff.py", "identity.py",
    "mcp_server.py", "registry.py", "factory/mcp-factory.yml", "assets/AGENTS.md",
    "assets/Dockerfile", "assets/docker/worker.py", "assets/compat/vl-convert-1.9.0.json",
    "assets/themes/benizar.json", "assets/tokens/benizar.json",
)
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:a[0-9]+|b[0-9]+|rc[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9]+(?:[.-][a-z0-9]+)*)?")
SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = r"(?:(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])(?::[0-9]+)?/)?[a-z0-9]+(?:(?:[._-]+[a-z0-9]+)|(?:/[a-z0-9]+))*"
REPO_DIGEST = re.compile(REPOSITORY + r"@sha256:[0-9a-f]{64}")
IMAGE_REFERENCE = re.compile(REPOSITORY + r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?(?:@sha256:[0-9a-f]{64})?")


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path: pathlib.Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("identity observation requires a bounded regular file")
        data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("identity observation exceeds its bound")
        return data


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version_file(data: bytes) -> str:
    # Never execute or reload a possibly replaced version module.
    tree = ast.parse(data.decode("utf-8"))
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        raise ValueError("invalid version data")
    assignment = tree.body[0]
    if (len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name)
            or assignment.targets[0].id != "__version__" or not isinstance(assignment.value, ast.Constant)):
        raise ValueError("invalid version data")
    value = assignment.value.value
    if not isinstance(value, str) or len(value) > 128 or not VERSION.fullmatch(value):
        raise ValueError("invalid version data")
    return value


def _distribution_root(roots: tuple[str, ...]) -> pathlib.Path | None:
    # Do not search the consumer cwd or arbitrary entries later added to sys.path.
    distribution = next(iter(importlib.metadata.Distribution.discover(name="vegavisuals", path=roots)), None)
    if distribution is None:
        return None
    # Installed wheels/editables use PathDistribution on the supported Pythons.
    # Pin its metadata directory, then perform our own bounded no-follow reads.
    path = getattr(distribution, "_path", None)
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError("non-filesystem distribution metadata is unsupported")
    return pathlib.Path(path).absolute()


def _distribution(roots: tuple[str, ...]) -> dict[str, Any] | None:
    root = _distribution_root(roots)
    if root is None:
        return None
    raw = _read(root / "METADATA")
    metadata = email.parser.BytesParser().parsebytes(raw, headersonly=True)
    version = metadata.get("Version", "")
    if (metadata.get("Name", "").lower().replace("_", "-") != "vegavisuals"
            or len(version) > 128 or not VERSION.fullmatch(version)):
        raise ValueError("invalid distribution metadata")
    result: dict[str, Any] = {
        "metadata_root": str(root), "version": version, "metadata_sha256": _digest(raw),
        "record_sha256": None, "direct_url_sha256": None, "archive_sha256": None,
        "vcs_commit_id": None, "editable": False,
    }
    try:
        result["record_sha256"] = _digest(_read(root / "RECORD"))
    except FileNotFoundError:
        pass
    try:
        raw_origin = _read(root / "direct_url.json")
    except FileNotFoundError:
        return result
    origin = json.loads(raw_origin)
    result["direct_url_sha256"] = _digest(raw_origin)
    # Never return the URL, requested ref, credentials or arbitrary metadata.
    archive = origin.get("archive_info", {})
    sha = archive.get("hashes", {}).get("sha256")
    if sha is None and str(archive.get("hash", "")).startswith("sha256="):
        sha = archive["hash"][7:]
    if sha is not None:
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ValueError("invalid archive identity")
        result["archive_sha256"] = sha
    commit = origin.get("vcs_info", {}).get("commit_id")
    if commit is not None:
        if not isinstance(commit, str) or not REVISION.fullmatch(commit):
            raise ValueError("invalid source identity")
        result["vcs_commit_id"] = commit
    result["editable"] = origin.get("dir_info", {}).get("editable") is True
    return result


def _source_revision(root: pathlib.Path) -> str | None:
    # Observe HEAD/refs as data. No Git invocation, hooks, index refresh, remote
    # access or inherited GIT_* redirection, including during Registry startup.
    try:
        git = root / ".git"
        if not git.is_dir():
            marker = _read(git).decode("utf-8").strip()
            if not marker.startswith("gitdir: ") or "\n" in marker:
                return None
            git = (root / marker[8:]).resolve()
        common = git
        try:
            common = (git / _read(git / "commondir").decode("utf-8").strip()).resolve()
        except FileNotFoundError:
            pass
        value = _read(git / "HEAD").decode("ascii").strip()
        for _ in range(5):
            if REVISION.fullmatch(value):
                return value
            if not value.startswith("ref: "):
                return None
            ref = value[5:]
            path = pathlib.PurePosixPath(ref)
            if not ref.startswith("refs/") or any(part in {"", ".", ".."} for part in path.parts) or "\\" in ref:
                return None
            try:
                value = _read(common / ref).decode("ascii").strip()
            except FileNotFoundError:
                for line in _read(common / "packed-refs").decode("ascii").splitlines():
                    fields = line.split(" ", 1)
                    if len(fields) == 2 and fields[1] == ref and REVISION.fullmatch(fields[0]):
                        return fields[0]
                return None
    except (OSError, ValueError, UnicodeError):
        return None
    return None


def _disk_snapshot(package_root: pathlib.Path, source_root: pathlib.Path | None, distribution_roots: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {"version_file": None, "package_files_sha256": None,
                              "distribution": None, "source_revision": None, "errors": []}
    try:
        values = {name: _read(package_root / name) for name in PACKAGE_FILES}
        result["version_file"] = _version_file(values["_version.py"])
        result["package_files_sha256"] = _digest(b"".join(
            len(name.encode()).to_bytes(8, "big") + name.encode()
            + len(values[name]).to_bytes(8, "big") + values[name] for name in PACKAGE_FILES))
    except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError):
        result["errors"].append("package-files-unavailable")
    try:
        result["distribution"] = _distribution(distribution_roots)
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        result["errors"].append("distribution-metadata-unavailable")
    if source_root is not None:
        result["source_revision"] = _source_revision(source_root)
        if result["source_revision"] is None:
            result["errors"].append("source-revision-unavailable")
    return result


@dataclass(frozen=True)
class StartupIdentity:
    instance_id: str
    started_at: str
    pid: int
    package_root: pathlib.Path
    source_root: pathlib.Path | None
    distribution_roots: tuple[str, ...]
    mode: str
    interpreter: dict[str, Any]
    disk: dict[str, Any]

    @classmethod
    def capture(cls, source_root: pathlib.Path | None) -> StartupIdentity:
        instance_id, started_at, pid = uuid.uuid4().hex, _utc_now(), os.getpid()
        roots = tuple(dict.fromkeys((str(PACKAGE_ROOT.parent), sysconfig.get_path("purelib"), sysconfig.get_path("platlib"))))
        disk = _disk_snapshot(PACKAGE_ROOT, source_root, roots)
        distribution = disk["distribution"]
        if source_root is not None or (distribution and distribution["editable"]):
            mode = "development"
        elif distribution and pathlib.Path(distribution["metadata_root"]).parent == PACKAGE_ROOT.parent:
            mode = "installed"
        else:
            mode = "unmanaged"
        interpreter = {"executable": sys.executable, "resolved_executable": str(pathlib.Path(sys.executable).resolve()),
                       "implementation": sys.implementation.name, "version": ".".join(map(str, sys.version_info[:3]))}
        return cls(instance_id, started_at, pid, PACKAGE_ROOT, source_root, roots, mode, interpreter, disk)


def _renderer(registry: Registry, profile: str, mismatches: list[dict[str, str]]) -> dict[str, Any]:
    from .registry import RENDERER_CONTRACT_LABEL, _load_json_text

    result: dict[str, Any] = {
        "profile": profile if isinstance(profile, str) and len(profile) <= 128 else None,
        "profile_scope": "query", "profile_sha256": None,
        "selection_mode": "explicit-image-id" if registry.renderer_image_id is not None else "profile",
        "requested_image": registry.renderer_image_id, "expected_image_id": registry.renderer_image_id,
        "expected_contract_sha256": None, "observed_image_id": None, "observed_contract_sha256": None,
        "repo_digests": None, "inspection_returncode": None, "available": False,
    }
    def problem(code: str) -> None:
        mismatches.append({"component": "renderer", "code": code})
    if result["profile"] is None:
        problem("renderer-profile-unavailable")
        return result
    try:
        name, data, path = registry._load_profile(profile)
        result["profile"] = name
        result["profile_sha256"] = _digest(_read(path))
        result["expected_contract_sha256"] = registry._renderer_contract(path)
        selected = registry.renderer_image_id or data["image"]
        if len(selected) > 512 or (not IMAGE_ID.fullmatch(selected) and not IMAGE_REFERENCE.fullmatch(selected)):
            raise ValueError("invalid image reference")
        result["requested_image"] = selected
    except (OSError, ValueError, TypeError, VegavisualsError):
        problem("renderer-profile-unavailable")
        return result
    template = ('{"image_id":{{json .Id}},"contract":{{json (index .Config.Labels "'
                + RENDERER_CONTRACT_LABEL + '")}},"repo_digests":{{json .RepoDigests}}}')
    try:
        inspected = registry._runner(["docker", "image", "inspect", "--format", template, selected],
                                     cwd=registry.project_root, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        problem("renderer-inspection-failed")
        return result
    code = inspected.get("returncode")
    result["inspection_returncode"] = code if type(code) is int else None
    if type(code) is not int or code != 0:
        # Partial stdout is not a successful observation. Never disclose stderr.
        problem("renderer-inspection-failed")
        return result
    try:
        value = _load_json_text(str(inspected.get("stdout", "")), "renderer identity")
        if (not isinstance(value, dict) or set(value) != {"image_id", "contract", "repo_digests"}
                or not isinstance(value["image_id"], str) or not IMAGE_ID.fullmatch(value["image_id"])):
            raise ValueError("invalid image identity")
        result["observed_image_id"] = value["image_id"]
        label = value["contract"]
        result["observed_contract_sha256"] = label if isinstance(label, str) and SHA256.fullmatch(label) else None
        digests = value["repo_digests"] if value["repo_digests"] is not None else []
        if (not isinstance(digests, list) or len(digests) > 64 or any(
                not isinstance(item, str) or len(item) > 512 or not REPO_DIGEST.fullmatch(item) for item in digests)):
            raise ValueError("invalid recorded repository digests")
        result["repo_digests"] = sorted(set(digests))
    except (ValueError, TypeError, VegavisualsError, RecursionError):
        problem("renderer-inspection-invalid")
        return result
    identity_matches = registry.renderer_image_id is None or result["observed_image_id"] == registry.renderer_image_id
    contract_matches = result["observed_contract_sha256"] == result["expected_contract_sha256"]
    if not identity_matches:
        problem("renderer-image-id-mismatch")
    if not contract_matches:
        problem("renderer-contract-mismatch")
    result["available"] = identity_matches and contract_matches
    return result


def report(registry: Registry, startup: StartupIdentity, profile: str) -> dict[str, Any]:
    current = _disk_snapshot(startup.package_root, startup.source_root, startup.distribution_roots)
    mismatches: list[dict[str, str]] = []
    for scope, snapshot in (("startup", startup.disk), ("current_disk", current)):
        mismatches.extend({"component": f"package.{scope}", "code": code} for code in snapshot["errors"])
    if startup.mode == "unmanaged":
        mismatches.append({"component": "package", "code": "unmanaged-installation"})
    if current["version_file"] != LOADED_VERSION:
        mismatches.append({"component": "package", "code": "version-file-differs-from-loaded"})
    distribution = current["distribution"]
    if distribution is not None and distribution["version"] != LOADED_VERSION:
        mismatches.append({"component": "package", "code": "distribution-version-differs-from-loaded"})
    for field, code in (("package_files_sha256", "package-files-changed"), ("distribution", "distribution-metadata-changed"),
                        ("source_revision", "source-revision-changed")):
        if startup.disk[field] != current[field]:
            mismatches.append({"component": "package", "code": code})
    pid = os.getpid()
    if pid != startup.pid:
        mismatches.append({"component": "instance", "code": "process-changed"})
    try:
        bound = os.fstat(registry._project_root_fd)
        matches = all(os.path.samestat(bound, os.stat(path)) for path in (registry.project_root, registry._startup_project_root))
    except (OSError, ValueError):
        matches = False
    if not matches:
        mismatches.append({"component": "workspace", "code": "workspace-binding-changed"})
    renderer = _renderer(registry, profile, mismatches)
    return {
        "schema_version": SCHEMA_VERSION, "kind": "vegavisuals-runtime-identity", "ok": not mismatches,
        "observed_at": _utc_now(),
        "instance": {"id": startup.instance_id, "started_at": startup.started_at, "pid": pid, "startup_pid": startup.pid},
        "interpreter": copy.deepcopy(startup.interpreter),
        "workspace": {"root": str(registry.project_root), "binding_matches_startup": matches},
        "package": {"name": "vegavisuals", "loaded_version": LOADED_VERSION, "mode": startup.mode,
                    "root": str(startup.package_root), "source_root": str(startup.source_root) if startup.source_root else None,
                    "startup": copy.deepcopy(startup.disk), "current_disk": current},
        "renderer": renderer, "mismatches": mismatches,
    }
