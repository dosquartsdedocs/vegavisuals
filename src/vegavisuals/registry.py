from __future__ import annotations

import base64
import copy
import csv
import ctypes
import errno
import fcntl
import hashlib
import importlib.resources
import io
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import yaml

from ._version import __version__
from .errors import ManifestError, PolicyError, RenderError, ValidationError


DEFAULT_PROFILE = "vl-convert-1.9.0"
DEFAULT_FAMILY = "benizar"
DEFAULT_RELEASE = f"v{__version__}"
REPOSITORY_URL = "https://github.com/dosquartsdedocs/vegavisuals"
MCP_VERSION = "1.29.0"
MANIFEST_NAME = ".vegavisuals.yml"
LOCK_NAME = ".vegavisuals.lock.json"
RECEIPT_PATH = ".unaltraweb/receipts/vegavisuals.json"
LOCK_VERSION = 2
CACHE_VERSION = 2
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_RECEIPT_FILES = 500
MAX_RECEIPT_FILE_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_TOTAL_BYTES = 256 * 1024 * 1024
RECEIPT_JSON_MAX_DEPTH = 64
RECEIPT_JSON_MAX_NODES = 100_000
PROJECT_LOCK_PATH = ".cache/vegavisuals/project.lock"
CONTAINER_LABEL = "io.context.mcp-factory=vegavisuals"
CONTAINER_WORKSPACE_LABEL = "io.context.mcp-factory.workspace"
RENDERER_CONTRACT_LABEL = "io.vegavisuals.renderer-contract"
MAX_JSON_DEPTH = 128
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
OUTPUT_MIME_TYPES = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
}
ENGINE_ALIASES = {
    "vega-lite": "vega-lite",
    "vegalite": "vega-lite",
    "vl": "vega-lite",
    "vega": "vega",
    "vg": "vega",
}
SAFE_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SAFE_VISUALIZATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VEGA_SOURCE_SUFFIXES = (".vl.json", ".vg.json")
VEGA_LITE_SCHEMA_RE = re.compile(
    r"^https://vega\.github\.io/schema/vega-lite/v(?P<version>[0-9]+(?:\.[0-9]+)?)\.json$",
    re.IGNORECASE,
)
VEGA_SCHEMA_RE = re.compile(
    r"^https://vega\.github\.io/schema/vega/v(?P<version>[0-9]+(?:\.[0-9]+)?)\.json$",
    re.IGNORECASE,
)
VEGA_LITE_MARKS = {
    "arc",
    "area",
    "bar",
    "boxplot",
    "circle",
    "errorband",
    "errorbar",
    "geoshape",
    "image",
    "line",
    "point",
    "rect",
    "rule",
    "square",
    "text",
    "tick",
    "trail",
}
VEGA_MARKS = {"arc", "area", "group", "image", "line", "path", "rect", "rule", "shape", "symbol", "text", "trail"}
MCP_TOOL_NAMES = (
    "initialize_project",
    "validate_visualization",
    "render_visualization",
    "render_visualization_text",
    "visualization_status",
    "visualization_check",
    "render_visualizations",
    "theme_inventory",
    "compatibility_status",
    "factory_check",
    "release_status",
    "update",
    "factory_manifest",
)
MCP_RESOURCE_URIS = (
    "vegavisuals://agent-guide",
    "vegavisuals://themes",
    "vegavisuals://compatibility",
    "vegavisuals://project/status",
    "vegavisuals://project/check",
    "vegavisuals://factory/check",
    "vegavisuals://release",
    "vegavisuals://factory-manifest",
)

CommandResult = dict[str, Any]
Runner = Callable[..., CommandResult]


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    sha256: str | None = None
    size: int = 0
    device: int | None = None
    inode: int | None = None


def _renameat2(source_fd: int, source: str, target_fd: int, target: str, flags: int) -> None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is required for safe publication") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ManifestError("visualization manifest mapping keys must be scalar") from exc
        if duplicate:
            raise ManifestError(f"duplicate YAML key in visualization manifest: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON number: {value}")


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValidationError(f"non-finite JSON number: {value}")
    return parsed


def _check_json_depth(value: Any, description: str) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValidationError(f"JSON nesting exceeds {MAX_JSON_DEPTH} levels in {description}")
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _load_json_text(text: str, description: str) -> Any:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except ValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise ValidationError(f"invalid JSON in {description}: {exc}") from exc
    _check_json_depth(value, description)
    return value


def _load_json_file(path: pathlib.Path, description: str | None = None) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read {description or path}: {exc}") from exc
    return _load_json_text(text, description or str(path))


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"value cannot be serialized as strict JSON: {exc}") from exc


def json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_command(
    command: list[str],
    *,
    cwd: pathlib.Path,
    timeout: int,
) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return {
            "command": command,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return {
            "command": command,
            "returncode": 124,
            "stdout": stdout[-65536:],
            "stderr": stderr[-65536:] or f"command timed out after {timeout} seconds",
        }
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-65536:],
        "stderr": completed.stderr[-65536:],
    }


def asset_root() -> pathlib.Path:
    resource = importlib.resources.files("vegavisuals").joinpath("assets")
    root = pathlib.Path(str(resource)).resolve()
    if not root.is_dir():
        raise ValidationError("installed vegavisuals package does not contain its assets")
    return root


def source_checkout() -> pathlib.Path | None:
    candidate = pathlib.Path(__file__).resolve().parents[2]
    if (candidate / ".git").exists() and (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def factory_metadata_root() -> pathlib.Path:
    checkout = source_checkout()
    if checkout is not None:
        return checkout
    packaged = pathlib.Path(__file__).resolve().parent / "factory"
    return packaged if packaged.is_dir() else asset_root()


def _docker_config_root() -> pathlib.Path:
    configured = os.environ.get("DOCKER_CONFIG", "").strip()
    root = pathlib.Path(configured).expanduser() if configured else pathlib.Path.home() / ".docker"
    return pathlib.Path(os.path.abspath(root))


def _docker_endpoint_scope() -> dict[str, str]:
    context = os.environ.get("DOCKER_CONTEXT", "").strip()
    if context:
        if context == "default":
            return {"kind": "host", "value": "unix:///var/run/docker.sock"}
        return {"kind": "context", "value": context, "config": str(_docker_config_root())}

    host = os.environ.get("DOCKER_HOST", "").strip()
    if host:
        return {"kind": "host", "value": host}

    config_root = _docker_config_root()
    try:
        with (config_root / "config.json").open("rb") as config_handle:
            raw = config_handle.read(1024 * 1024 + 1)
        config = json.loads(raw) if len(raw) <= 1024 * 1024 else {}
        current = str(config.get("currentContext") or "").strip() if isinstance(config, dict) else ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        current = ""
    if current and current != "default":
        return {"kind": "context", "value": current, "config": str(config_root)}
    return {"kind": "host", "value": "unix:///var/run/docker.sock"}


def _renderer_lock_name(image: str) -> str:
    scope = json.dumps(_docker_endpoint_scope(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(scope.encode("utf-8") + b"\0" + image.encode("utf-8")).hexdigest()
    return f"{digest}.lock"


def _open_renderer_lock_directory() -> int:
    uid = os.geteuid()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    candidates = (
        (pathlib.Path(f"/run/user/{uid}"), ".unaltra-renderer-locks", True),
        (pathlib.Path("/tmp"), f".unaltra-renderer-locks-{uid}", False),
    )
    failures: list[str] = []
    for parent, name, private_parent in candidates:
        parent_descriptor = -1
        directory_descriptor = -1
        try:
            parent_descriptor = os.open(parent, directory_flags)
            parent_status = os.fstat(parent_descriptor)
            parent_mode = stat.S_IMODE(parent_status.st_mode)
            if not stat.S_ISDIR(parent_status.st_mode):
                raise OSError("lock parent is not a directory")
            if private_parent:
                if parent_status.st_uid != uid or parent_mode & 0o077:
                    raise OSError("runtime lock parent is not private to the current user")
            elif parent_status.st_uid not in {0, uid} or not parent_status.st_mode & stat.S_ISVTX:
                raise OSError("temporary lock parent is not sticky and trusted")

            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            directory_descriptor = os.open(name, directory_flags, dir_fd=parent_descriptor)
            directory_status = os.fstat(directory_descriptor)
            directory_mode = stat.S_IMODE(directory_status.st_mode)
            if (
                not stat.S_ISDIR(directory_status.st_mode)
                or directory_status.st_uid != uid
                or directory_mode & 0o077
            ):
                raise OSError("renderer lock directory is not private to the current user")
            return directory_descriptor
        except OSError as exc:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
            failures.append(f"{parent}: {exc}")
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
    raise RenderError(f"cannot open shared renderer lock directory: {'; '.join(failures)}")


def _docker_mount_spec(*fields: str) -> str:
    encoded = io.StringIO()
    csv.writer(encoded, lineterminator="").writerow(fields)
    return encoded.getvalue()


def _entrypoint_python(command_path: str) -> str | None:
    try:
        first_line = pathlib.Path(command_path).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (IndexError, OSError, UnicodeDecodeError):
        return None
    if not first_line.startswith("#!"):
        return None
    executable = first_line[2:].strip().split()[0]
    return executable if pathlib.Path(executable).is_file() else None


def _relative(path: pathlib.Path, root: pathlib.Path) -> str:
    return path.relative_to(root).as_posix()


def _path_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path == root or root in path.parents


def workspace_id(project_root: pathlib.Path) -> str:
    return hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:12]


class _ProjectPublication:
    """One descriptor-relative replacement that can be committed or rolled back."""

    def __init__(
        self,
        parent_fd: int,
        target: str,
        backup: str | None,
        installed: _FileSnapshot,
        backup_snapshot: _FileSnapshot | None,
        snapshot_reader: Callable[[int, str], _FileSnapshot],
        archive_file: Callable[[int, str, _FileSnapshot], str],
    ) -> None:
        self.parent_fd = parent_fd
        self.target = target
        self.backup = backup
        self.installed = installed
        self.backup_snapshot = backup_snapshot
        self.snapshot_reader = snapshot_reader
        self.archive_file = archive_file
        self.closed = False

    def _snapshot(self, name: str, role: str) -> _FileSnapshot:
        try:
            return self.snapshot_reader(self.parent_fd, name)
        except BaseException as exc:
            backup = f"; preserved original as {self.backup}" if self.backup is not None else ""
            raise RenderError(f"publication cannot safely read {role}{backup}") from exc

    def _reverse_exchange(self, message: str) -> None:
        assert self.backup is not None
        try:
            _renameat2(
                self.parent_fd,
                self.backup,
                self.parent_fd,
                self.target,
                RENAME_EXCHANGE,
            )
        except OSError as exc:
            raise RenderError(f"{message}; preserved displaced file as {self.backup}") from exc

    def _archive_transaction(self, name: str, snapshot: _FileSnapshot) -> None:
        self.archive_file(self.parent_fd, name, snapshot)

    def commit(self) -> None:
        if self.closed:
            return
        commit_error: BaseException | None = None
        try:
            if self.backup is not None:
                if self.backup_snapshot is None:
                    raise RenderError(f"publication has no snapshot for preserved original {self.backup}")
                current_backup = self._snapshot(self.backup, "preserved original")
                if current_backup != self.backup_snapshot:
                    raise RenderError(
                        f"publication preserved a concurrently changed original as {self.backup}"
                    )
                self._archive_transaction(self.backup, self.backup_snapshot)
            try:
                os.fsync(self.parent_fd)
            except OSError:
                # The replacement was already fsynced before this cleanup handle was returned.
                pass
        except BaseException as exc:
            commit_error = exc
        finally:
            os.close(self.parent_fd)
            self.closed = True
        if commit_error is not None:
            raise commit_error

    def rollback(self) -> None:
        if self.closed:
            return
        rollback_error: BaseException | None = None
        try:
            current = self._snapshot(self.target, "current published target")
            if current != self.installed:
                backup = f"; preserved original as {self.backup}" if self.backup is not None else ""
                raise RenderError(f"publication rollback refused to overwrite a concurrent edit{backup}")
            if self.backup is not None:
                if self.backup_snapshot is None:
                    raise RenderError(f"publication rollback has no snapshot for preserved original {self.backup}")
                original = self._snapshot(self.backup, "preserved original")
                if original != self.backup_snapshot:
                    raise RenderError(
                        f"publication rollback refused to overwrite a changed backup; preserved original as {self.backup}"
                    )
                self._reverse_exchange("publication rollback could not restore the original")
                displaced: _FileSnapshot | None = None
                restored: _FileSnapshot | None = None
                try:
                    displaced = self._snapshot(self.backup, "displaced published target")
                except RenderError:
                    pass
                try:
                    restored = self._snapshot(self.target, "restored original")
                except RenderError:
                    pass
                if displaced == self.installed and restored == self.backup_snapshot:
                    self._archive_transaction(self.backup, self.installed)
                elif restored == self.backup_snapshot:
                    self._reverse_exchange("publication rollback could not expose a concurrent edit")
                    raise RenderError(
                        f"publication rollback detected a concurrent target change; "
                        f"preserved original as {self.backup}"
                    )
                else:
                    raise RenderError(
                        f"publication rollback preserved a concurrent visible edit and displaced file as {self.backup}"
                    )
            else:
                recovery = f".{self.target}.{secrets.token_hex(12)}.rollback"
                os.rename(self.target, recovery, src_dir_fd=self.parent_fd, dst_dir_fd=self.parent_fd)
                try:
                    displaced = self._snapshot(recovery, "displaced published target")
                except BaseException as snapshot_error:
                    try:
                        _renameat2(
                            self.parent_fd,
                            recovery,
                            self.parent_fd,
                            self.target,
                            RENAME_NOREPLACE,
                        )
                    except OSError as exc:
                        raise RenderError(
                            f"publication rollback could not restore an unsafe target; "
                            f"preserved displaced file as {recovery}"
                        ) from exc
                    raise snapshot_error
                if displaced != self.installed:
                    try:
                        _renameat2(
                            self.parent_fd,
                            recovery,
                            self.parent_fd,
                            self.target,
                            RENAME_NOREPLACE,
                        )
                    except OSError as exc:
                        raise RenderError(
                            f"publication rollback conflicted with a concurrent edit; preserved displaced file as {recovery}"
                        ) from exc
                    raise RenderError("publication rollback refused to remove a concurrently edited file")
                self._archive_transaction(recovery, self.installed)
            try:
                os.fsync(self.parent_fd)
            except OSError:
                pass
        except BaseException as exc:
            rollback_error = exc
        finally:
            os.close(self.parent_fd)
            self.closed = True
        if rollback_error is not None:
            if isinstance(rollback_error, RenderError):
                raise rollback_error
            raise RenderError(f"failed to roll back project publication: {rollback_error}") from rollback_error


class Registry:
    """Core registry shared by the CLI and FastMCP adapter."""

    def __init__(self, project_root: str | pathlib.Path = ".", *, runner: Runner | None = None) -> None:
        root = pathlib.Path(project_root).expanduser()
        try:
            self.project_root = root.resolve(strict=True)
        except OSError as exc:
            raise PolicyError(f"consumer project root does not exist: {project_root}") from exc
        if not self.project_root.is_dir():
            raise PolicyError(f"consumer project root is not a directory: {project_root}")
        self.assets = asset_root()
        try:
            self._project_root_fd = os.open(
                self.project_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise PolicyError(f"consumer project root cannot be pinned safely: {project_root}") from exc
        self._runner = runner if runner is not None else _run_command

    def close(self) -> None:
        descriptor = getattr(self, "_project_root_fd", -1)
        if descriptor >= 0:
            self._project_root_fd = -1
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def initialize_project(self, *, force: bool = False) -> dict[str, Any]:
        manifest_data = (
            "version: 1\n"
            f"profile: {DEFAULT_PROFILE}\n"
            f"family: {DEFAULT_FAMILY}\n"
            "visualizations: []\n"
        ).encode("utf-8")
        relative = self._project_write_relative(MANIFEST_NAME, description="visualization manifest")
        with self._project_lock():
            snapshot = self._project_file_snapshot(
                relative,
                max_bytes=1024 * 1024,
                description="visualization manifest",
            )
            if snapshot.exists and not force:
                return {
                    "ok": True,
                    "project": str(self.project_root),
                    "created": [],
                    "ensured": [".cache/vegavisuals"],
                    "preserved": [MANIFEST_NAME],
                    "manifest": MANIFEST_NAME,
                    "force": False,
                }
            publication = self._replace_project_bytes(
                relative,
                manifest_data,
                expected=snapshot,
            )
            publication.commit()
        return {
            "ok": True,
            "project": str(self.project_root),
            "created": [MANIFEST_NAME],
            "ensured": [".cache/vegavisuals"],
            "preserved": [],
            "manifest": MANIFEST_NAME,
            "force": force,
        }

    def resolve_project_path(
        self,
        raw: str | pathlib.Path,
        *,
        must_exist: bool = False,
        description: str = "path",
    ) -> pathlib.Path:
        if not str(raw).strip():
            raise PolicyError(f"{description} cannot be empty")
        candidate = pathlib.Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise PolicyError(f"cannot resolve {description}: {raw}") from exc
        if not _path_within(resolved, self.project_root):
            raise PolicyError(f"{description} is outside the consumer project: {raw}")
        if must_exist and not resolved.is_file():
            raise ValidationError(f"{description} is not a file: {raw}")
        return resolved

    def _project_write_relative(
        self,
        raw: str | pathlib.Path,
        *,
        description: str,
    ) -> str:
        raw_text = str(raw)
        if not raw_text.strip():
            raise PolicyError(f"{description} cannot be empty")
        if "\0" in raw_text:
            raise PolicyError(f"{description} contains a null byte")
        try:
            raw_text.encode("utf-8")
            candidate = pathlib.Path(raw_text).expanduser()
        except (UnicodeEncodeError, RuntimeError, OSError) as exc:
            raise PolicyError(f"{description} is not a valid UTF-8 project path") from exc
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        normalized = pathlib.Path(os.path.abspath(candidate))
        try:
            relative = normalized.relative_to(self.project_root)
        except ValueError as exc:
            raise PolicyError(f"{description} is outside the consumer project: {raw}") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise PolicyError(f"{description} must identify a project file: {raw}")
        return relative.as_posix()

    def _manifest_project_relative(self, raw: str, *, description: str) -> str:
        parsed = urllib.parse.urlsplit(raw)
        candidate = pathlib.Path(raw)
        if (
            raw != raw.strip()
            or "\\" in raw
            or candidate.is_absolute()
            or ".." in candidate.parts
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ManifestError(f"{description} must be a safe project-relative path: {raw}")
        return self._project_write_relative(raw, description=description)

    def _dependency_project_relative(self, raw: Any, *, description: str) -> str:
        if not isinstance(raw, str) or not raw.strip() or raw != raw.strip():
            raise ValidationError(f"{description} must be a non-empty project-relative string")
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or any(character in raw for character in "${}")
        ):
            raise PolicyError(f"{description} must be a static project-relative path: {raw}")
        decoded = urllib.parse.unquote(parsed.path)
        candidate = pathlib.Path(decoded)
        if "\\" in decoded or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise PolicyError(f"{description} must be a confined project-relative path: {raw}")
        return self._project_write_relative(decoded, description=description)

    @contextmanager
    def _open_project_parent(self, relative: str, *, create: bool) -> Iterable[tuple[int, str]]:
        path = pathlib.PurePosixPath(relative)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise PolicyError(f"invalid project-relative path: {relative}")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        if self._project_root_fd < 0:
            raise PolicyError("consumer project root descriptor is closed")
        current = os.dup(self._project_root_fd)
        try:
            for part in path.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=current)
                    except FileExistsError:
                        pass
                try:
                    next_fd = os.open(part, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    raise
                except OSError as exc:
                    raise PolicyError(
                        f"project path contains a missing, non-directory, or symlinked parent: {relative}"
                    ) from exc
                os.close(current)
                current = next_fd
            yield current, path.name
        finally:
            os.close(current)

    @contextmanager
    def _project_lock(self) -> Iterable[None]:
        relative = self._project_write_relative(PROJECT_LOCK_PATH, description="project render lock")
        with self._open_project_parent(relative, create=True) as (parent_fd, name):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise PolicyError("project render lock is not a regular no-follow file") from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise PolicyError("project render lock is not a regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _read_regular_descriptor(
        self,
        descriptor: int,
        *,
        description: str,
        max_bytes: int | None = None,
        collect: bool,
    ) -> tuple[_FileSnapshot, bytes | None]:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PolicyError(f"{description} is not a regular file")
        if max_bytes is not None and before.st_size > max_bytes:
            raise ValidationError(f"{description} exceeds {max_bytes} bytes")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if max_bytes is not None and total > max_bytes:
                raise ValidationError(f"{description} exceeds {max_bytes} bytes")
            digest.update(block)
            if collect:
                chunks.append(block)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after or total != after.st_size:
            raise PolicyError(f"{description} changed while it was being read")
        snapshot = _FileSnapshot(
            exists=True,
            sha256=digest.hexdigest(),
            size=total,
            device=after.st_dev,
            inode=after.st_ino,
        )
        return snapshot, b"".join(chunks) if collect else None

    def _read_regular_at(
        self,
        parent_fd: int,
        name: str,
        *,
        description: str,
        max_bytes: int | None,
        collect: bool,
    ) -> tuple[_FileSnapshot, bytes | None]:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PolicyError(f"{description} must not be a symlink") from exc
            raise ValidationError(f"cannot read {description}: {exc}") from exc
        try:
            return self._read_regular_descriptor(
                descriptor,
                description=description,
                max_bytes=max_bytes,
                collect=collect,
            )
        finally:
            os.close(descriptor)

    def _read_project_snapshot(
        self,
        relative: str,
        *,
        description: str,
        max_bytes: int | None,
        collect: bool,
        missing_ok: bool,
    ) -> tuple[_FileSnapshot, bytes | None] | None:
        try:
            with self._open_project_parent(relative, create=False) as (parent_fd, name):
                try:
                    return self._read_regular_at(
                        parent_fd,
                        name,
                        description=f"{description}: {relative}",
                        max_bytes=max_bytes,
                        collect=collect,
                    )
                except ValidationError as exc:
                    if missing_ok and isinstance(exc.__cause__, FileNotFoundError):
                        return None
                    raise
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ValidationError(f"cannot read {description}: file does not exist: {relative}")

    def _archive_publication_file(
        self,
        source_fd: int,
        source_name: str,
        expected: _FileSnapshot,
    ) -> str:
        if not expected.exists or expected.sha256 is None:
            raise RenderError("cannot archive a missing publication snapshot")
        relative = (
            f".cache/vegavisuals/replaced/{expected.sha256[:16]}-"
            f"{secrets.token_hex(12)}.replaced"
        )
        with self._open_project_parent(relative, create=True) as (archive_fd, archive_name):
            os.fchmod(archive_fd, 0o700)
            try:
                _renameat2(
                    source_fd,
                    source_name,
                    archive_fd,
                    archive_name,
                    RENAME_NOREPLACE,
                )
            except OSError as exc:
                raise RenderError(
                    f"could not preserve replaced file in {relative}; original remains as {source_name}"
                ) from exc
            archived, _ = self._read_regular_at(
                archive_fd,
                archive_name,
                description=f"archived replaced file {relative}",
                max_bytes=expected.size,
                collect=False,
            )
            if archived != expected:
                raise RenderError(
                    f"replaced file changed while being archived; preserved it as {relative}"
                )
            os.fsync(source_fd)
            os.fsync(archive_fd)
        return relative

    def _require_recovery_filesystem(self, output_relative: str) -> None:
        probe = ".cache/vegavisuals/replaced/.filesystem-probe"
        with self._open_project_parent(output_relative, create=True) as (output_fd, _):
            with self._open_project_parent(probe, create=True) as (archive_fd, _):
                os.fchmod(archive_fd, 0o700)
                devices = {
                    os.fstat(self._project_root_fd).st_dev,
                    os.fstat(output_fd).st_dev,
                    os.fstat(archive_fd).st_dev,
                }
                if len(devices) != 1:
                    raise PolicyError(
                        "project lock, visualization output, and .cache/vegavisuals/replaced "
                        "must share one filesystem"
                    )

    def _read_project_bytes(
        self,
        relative: str,
        *,
        description: str,
        max_bytes: int | None = None,
    ) -> bytes:
        result = self._read_project_snapshot(
            relative,
            description=description,
            max_bytes=max_bytes,
            collect=True,
            missing_ok=False,
        )
        assert result is not None and result[1] is not None
        return result[1]

    def _project_file_snapshot(
        self,
        relative: str,
        *,
        max_bytes: int | None = None,
        description: str = "managed project file",
    ) -> _FileSnapshot:
        result = self._read_project_snapshot(
            relative,
            description=description,
            max_bytes=max_bytes,
            collect=False,
            missing_ok=True,
        )
        return result[0] if result is not None else _FileSnapshot(exists=False)

    def _project_file_hash(
        self,
        relative: str,
        *,
        max_bytes: int | None = None,
        description: str = "managed project file",
    ) -> tuple[str, int] | None:
        snapshot = self._project_file_snapshot(relative, max_bytes=max_bytes, description=description)
        if not snapshot.exists:
            return None
        assert snapshot.sha256 is not None
        return snapshot.sha256, snapshot.size

    def _replace_project_bytes(
        self,
        relative: str,
        data: bytes,
        *,
        mode: int = 0o644,
        expected: _FileSnapshot | None = None,
        transaction_log: list[_ProjectPublication] | None = None,
    ) -> _ProjectPublication:
        with self._open_project_parent(relative, create=True) as (parent_fd, name):
            transaction_fd = os.dup(parent_fd)
        temporary = ""
        backup: str | None = None
        backup_snapshot: _FileSnapshot | None = None
        published = False
        preserve_on_error = False
        installed: _FileSnapshot | None = None
        publication: _ProjectPublication | None = None
        snapshot_limit = len(data)
        try:
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                mode,
                dir_fd=transaction_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            installed, _ = self._read_regular_at(
                transaction_fd,
                temporary,
                description=f"publication candidate for {relative}",
                max_bytes=len(data),
                collect=False,
            )
            if installed.sha256 != _sha256_bytes(data) or installed.size != len(data):
                raise RenderError(f"publication candidate changed before installation: {relative}")

            if expected is not None and not expected.exists:
                try:
                    _renameat2(transaction_fd, temporary, transaction_fd, name, RENAME_NOREPLACE)
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise RenderError(f"managed path appeared while it was being published: {relative}") from exc
                    raise PolicyError(f"safe no-replace publication is unavailable: {exc}") from exc
                published = True
            elif expected is not None:
                try:
                    _renameat2(transaction_fd, temporary, transaction_fd, name, RENAME_EXCHANGE)
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        raise RenderError(f"managed path disappeared while it was being published: {relative}") from exc
                    raise PolicyError(f"safe exchange publication is unavailable: {exc}") from exc
                backup = temporary
                backup_snapshot = expected
                published = True
                snapshot_limit = max(snapshot_limit, expected.size)
                try:
                    displaced, _ = self._read_regular_at(
                        transaction_fd,
                        temporary,
                        description=f"displaced managed path {relative}",
                        max_bytes=expected.size,
                        collect=False,
                    )
                    if displaced != expected:
                        raise RenderError(f"managed path changed while it was being published: {relative}")
                except BaseException as exc:
                    try:
                        _renameat2(transaction_fd, temporary, transaction_fd, name, RENAME_EXCHANGE)
                    except OSError as reverse_error:
                        preserve_on_error = True
                        preserved = pathlib.PurePosixPath(relative).with_name(temporary).as_posix()
                        raise RenderError(
                            f"managed path changed and the safe exchange could not be reversed; "
                            f"preserved displaced file as {preserved}"
                        ) from reverse_error
                    published = False
                    backup = None
                    backup_snapshot = None
                    if isinstance(exc, RenderError):
                        raise
                    raise RenderError(f"managed path changed while it was being published: {relative}") from exc
                backup_snapshot = displaced
            else:
                try:
                    target_stat = os.stat(name, dir_fd=transaction_fd, follow_symlinks=False)
                except FileNotFoundError:
                    target_stat = None
                if target_stat is not None:
                    if not stat.S_ISREG(target_stat.st_mode):
                        raise PolicyError(f"refusing to replace a non-regular managed path: {relative}")
                    backup = f".{name}.{secrets.token_hex(12)}.bak"
                    os.rename(name, backup, src_dir_fd=transaction_fd, dst_dir_fd=transaction_fd)
                    backup_snapshot, _ = self._read_regular_at(
                        transaction_fd,
                        backup,
                        description=f"preserved managed path {relative}",
                        max_bytes=target_stat.st_size,
                        collect=False,
                    )
                    snapshot_limit = max(snapshot_limit, backup_snapshot.size)
                os.rename(temporary, name, src_dir_fd=transaction_fd, dst_dir_fd=transaction_fd)
                published = True
            os.fsync(transaction_fd)
            publication = _ProjectPublication(
                transaction_fd,
                name,
                backup,
                installed,
                backup_snapshot,
                lambda directory_fd, entry: self._read_regular_at(
                    directory_fd,
                    entry,
                    description=f"published managed path {relative}",
                    max_bytes=snapshot_limit,
                    collect=False,
                )[0],
                self._archive_publication_file,
            )
            if transaction_log is not None:
                transaction_log.append(publication)
            return publication
        except BaseException:
            if preserve_on_error:
                try:
                    os.fsync(transaction_fd)
                except OSError:
                    pass
                os.close(transaction_fd)
                raise
            if publication is not None:
                publication.rollback()
            elif published and installed is not None:
                _ProjectPublication(
                    transaction_fd,
                    name,
                    backup,
                    installed,
                    backup_snapshot,
                    lambda directory_fd, entry: self._read_regular_at(
                        directory_fd,
                        entry,
                        description=f"published managed path {relative}",
                        max_bytes=snapshot_limit,
                        collect=False,
                    )[0],
                    self._archive_publication_file,
                ).rollback()
            else:
                try:
                    if temporary:
                        try:
                            os.unlink(temporary, dir_fd=transaction_fd)
                        except FileNotFoundError:
                            pass
                    if backup is not None:
                        try:
                            _renameat2(transaction_fd, backup, transaction_fd, name, RENAME_NOREPLACE)
                        except OSError:
                            pass
                finally:
                    os.close(transaction_fd)
            raise

    def _write_project_json(self, relative: str, value: Any) -> None:
        data = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        publication = self._replace_project_bytes(relative, data)
        publication.commit()

    def _publish_owned_project_bytes(self, relative: str, data: bytes, *, max_bytes: int) -> None:
        if len(data) > max_bytes:
            raise ValidationError(f"owned project file exceeds {max_bytes} bytes: {relative}")
        with self._open_project_parent(relative, create=True) as (parent_fd, name):
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=parent_fd,
                )
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fchmod(descriptor, 0o644)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                candidate, _ = self._read_regular_at(
                    parent_fd,
                    temporary,
                    description=f"owned publication candidate for {relative}",
                    max_bytes=max_bytes,
                    collect=False,
                )
                if candidate.sha256 != _sha256_bytes(data) or candidate.size != len(data):
                    raise RenderError(f"owned publication candidate changed before installation: {relative}")
                try:
                    target = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    target = None
                if target is not None and stat.S_ISDIR(target.st_mode):
                    raise PolicyError(f"refusing to replace a directory at owned project path: {relative}")
                os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                temporary = ""
                os.fsync(parent_fd)
            finally:
                if temporary:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass

    def _invalidate_receipt(self) -> None:
        try:
            with self._open_project_parent(RECEIPT_PATH, create=False) as (parent_fd, name):
                try:
                    target = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if stat.S_ISDIR(target.st_mode):
                    return
        except (FileNotFoundError, PolicyError):
            return
        invalid = _json_bytes({"schema_version": 1, "provider": "vegavisuals", "ok": False}) + b"\n"
        self._publish_owned_project_bytes(RECEIPT_PATH, invalid, max_bytes=MAX_RECEIPT_BYTES)

    def _asset_path(self, category: str, name: str) -> pathlib.Path:
        normalized = name.removesuffix(".json")
        if not SAFE_ASSET_NAME.fullmatch(normalized):
            raise ValidationError(f"invalid {category} name: {name}")
        path = self.assets / category / f"{normalized}.json"
        if not path.is_file():
            raise ValidationError(f"unknown {category}: {normalized}")
        return path

    def _load_profile(self, profile: str) -> tuple[str, dict[str, Any], pathlib.Path]:
        normalized = profile.removesuffix(".json")
        path = self._asset_path("compat", normalized)
        value = _load_json_file(path, f"compatibility profile {normalized}")
        if not isinstance(value, dict):
            raise ValidationError(f"compatibility profile must be an object: {normalized}")
        required = {
            "id",
            "image",
            "base_image",
            "dockerfile",
            "vl_convert_python",
            "vega_runtime",
            "vega_lite",
            "engines",
            "formats",
            "fonts",
            "network_policy",
            "runtime_limits",
        }
        missing = sorted(required - value.keys())
        if missing:
            raise ValidationError(f"compatibility profile {normalized} is missing: {', '.join(missing)}")
        allowed = required | {"pdf_normalizer"}
        unknown = sorted(value.keys() - allowed)
        if unknown:
            raise ValidationError(f"compatibility profile {normalized} has unknown fields: {', '.join(unknown)}")
        if value.get("id") != normalized:
            raise ValidationError(f"compatibility profile id does not match filename: {normalized}")
        for field in ("image", "base_image", "dockerfile", "vl_convert_python", "vega_runtime"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise ValidationError(f"compatibility profile {normalized} requires string field {field}")
        if "@sha256:" not in value["base_image"] or not IMAGE_ID_RE.fullmatch(
            "sha256:" + value["base_image"].rsplit("@sha256:", 1)[1]
        ):
            raise ValidationError(f"compatibility profile base image must be pinned by sha256 digest: {normalized}")
        dockerfile_relative = pathlib.PurePosixPath(value["dockerfile"])
        if dockerfile_relative.is_absolute() or ".." in dockerfile_relative.parts:
            raise ValidationError(f"compatibility profile dockerfile must stay inside assets: {normalized}")
        if not (self.assets / dockerfile_relative).is_file():
            raise ValidationError(f"compatibility profile dockerfile is missing: {normalized}")
        if value.get("engines") != ["vega-lite", "vega"]:
            raise ValidationError(f"compatibility profile has an invalid engine contract: {normalized}")
        if value.get("formats") != ["svg", "png", "pdf"]:
            raise ValidationError(f"compatibility profile has an invalid format contract: {normalized}")
        vega_lite = value.get("vega_lite")
        if not isinstance(vega_lite, dict) or set(vega_lite) != {"default", "supported"}:
            raise ValidationError(f"compatibility profile has an invalid Vega-Lite contract: {normalized}")
        supported = vega_lite.get("supported")
        if (
            not isinstance(vega_lite.get("default"), str)
            or not isinstance(supported, list)
            or not supported
            or not all(isinstance(item, str) and re.fullmatch(r"[0-9]+\.[0-9]+", item) for item in supported)
            or len(set(supported)) != len(supported)
            or vega_lite["default"] not in supported
        ):
            raise ValidationError(f"compatibility profile has invalid Vega-Lite versions: {normalized}")
        network = value.get("network_policy")
        if (
            not isinstance(network, dict)
            or set(network) != {"runtime", "remote_data", "local_data"}
            or network.get("runtime") != "none"
            or network.get("remote_data") != "deny"
            or network.get("local_data") != "project-relative-host-inlined"
        ):
            raise ValidationError(f"compatibility profile must deny renderer networking: {normalized}")
        fonts = value.get("fonts")
        if not isinstance(fonts, list) or not fonts:
            raise ValidationError(f"compatibility profile requires a font inventory: {normalized}")
        for font in fonts:
            if (
                not isinstance(font, dict)
                or set(font) != {"family", "package", "version"}
                or not all(isinstance(font.get(key), str) and font[key] for key in font)
            ):
                raise ValidationError(f"compatibility profile contains an invalid font entry: {normalized}")
        limits = value.get("runtime_limits")
        integer_limits = {
            "pids",
            "timeout_seconds",
            "max_source_bytes",
            "max_inline_source_bytes",
            "max_dependency_bytes",
            "max_prepared_spec_bytes",
            "max_output_bytes",
            "max_inline_response_bytes",
        }
        expected_limits = integer_limits | {"cpus", "memory", "tmpfs"}
        if not isinstance(limits, dict) or set(limits) != expected_limits:
            raise ValidationError(f"compatibility profile has invalid runtime limit fields: {normalized}")
        if any(type(limits[key]) is not int or limits[key] <= 0 for key in integer_limits):
            raise ValidationError(f"compatibility profile runtime integer limits must be positive: {normalized}")
        try:
            cpus = float(limits["cpus"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"compatibility profile cpus limit is invalid: {normalized}") from exc
        if not math.isfinite(cpus) or cpus <= 0:
            raise ValidationError(f"compatibility profile cpus limit must be positive: {normalized}")
        for field in ("memory", "tmpfs"):
            if not isinstance(limits[field], str) or not re.fullmatch(r"[1-9][0-9]*[kKmMgG]", limits[field]):
                raise ValidationError(f"compatibility profile {field} limit is invalid: {normalized}")
        normalizer = value.get("pdf_normalizer")
        if (
            not isinstance(normalizer, dict)
            or set(normalizer) != {"tool", "version", "arguments"}
            or normalizer.get("tool") != "qpdf"
            or not isinstance(normalizer.get("version"), str)
            or not normalizer["version"]
            or normalizer.get("arguments") != ["--deterministic-id", "--object-streams=disable"]
        ):
            raise ValidationError(f"compatibility profile has an invalid PDF normalizer contract: {normalized}")
        return normalized, value, path

    def _load_theme(self, family: str) -> tuple[str, dict[str, Any], pathlib.Path, pathlib.Path]:
        normalized = family.removesuffix(".json")
        path = self._asset_path("themes", normalized)
        token_path = self._asset_path("tokens", normalized)
        theme = _load_json_file(path, f"theme {normalized}")
        tokens = _load_json_file(token_path, f"tokens {normalized}")
        if not isinstance(theme, dict) or not isinstance(tokens, dict):
            raise ValidationError(f"theme and token documents must be objects: {normalized}")
        if tokens.get("family") != normalized:
            raise ValidationError(f"token family does not match filename: {normalized}")
        if not isinstance(theme.get("font"), str) or not isinstance(theme.get("range"), dict):
            raise ValidationError(f"theme is not a Vega/Vega-Lite config object: {normalized}")
        token_font = tokens.get("font")
        colors = tokens.get("colors")
        if (
            set(tokens) != {"family", "font", "colors"}
            or not isinstance(token_font, dict)
            or set(token_font) != {"sans", "mono"}
            or not all(isinstance(key, str) and isinstance(value, str) and value for key, value in token_font.items())
            or not isinstance(colors, dict)
            or not colors
            or not all(
                isinstance(key, str) and isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value)
                for key, value in colors.items()
            )
        ):
            raise ValidationError(f"tokens have an invalid design-token contract: {normalized}")
        if theme["font"] != token_font.get("sans"):
            raise ValidationError(f"theme font does not match the sans token: {normalized}")
        category = theme["range"].get("category")
        if not isinstance(category, list) or not category or not all(isinstance(item, str) for item in category):
            raise ValidationError(f"theme category range is invalid: {normalized}")
        token_colors = set(colors.values())
        if any(item not in token_colors for item in category):
            raise ValidationError(f"theme category range is not backed by design tokens: {normalized}")
        stack: list[Any] = [theme]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
            elif isinstance(current, str) and current.startswith("#"):
                if not re.fullmatch(r"#[0-9A-Fa-f]{6}", current) or current not in token_colors:
                    raise ValidationError(f"theme color is not backed by design tokens: {normalized}: {current}")
        return normalized, theme, path, token_path

    def compatibility_status(self, profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
        profiles: list[dict[str, Any]] = []
        for path in sorted((self.assets / "compat").glob("*.json")):
            _, value, _ = self._load_profile(path.stem)
            profiles.append(
                {
                    "name": path.stem,
                    "path": f"compat/{path.name}",
                    "image": value["image"],
                    "base_image": value["base_image"],
                    "vl_convert_python": value["vl_convert_python"],
                    "vega_runtime": value["vega_runtime"],
                    "vega_lite": value["vega_lite"],
                    "formats": value["formats"],
                }
            )
        normalized, selected, selected_path = self._load_profile(profile)
        return {
            "ok": True,
            "requested": normalized,
            "requested_path": f"compat/{selected_path.name}",
            "profile": selected,
            "profiles": profiles,
            "count": len(profiles),
        }

    def profile_inventory(self) -> dict[str, Any]:
        return self.compatibility_status(DEFAULT_PROFILE)

    def theme_inventory(self, family: str = "") -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for path in sorted((self.assets / "themes").glob("*.json")):
            if family and path.stem != family.removesuffix(".json"):
                continue
            normalized, theme, theme_path, token_path = self._load_theme(path.stem)
            items.append(
                {
                    "family": normalized,
                    "theme": f"themes/{theme_path.name}",
                    "tokens": f"tokens/{token_path.name}",
                    "theme_sha256": _sha256_bytes(_json_bytes(theme)),
                    "tokens_sha256": _sha256_file(token_path),
                    "font": theme.get("font"),
                    "category_range": theme.get("range", {}).get("category", []),
                }
            )
        if family and not items:
            raise ValidationError(f"unknown theme family: {family}")
        return {"ok": True, "families": items, "count": len(items), "default": DEFAULT_FAMILY}

    def factory_check(
        self,
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
    ) -> dict[str, Any]:
        issues: list[str] = []
        try:
            compatibility = self.compatibility_status(profile)
        except ValidationError as exc:
            compatibility = {"ok": False, "error": str(exc)}
            issues.append(str(exc))
        try:
            themes = self.theme_inventory(family)
        except ValidationError as exc:
            themes = {"ok": False, "error": str(exc)}
            issues.append(str(exc))
        dockerfile = self.assets / "Dockerfile"
        worker = self.assets / "docker" / "worker.py"
        discovery_manifest = factory_metadata_root() / "mcp-factory.yml"
        if not dockerfile.is_file():
            issues.append("packaged Dockerfile is missing")
        if not worker.is_file():
            issues.append("packaged renderer worker is missing")
        if not discovery_manifest.is_file():
            issues.append("factory discovery manifest is missing")
        discovery: dict[str, Any] | None = None
        if discovery_manifest.is_file():
            try:
                raw = discovery_manifest.read_bytes()
                if len(raw) > 1024 * 1024:
                    raise ValidationError("factory discovery manifest exceeds 1 MiB")
                text = raw.decode("utf-8")
                if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)):
                    raise ValidationError("factory discovery manifest must not use YAML aliases")
                loaded = yaml.load(text, Loader=_UniqueKeyLoader)
                if not isinstance(loaded, dict):
                    raise ValidationError("factory discovery manifest must be a mapping")
                discovery = loaded
                dynamic = self.factory_manifest()
                checkout = source_checkout()
                parity = {
                    "schema_version": dynamic["schema_version"],
                    "name": dynamic["name"],
                    "version": dynamic["version"],
                    "kind": dynamic["kind"],
                    "description": dynamic["description"],
                    "license": dynamic["license"],
                    "repository": dynamic["repository"],
                    "workspace_rule": dynamic["workspace_rule"],
                    "runtime": dynamic["runtime"],
                    "discovery": dynamic["discovery"],
                    "release": dynamic["release"],
                    "defaults": dynamic["defaults"],
                    "contracts": dynamic["contracts"],
                }
                for key, expected in parity.items():
                    if discovery.get(key) != expected:
                        issues.append(f"static factory manifest does not match dynamic {key}")

                def normalize_command(command: Any) -> Any:
                    if checkout is None and isinstance(command, list) and command[:1] == ["vegavisuals"]:
                        return [sys.executable, "-m", "vegavisuals.cli", *command[1:]]
                    return command

                static_transport = discovery.get("transport")
                if not isinstance(static_transport, dict) or {
                    **static_transport,
                    "command": normalize_command(static_transport.get("command")),
                } != dynamic["transport"]:
                    issues.append("static factory manifest does not match dynamic transport")
                static_commands = discovery.get("commands")
                if not isinstance(static_commands, dict) or {
                    key: normalize_command(command) for key, command in static_commands.items()
                } != dynamic["commands"]:
                    issues.append("static factory manifest does not match dynamic commands")
                static_mcp = discovery.get("mcp")
                static_assets = discovery.get("factory_assets")
                if not isinstance(static_assets, str) or (
                    discovery_manifest.parent / static_assets
                ).resolve() != self.assets:
                    issues.append("static factory assets path is invalid")
                if checkout is None:
                    serialized = json.dumps(discovery, sort_keys=True)
                    for forbidden in (
                        "${factoryRoot}",
                        "scripts/factory-launcher",
                        "src/vegavisuals/assets",
                        '"make"',
                    ):
                        if forbidden in serialized:
                            issues.append(f"installed factory manifest references checkout-only value: {forbidden}")
                if not isinstance(static_mcp, dict):
                    issues.append("static factory manifest has no MCP contract")
                else:
                    for key in ("server_name", "transport", "consumer_root_fixed_at_startup"):
                        if static_mcp.get(key) != dynamic["mcp"][key]:
                            issues.append(f"static factory MCP {key} does not match the adapter")
                    if static_mcp.get("required_tools") != list(MCP_TOOL_NAMES):
                        issues.append("static factory tools do not match the MCP adapter")
                    if static_mcp.get("resources") != list(MCP_RESOURCE_URIS):
                        issues.append("static factory resources do not match the MCP adapter")
            except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
                issues.append(str(exc))
        if dockerfile.is_file():
            dockerfile_text = dockerfile.read_text(encoding="utf-8")
            if "ARG BASE_IMAGE=" not in dockerfile_text or "FROM ${BASE_IMAGE}" not in dockerfile_text:
                issues.append("packaged Dockerfile does not consume the pinned base image argument")
            if "qpdf=" not in dockerfile_text:
                issues.append("packaged Dockerfile does not install the PDF normalizer")
        if worker.is_file():
            try:
                compile(worker.read_text(encoding="utf-8"), str(worker), "exec")
            except (OSError, SyntaxError) as exc:
                issues.append(f"packaged renderer worker is invalid: {exc}")
        return {
            "ok": not issues,
            "issues": issues,
            "compatibility": compatibility,
            "themes": themes,
            "assets": {
                "dockerfile": dockerfile.is_file(),
                "worker": worker.is_file(),
                "factory_manifest": discovery_manifest.is_file(),
            },
            "discovery": {
                "path": str(discovery_manifest),
                "parsed": discovery is not None,
            },
        }

    def _normalize_engine(
        self,
        requested: str,
        *,
        source_name: str = "",
        spec: dict[str, Any] | None = None,
    ) -> str:
        normalized_requested = (requested or "auto").strip().lower()
        inferred: str | None = None
        lower_name = source_name.lower()
        if lower_name.endswith(".vl.json"):
            inferred = "vega-lite"
        elif lower_name.endswith(".vg.json"):
            inferred = "vega"

        if normalized_requested != "auto":
            normalized = ENGINE_ALIASES.get(normalized_requested)
            if not normalized:
                raise ValidationError("engine must be auto, vega-lite, or vega")
            if inferred and inferred != normalized:
                raise ValidationError(f"source suffix identifies {inferred}, not {normalized}")
            return normalized
        if inferred:
            return inferred
        if spec is not None:
            schema = str(spec.get("$schema") or "").lower()
            if "/vega-lite/" in schema:
                return "vega-lite"
            if "/vega/" in schema:
                return "vega"
            if "mark" in spec and "marks" not in spec:
                return "vega-lite"
            if "marks" in spec and isinstance(spec.get("marks"), list):
                return "vega"
        raise ValidationError("engine cannot be inferred; use vega-lite or vega explicitly")

    def _select_vega_lite_version(
        self,
        spec: dict[str, Any],
        engine: str,
        profile_data: dict[str, Any],
    ) -> str | None:
        schema = spec.get("$schema")
        if schema is not None and (not isinstance(schema, str) or not schema.strip()):
            raise ValidationError("$schema must be a non-empty string when present")
        if engine == "vega-lite":
            default = profile_data["vega_lite"]["default"]
            supported = profile_data["vega_lite"]["supported"]
            if schema is None:
                return default
            match = VEGA_LITE_SCHEMA_RE.search(schema)
            if match is None:
                raise ValidationError("Vega-Lite source has an unsupported $schema URL")
            requested = match.group("version")
            if "." in requested:
                if requested not in supported:
                    raise ValidationError(f"Vega-Lite schema version is not supported by the profile: {requested}")
                return requested
            major = requested
            if default.split(".", 1)[0] == major:
                return default
            candidates = [item for item in supported if item.split(".", 1)[0] == major]
            if not candidates:
                raise ValidationError(f"Vega-Lite schema major version is not supported by the profile: {major}")
            return max(candidates, key=lambda item: tuple(int(part) for part in item.split(".")))

        if schema is not None:
            match = VEGA_SCHEMA_RE.search(schema)
            if match is None:
                raise ValidationError("raw Vega source has an unsupported $schema URL")
            requested = match.group("version")
            runtime = profile_data["vega_runtime"]
            if requested.split(".", 1)[0] != runtime.split(".", 1)[0]:
                raise ValidationError(f"Vega schema version is not supported by the profile: {requested}")
            if "." in requested and requested != runtime:
                raise ValidationError(f"Vega schema version does not match the pinned runtime: {requested}")
        return None

    def _validate_semantic_basics(self, spec: dict[str, Any], engine: str) -> None:
        if engine == "vega-lite":
            def validate_unit(value: dict[str, Any], description: str) -> None:
                mark = value.get("mark")
                if mark is not None:
                    mark_type = mark if isinstance(mark, str) else mark.get("type") if isinstance(mark, dict) else None
                    if not isinstance(mark_type, str) or mark_type not in VEGA_LITE_MARKS:
                        raise ValidationError(f"{description} has an invalid Vega-Lite mark")
                layers = value.get("layer")
                if layers is not None:
                    if not isinstance(layers, list) or not layers or not all(isinstance(item, dict) for item in layers):
                        raise ValidationError(f"{description} layer must be a non-empty list of objects")
                    for index, layer in enumerate(layers):
                        validate_unit(layer, f"{description} layer {index}")
                for key in ("hconcat", "vconcat", "concat"):
                    children = value.get(key)
                    if children is not None:
                        if not isinstance(children, list) or not children or not all(isinstance(item, dict) for item in children):
                            raise ValidationError(f"{description} {key} must be a non-empty list of objects")
                        for index, child in enumerate(children):
                            validate_unit(child, f"{description} {key} {index}")
                nested = value.get("spec")
                if nested is not None:
                    if not isinstance(nested, dict):
                        raise ValidationError(f"{description} spec must be an object")
                    validate_unit(nested, f"{description} spec")
                if not any(key in value for key in ("mark", "layer", "hconcat", "vconcat", "concat", "facet", "repeat", "spec")):
                    raise ValidationError(f"{description} does not contain a Vega-Lite view definition")

            validate_unit(spec, "visualization")
            return

        marks = spec.get("marks", [])
        if not isinstance(marks, list):
            raise ValidationError("raw Vega marks must be a list")

        def validate_marks(items: list[Any], description: str) -> None:
            for index, mark in enumerate(items):
                if not isinstance(mark, dict) or mark.get("type") not in VEGA_MARKS:
                    raise ValidationError(f"{description} mark {index} has an invalid raw Vega mark type")
                nested = mark.get("marks")
                if nested is not None:
                    if not isinstance(nested, list):
                        raise ValidationError(f"{description} group mark {index} marks must be a list")
                    validate_marks(nested, f"{description} group mark {index}")

        validate_marks(marks, "visualization")

    def _read_spec(
        self,
        source_relative: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, dict[str, Any]]:
        raw = self._read_project_bytes(
            source_relative,
            description="visualization source",
            max_bytes=max_bytes,
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("visualization source must be UTF-8 JSON") from exc
        value = _load_json_text(text, source_relative)
        if not isinstance(value, dict):
            raise ValidationError("visualization source must contain a JSON object")
        return raw, value

    def _prepare_data_urls(
        self,
        spec: dict[str, Any],
        *,
        source: str | None,
        inline: bool,
        max_dependency_bytes: int,
        max_prepared_spec_bytes: int = 0,
        base_spec_bytes: int = 0,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        prepared = copy.deepcopy(spec)
        dependency_paths: dict[str, dict[str, Any]] = {}
        dependency_bytes = 0
        expanded_data_bytes = 0

        if source is None and not inline:
            raise ValidationError("file rendering requires a source path")

        def rewrite_data(data: Any) -> None:
            nonlocal dependency_bytes, expanded_data_bytes
            if isinstance(data, list):
                for item in data:
                    rewrite_data(item)
                return
            if not isinstance(data, dict):
                return
            if "url" in data:
                raw_url = data["url"]
                if inline:
                    raise PolicyError("inline rendering permits inline values only; data.url is forbidden")
                if not isinstance(raw_url, str) or not raw_url.strip():
                    raise PolicyError("data.url must be a non-empty static string")
                parsed = urllib.parse.urlsplit(raw_url)
                scheme = parsed.scheme.lower()
                if scheme in {"http", "https"} or parsed.netloc:
                    raise PolicyError(f"remote data URL is forbidden: {raw_url}")
                if scheme:
                    raise PolicyError(f"unsupported data URL scheme '{scheme}': {raw_url}")
                if parsed.query or parsed.fragment:
                    raise PolicyError(f"local data URL cannot contain a query or fragment: {raw_url}")
                decoded = urllib.parse.unquote(parsed.path)
                if not decoded or pathlib.PurePosixPath(decoded).is_absolute() or "\\" in decoded:
                    raise PolicyError(f"local data URL must be project-relative: {raw_url}")
                if source is None:
                    raise ValidationError("local data resolution requires a source path")
                local_relative = self._dependency_project_relative(
                    raw_url,
                    description="local data URL",
                )
                snapshot = dependency_paths.get(local_relative)
                if snapshot is None:
                    remaining = max_dependency_bytes - dependency_bytes
                    if remaining < 0:
                        raise ValidationError(f"local data dependencies exceed {max_dependency_bytes} bytes")
                    local_bytes = self._read_project_bytes(
                        local_relative,
                        description="local data file",
                        max_bytes=remaining,
                    )
                    dependency_bytes += len(local_bytes)
                    try:
                        local_text = local_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValidationError(f"local data file must be UTF-8 text: {local_relative}") from exc
                    snapshot = {
                        "path": local_relative,
                        "bytes": len(local_bytes),
                        "sha256": _sha256_bytes(local_bytes),
                        "_text": local_text,
                        "_json_bytes": (
                            len(local_bytes)
                            + 2
                            + local_bytes.count(b'"')
                            + local_bytes.count(b"\\")
                            + sum(local_bytes.count(bytes([value])) for value in (8, 9, 10, 12, 13))
                            + 5 * sum(
                                local_bytes.count(bytes([value]))
                                for value in range(32)
                                if value not in {8, 9, 10, 12, 13}
                            )
                        ),
                        "kind": "data",
                    }
                    dependency_paths[local_relative] = snapshot
                local_text = snapshot["_text"]
                expanded_data_bytes += int(snapshot["_json_bytes"])
                if max_prepared_spec_bytes and base_spec_bytes + expanded_data_bytes > max_prepared_spec_bytes:
                    raise ValidationError(
                        f"prepared visualization exceeds {max_prepared_spec_bytes} bytes after local data inlining"
                    )
                data_format = data.get("format")
                if data_format is not None and not isinstance(data_format, dict):
                    raise ValidationError(f"local data format must be an object: {local_relative}")
                if data_format is None or "type" not in data_format:
                    suffix = pathlib.PurePosixPath(local_relative).suffix.lower()
                    inferred_formats = {
                        ".csv": "csv",
                        ".tsv": "tsv",
                        ".json": "json",
                        ".geojson": "json",
                    }
                    inferred = inferred_formats.get(suffix)
                    if inferred is None:
                        raise ValidationError(
                            f"local data format cannot be inferred for {local_relative}; declare data.format.type"
                        )
                    data["format"] = {**(data_format or {}), "type": inferred}
                elif not isinstance(data_format.get("type"), str):
                    raise ValidationError(f"local data format must contain a string type: {local_relative}")
                data.pop("url")
                data["values"] = local_text
            for key, child in list(data.items()):
                if key not in {"url", "values"}:
                    walk(child)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in list(value.items()):
                    if key == "data":
                        rewrite_data(child)
                    elif key in {"url", "href"}:
                        scope = "inline rendering" if inline else "file rendering"
                        raise PolicyError(f"{scope} forbids image, hyperlink, and dynamic URL dependencies")
                    else:
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(prepared)
        if inline:
            return prepared, []
        dependencies: list[dict[str, Any]] = []
        total = 0
        for _, snapshot in sorted(dependency_paths.items(), key=lambda item: item[1]["path"]):
            total += int(snapshot["bytes"])
            if total > max_dependency_bytes:
                raise ValidationError(f"local data dependencies exceed {max_dependency_bytes} bytes")
            dependencies.append({key: value for key, value in snapshot.items() if not key.startswith("_")})
        return prepared, dependencies

    def _add_explicit_inputs(
        self,
        dependencies: list[dict[str, Any]],
        inputs: Iterable[str],
        *,
        max_dependency_bytes: int,
    ) -> list[dict[str, Any]]:
        by_path = {item["path"]: item for item in dependencies}
        total = sum(int(item["bytes"]) for item in dependencies)
        for raw in inputs:
            relative = self._dependency_project_relative(raw, description="visualization input")
            if relative not in by_path:
                remaining = max_dependency_bytes - total
                if remaining < 0:
                    raise ValidationError(f"visualization dependencies exceed {max_dependency_bytes} bytes")
                content = self._read_project_bytes(
                    relative,
                    description="visualization input",
                    max_bytes=remaining,
                )
                total += len(content)
                by_path[relative] = {
                    "path": relative,
                    "bytes": len(content),
                    "sha256": _sha256_bytes(content),
                    "kind": "input",
                }
        result = [by_path[key] for key in sorted(by_path)]
        if sum(int(item["bytes"]) for item in result) > max_dependency_bytes:
            raise ValidationError(f"visualization dependencies exceed {max_dependency_bytes} bytes")
        return result

    def _validate_file(
        self,
        source_path: str,
        *,
        engine: str,
        profile: str,
        family: str,
        inputs: Iterable[str] = (),
    ) -> dict[str, Any]:
        profile_name, profile_data, profile_path = self._load_profile(profile)
        family_name, _, theme_path, _ = self._load_theme(family)
        limits = profile_data["runtime_limits"]
        source_relative = self._project_write_relative(source_path, description="visualization source")
        if not source_relative.lower().endswith(".json"):
            raise ValidationError("visualization source must be JSON (.vl.json or .vg.json)")
        raw, spec = self._read_spec(source_relative, max_bytes=int(limits["max_source_bytes"]))
        resolved_engine = self._normalize_engine(
            engine,
            source_name=pathlib.PurePosixPath(source_relative).name,
            spec=spec,
        )
        vega_lite_version = self._select_vega_lite_version(spec, resolved_engine, profile_data)
        self._validate_semantic_basics(spec, resolved_engine)
        prepared, dependencies = self._prepare_data_urls(
            spec,
            source=source_relative,
            inline=False,
            max_dependency_bytes=int(limits["max_dependency_bytes"]),
            max_prepared_spec_bytes=int(limits["max_prepared_spec_bytes"]),
            base_spec_bytes=len(raw),
        )
        dependencies = self._add_explicit_inputs(
            dependencies,
            inputs,
            max_dependency_bytes=int(limits["max_dependency_bytes"]),
        )
        prepared_bytes = _json_bytes(prepared)
        if len(prepared_bytes) > int(limits["max_prepared_spec_bytes"]):
            raise ValidationError(
                f"prepared visualization exceeds {limits['max_prepared_spec_bytes']} bytes after local data inlining"
            )
        source_hash = _sha256_bytes(raw)
        return {
            "ok": True,
            "source": source_relative,
            "source_sha256": source_hash,
            "source_bytes": len(raw),
            "engine": resolved_engine,
            "vega_lite_version": vega_lite_version,
            "family": family_name,
            "profile": profile_name,
            "dependencies": dependencies,
            "_prepared_spec_bytes": prepared_bytes,
            "_profile_data": profile_data,
            "_profile_path": profile_path,
            "_theme_path": theme_path,
        }

    def _public_validation(self, validation: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (
                [{child_key: child_value for child_key, child_value in item.items() if not child_key.startswith("_")} for item in value]
                if key == "dependencies"
                else value
            )
            for key, value in validation.items()
            if not key.startswith("_")
        }

    def validate_visualization(
        self,
        source_path: str,
        *,
        engine: str = "auto",
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
        inputs: Iterable[str] = (),
    ) -> dict[str, Any]:
        validation = self._validate_file(
            source_path,
            engine=engine,
            profile=profile,
            family=family,
            inputs=inputs,
        )
        renderer = self._inspect_renderer(validation["profile"], validation["_profile_data"], validation["_profile_path"])
        fingerprint = self._fingerprint(
            validation,
            output_format="svg",
        )
        return {
            **self._public_validation(validation),
            "fingerprint": fingerprint,
            "renderer": self._public_renderer(renderer),
        }

    def _fingerprint(
        self,
        validation: dict[str, Any],
        *,
        output_format: str,
    ) -> str:
        dependencies = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "kind": item["kind"],
            }
            for item in validation["dependencies"]
        ]
        values = {
            "contract": 1,
            "source": validation["source"],
            "source_sha256": validation["source_sha256"],
            "engine": validation["engine"],
            "vega_lite_version": validation["vega_lite_version"],
            "format": output_format,
            "family": validation["family"],
            "theme_sha256": _sha256_file(validation["_theme_path"]),
            "profile": validation["profile"],
            "profile_sha256": _sha256_file(validation["_profile_path"]),
            "renderer_contract": self._renderer_contract(validation["_profile_path"]),
            "registry_contract": _sha256_file(pathlib.Path(__file__).resolve()),
            "dependencies": dependencies,
        }
        return _sha256_bytes(_json_bytes(values))

    def _resolve_format(self, output: pathlib.Path | None, requested: str | None) -> str:
        suffix = output.suffix.lower().lstrip(".") if output else ""
        output_format = (requested or suffix or "svg").strip().lower()
        if output_format not in OUTPUT_MIME_TYPES:
            raise ValidationError("output format must be svg, png, or pdf")
        if output and suffix != output_format:
            raise ValidationError(
                f"output suffix .{suffix or '<missing>'} does not match requested format {output_format}"
            )
        return output_format

    def _renderer_contract(self, profile_path: pathlib.Path) -> str:
        paths = [
            profile_path,
            self.assets / "Dockerfile",
            self.assets / "docker" / "worker.py",
            *sorted((self.assets / "themes").glob("*.json")),
        ]
        digest = hashlib.sha256()
        for path in paths:
            if not path.is_file():
                raise ValidationError(f"renderer contract asset is missing: {path.name}")
            digest.update(path.relative_to(self.assets).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _public_renderer(self, renderer: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in renderer.items()
            if key in {"ok", "available", "profile", "image", "image_id", "base_image", "renderer_contract", "built"}
        }

    def _lock_renderer(self, renderer: dict[str, Any]) -> dict[str, str]:
        image_id = renderer.get("image_id")
        if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
            raise RenderError("renderer does not have a verified immutable image ID")
        return {
            "image": str(renderer["image"]),
            "image_id": image_id,
            "base_image": str(renderer["base_image"]),
            "renderer_contract": str(renderer["renderer_contract"]),
        }

    @contextmanager
    def _renderer_build_lock(self, image: str) -> Iterable[None]:
        directory_descriptor = _open_renderer_lock_directory()
        descriptor = -1
        locked = False
        try:
            try:
                descriptor = os.open(
                    _renderer_lock_name(image),
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                lock_status = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(lock_status.st_mode)
                    or lock_status.st_uid != os.geteuid()
                    or stat.S_IMODE(lock_status.st_mode) & 0o077
                    or lock_status.st_nlink != 1
                ):
                    raise RenderError("renderer build lock is not a private regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise RenderError(f"cannot open renderer build lock: {exc}") from exc
            locked = True
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                os.close(directory_descriptor)

    def _inspect_renderer(
        self,
        profile_name: str,
        profile_data: dict[str, Any],
        profile_path: pathlib.Path,
    ) -> dict[str, Any]:
        renderer_contract = self._renderer_contract(profile_path)
        inspect = self._runner(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{.Id}}}}\t{{{{ index .Config.Labels "{RENDERER_CONTRACT_LABEL}" }}}}',
                str(profile_data["image"]),
            ],
            cwd=self.project_root,
            timeout=60,
        )
        image_id: str | None = None
        label: str | None = None
        if inspect.get("returncode") == 0:
            fields = str(inspect.get("stdout") or "").strip().split("\t", 1)
            if len(fields) == 2:
                image_id, label = fields
        available = bool(image_id and IMAGE_ID_RE.fullmatch(image_id) and label == renderer_contract)
        return {
            "ok": available,
            "available": available,
            "profile": profile_name,
            "image": profile_data["image"],
            "image_id": image_id if available else None,
            "base_image": profile_data["base_image"],
            "renderer_contract": renderer_contract,
            "inspect": inspect,
        }

    def _build_renderer(
        self,
        profile_name: str,
        profile_data: dict[str, Any],
        profile_path: pathlib.Path,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        dockerfile = self.assets / str(profile_data["dockerfile"])
        if not dockerfile.is_file():
            raise ValidationError(f"packaged renderer Dockerfile is missing: {dockerfile.name}")
        renderer_contract = self._renderer_contract(profile_path)
        command = [
            "docker",
            "build",
            "--build-arg",
            f"VL_CONVERT_VERSION={profile_data['vl_convert_python']}",
            "--build-arg",
            f"BASE_IMAGE={profile_data['base_image']}",
            "--build-arg",
            f"QPDF_VERSION={profile_data['pdf_normalizer']['version']}",
            "--label",
            CONTAINER_LABEL,
            "--label",
            f"{RENDERER_CONTRACT_LABEL}={renderer_contract}",
            "-f",
            str(dockerfile),
            "-t",
            str(profile_data["image"]),
            str(self.assets),
        ]
        payload: dict[str, Any] = {
            "ok": True,
            "profile": profile_name,
            "image": profile_data["image"],
            "dockerfile": str(dockerfile),
            "context": str(self.assets),
            "renderer_contract": renderer_contract,
            "command": command,
            "dry_run": dry_run,
        }
        if dry_run:
            return payload
        result = self._runner(command, cwd=self.assets, timeout=1800)
        payload["result"] = result
        payload["ok"] = result.get("returncode") == 0
        return payload

    def build_renderer(self, profile: str = DEFAULT_PROFILE, *, dry_run: bool = False) -> dict[str, Any]:
        profile_name, profile_data, profile_path = self._load_profile(profile)
        if dry_run:
            return self._build_renderer(profile_name, profile_data, profile_path, dry_run=True)
        with self._renderer_build_lock(str(profile_data["image"])):
            return self._build_renderer(profile_name, profile_data, profile_path, dry_run=False)

    def ensure_renderer(self, profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
        profile_name, profile_data, profile_path = self._load_profile(profile)
        with self._renderer_build_lock(str(profile_data["image"])):
            inspected = self._inspect_renderer(profile_name, profile_data, profile_path)
            if inspected["available"]:
                return {**inspected, "built": False}
            build = self._build_renderer(profile_name, profile_data, profile_path, dry_run=False)
            if not build.get("ok"):
                return {
                    **inspected,
                    "ok": False,
                    "built": True,
                    "build": build,
                }
            rebuilt = self._inspect_renderer(profile_name, profile_data, profile_path)
            return {
                **rebuilt,
                "ok": bool(rebuilt.get("available")),
                "built": True,
                "build": build,
            }

    def _container_user(self) -> tuple[int, int]:
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        if uid == 0:
            return 65534, 65534
        return uid, gid

    def _docker_command(
        self,
        *,
        staging: pathlib.Path,
        staged_source_name: str,
        output_name: str,
        container_name: str,
        renderer_image: str,
        engine: str,
        output_format: str,
        family: str,
        vega_lite_version: str | None,
        profile_data: dict[str, Any],
    ) -> list[str]:
        uid, gid = self._container_user()
        limits = profile_data["runtime_limits"]
        project_id = workspace_id(self.project_root)
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--pull",
            "never",
            "--label",
            CONTAINER_LABEL,
            "--label",
            f"{CONTAINER_WORKSPACE_LABEL}={project_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{uid}:{gid}",
            "--cpus",
            str(limits["cpus"]),
            "--memory",
            str(limits["memory"]),
            "--memory-swap",
            str(limits["memory"]),
            "--pids-limit",
            str(limits["pids"]),
            "--ulimit",
            "nofile=256:256",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits['tmpfs']},uid={uid},gid={gid},mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "XDG_CACHE_HOME=/tmp/.cache",
            "--mount",
            _docker_mount_spec("type=bind", f"source={staging}", "target=/output"),
            "--workdir",
            "/output",
            renderer_image,
            "--input",
            f"/output/{staged_source_name}",
            "--output",
            f"/output/{output_name}",
            "--engine",
            engine,
            "--format",
            output_format,
            "--theme",
            f"/opt/vegavisuals/themes/{family}.json",
            "--vl-version",
            str(vega_lite_version or profile_data["vega_lite"]["default"]),
            "--expected-vl-convert",
            str(profile_data["vl_convert_python"]),
            "--expected-vega",
            str(profile_data["vega_runtime"]),
            "--max-output-bytes",
            str(profile_data["runtime_limits"]["max_output_bytes"]),
        ]

    def _validate_artifact(
        self,
        staging_fd: int,
        output_name: str,
        output_format: str,
        *,
        max_bytes: int,
    ) -> tuple[dict[str, Any], bytes]:
        try:
            snapshot, data = self._read_regular_at(
                staging_fd,
                output_name,
                description="staged renderer output",
                max_bytes=max_bytes,
                collect=True,
            )
        except ValidationError as exc:
            raise RenderError(f"renderer did not create a safe regular staged output: {exc}") from exc
        assert data is not None
        if snapshot.size <= 0:
            raise RenderError("renderer created an empty output")
        return self._validate_artifact_data(data, output_format, max_bytes=max_bytes), data

    def _validate_artifact_data(self, data: bytes, output_format: str, *, max_bytes: int) -> dict[str, Any]:
        if not data:
            raise RenderError("renderer created an empty output")
        if len(data) > max_bytes:
            raise RenderError(f"renderer output exceeds {max_bytes} bytes")
        if output_format == "png":
            self._validate_png(data)
        elif output_format == "pdf":
            self._validate_pdf(data)
        else:
            self._validate_svg(data)
        return {
            "ok": True,
            "format": output_format,
            "bytes": len(data),
            "sha256": _sha256_bytes(data),
        }

    def _validate_png(self, data: bytes) -> None:
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RenderError("renderer output does not have a PNG signature")
        offset = 8
        seen_ihdr = False
        seen_idat = False
        seen_iend = False
        while offset < len(data):
            if offset + 12 > len(data):
                raise RenderError("renderer output has a truncated PNG chunk")
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            chunk_type = data[offset + 4 : offset + 8]
            chunk_end = offset + 12 + length
            if chunk_end > len(data):
                raise RenderError("renderer output has a truncated PNG chunk payload")
            payload = data[offset + 8 : offset + 8 + length]
            expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
            actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                raise RenderError(f"renderer output PNG chunk has an invalid CRC: {chunk_type!r}")
            if not seen_ihdr:
                if chunk_type != b"IHDR" or length != 13:
                    raise RenderError("renderer output PNG must begin with a 13-byte IHDR chunk")
                width, height = struct.unpack(">II", payload[:8])
                if width == 0 or height == 0:
                    raise RenderError("renderer output PNG has invalid dimensions")
                seen_ihdr = True
            elif chunk_type == b"IHDR":
                raise RenderError("renderer output PNG contains multiple IHDR chunks")
            if chunk_type == b"IDAT":
                seen_idat = True
            if chunk_type == b"IEND":
                if length != 0 or chunk_end != len(data):
                    raise RenderError("renderer output PNG has an invalid IEND chunk")
                seen_iend = True
                break
            offset = chunk_end
        if not (seen_ihdr and seen_idat and seen_iend):
            raise RenderError("renderer output PNG is missing required chunks")

    def _validate_pdf(self, data: bytes) -> None:
        if not re.match(rb"^%PDF-[12]\.[0-9]\r?\n", data):
            raise RenderError("renderer output does not have a valid PDF header")
        eof = re.search(rb"%%EOF[\x00\x09\x0a\x0c\x0d\x20]*$", data)
        if eof is None:
            raise RenderError("renderer output PDF does not end at an EOF marker")
        startxref_matches = list(re.finditer(rb"startxref\s+([0-9]+)\s+%%EOF", data[-4096:]))
        if not startxref_matches:
            raise RenderError("renderer output PDF has no final startxref")
        xref_offset = int(startxref_matches[-1].group(1))
        if xref_offset >= len(data) or not data[xref_offset : xref_offset + 4] == b"xref":
            raise RenderError("renderer output PDF startxref does not point to a classic xref table")
        if b"trailer" not in data[xref_offset:] or b"/Root" not in data[xref_offset:] or b"/Type /Catalog" not in data:
            raise RenderError("renderer output PDF is missing its catalog or trailer")
        if not re.search(rb"\b[0-9]+\s+[0-9]+\s+obj\b", data) or b"endobj" not in data:
            raise RenderError("renderer output PDF has no complete indirect objects")

    def _validate_svg(self, data: bytes) -> None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise RenderError(f"renderer output is not valid SVG XML: {exc}") from exc
        if root.tag.rsplit("}", 1)[-1].lower() != "svg":
            raise RenderError("renderer output XML root is not svg")
        url_pattern = re.compile(r"url\(\s*['\"]?([^)'\"]+)", re.IGNORECASE)
        import_pattern = re.compile(r"@import\b", re.IGNORECASE)
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""
            if tag in {"script", "foreignobject"}:
                raise RenderError(f"renderer SVG contains forbidden element: {tag}")
            for raw_name, raw_value in element.attrib.items():
                name = raw_name.rsplit("}", 1)[-1].lower()
                value = raw_value.strip()
                if name.startswith("on"):
                    raise RenderError(f"renderer SVG contains an event handler attribute: {name}")
                if import_pattern.search(value):
                    raise RenderError(f"renderer SVG contains a forbidden CSS import in attribute: {name}")
                if name in {"href", "src"} and value and not value.startswith("#"):
                    raise RenderError(f"renderer SVG contains an external reference: {value}")
                for match in url_pattern.finditer(value):
                    reference = match.group(1).strip()
                    if not reference.startswith("#"):
                        raise RenderError(f"renderer SVG contains an external URL reference: {reference}")
            if tag == "style" and element.text:
                if import_pattern.search(element.text):
                    raise RenderError("renderer SVG style contains a forbidden CSS import")
                for match in url_pattern.finditer(element.text):
                    reference = match.group(1).strip()
                    if not reference.startswith("#"):
                        raise RenderError(f"renderer SVG style contains an external URL reference: {reference}")

    def _artifact_payload(
        self,
        output_relative: str,
        output_format: str,
        *,
        include_data: bool,
        response_limit: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        data = self._read_project_bytes(
            output_relative,
            description="managed visualization artifact",
            max_bytes=max_bytes,
        )
        payload: dict[str, Any] = {
            "path": output_relative,
            "format": output_format,
            "mime_type": OUTPUT_MIME_TYPES[output_format],
            "bytes": len(data),
            "sha256": _sha256_bytes(data),
            "data_included": False,
        }
        if not include_data:
            return payload
        if output_format == "svg":
            if len(data) > response_limit:
                payload["data_omitted"] = f"SVG exceeds inline response limit of {response_limit} bytes"
                return payload
            payload["svg"] = data.decode("utf-8")
        else:
            encoded = base64.b64encode(data)
            if len(encoded) > response_limit:
                payload["data_omitted"] = f"base64 artifact exceeds inline response limit of {response_limit} bytes"
                return payload
            payload["data_base64"] = encoded.decode("ascii")
        payload["data_included"] = True
        return payload

    def _verify_dependencies(self, validation: dict[str, Any]) -> None:
        limits = validation["_profile_data"]["runtime_limits"]
        source_snapshot = self._project_file_hash(
            validation["source"],
            max_bytes=int(limits["max_source_bytes"]),
            description="visualization source",
        )
        if source_snapshot is None or source_snapshot[0] != validation["source_sha256"]:
            raise RenderError(f"visualization source changed while rendering: {validation['source']}")
        for item in validation["dependencies"]:
            snapshot = self._project_file_hash(
                item["path"],
                max_bytes=int(limits["max_dependency_bytes"]),
                description="visualization dependency",
            )
            if snapshot is None or snapshot[0] != item["sha256"]:
                raise RenderError(f"input changed while rendering: {item['path']}")

    def _load_lock_snapshot(self) -> tuple[dict[str, Any], _FileSnapshot]:
        try:
            result = self._read_project_snapshot(
                LOCK_NAME,
                description="visualization lock",
                max_bytes=MAX_LOCK_BYTES,
                collect=True,
                missing_ok=True,
            )
        except PolicyError:
            raise
        except ValidationError as exc:
            raise ManifestError(str(exc)) from exc
        if result is None:
            return {"version": LOCK_VERSION, "visualizations": {}}, _FileSnapshot(exists=False)
        snapshot, raw = result
        assert raw is not None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(f"{LOCK_NAME} must be UTF-8 JSON") from exc
        try:
            value = _load_json_text(text, LOCK_NAME)
        except ValidationError as exc:
            raise ManifestError(str(exc)) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "visualizations"}
            or type(value.get("version")) is not int
            or value.get("version") != LOCK_VERSION
        ):
            raise ManifestError(f"{LOCK_NAME} must use lock version {LOCK_VERSION}")
        entries = value.get("visualizations")
        if not isinstance(entries, dict):
            raise ManifestError(f"{LOCK_NAME} visualizations must be an object")
        outputs: set[str] = set()
        required_fields = {
            "source",
            "output",
            "engine",
            "format",
            "profile",
            "family",
            "vega_lite_version",
            "fingerprint",
            "output_sha256",
            "inputs",
            "renderer",
        }

        def lock_relative(raw_path: str, description: str) -> str:
            try:
                return self._project_write_relative(raw_path, description=description)
            except (ValidationError, ValueError, OSError) as exc:
                raise ManifestError(f"{LOCK_NAME} contains an invalid path: {description}") from exc

        for name, entry in entries.items():
            if (
                not isinstance(name, str)
                or not SAFE_VISUALIZATION_NAME.fullmatch(name)
                or not isinstance(entry, dict)
                or set(entry) != required_fields
            ):
                raise ManifestError(f"{LOCK_NAME} contains an invalid visualization entry")
            for field in ("source", "output", "profile", "family"):
                if not isinstance(entry.get(field), str) or not entry[field]:
                    raise ManifestError(f"lock entry {name} has an invalid {field}")
            output = lock_relative(entry["output"], f"lock output for {name}")
            if output != entry["output"] or output in outputs:
                raise ManifestError(f"lock entry {name} has a duplicate or non-normalized output")
            outputs.add(output)
            source = entry["source"]
            if source.startswith("inline:"):
                if not SHA256_RE.fullmatch(source.removeprefix("inline:")):
                    raise ManifestError(f"lock entry {name} has an invalid inline source identity")
            else:
                normalized_source = lock_relative(source, f"lock source for {name}")
                if normalized_source != source:
                    raise ManifestError(f"lock entry {name} has a non-normalized source")
            if entry.get("engine") not in {"vega-lite", "vega"}:
                raise ManifestError(f"lock entry {name} has an invalid engine")
            if entry.get("format") not in OUTPUT_MIME_TYPES:
                raise ManifestError(f"lock entry {name} has an invalid format")
            version = entry.get("vega_lite_version")
            if entry["engine"] == "vega-lite":
                if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
                    raise ManifestError(f"lock entry {name} has an invalid Vega-Lite version")
            elif version is not None:
                raise ManifestError(f"raw Vega lock entry {name} must not declare a Vega-Lite version")
            for field in ("fingerprint", "output_sha256"):
                if not isinstance(entry.get(field), str) or not SHA256_RE.fullmatch(entry[field]):
                    raise ManifestError(f"lock entry {name} has an invalid {field}")
            inputs = entry.get("inputs")
            if (
                not isinstance(inputs, list)
                or not all(isinstance(input_path, str) and input_path for input_path in inputs)
                or len(inputs) != len(set(inputs))
            ):
                raise ManifestError(f"lock entry {name} has invalid inputs")
            for input_path in inputs:
                if lock_relative(input_path, f"lock input for {name}") != input_path:
                    raise ManifestError(f"lock entry {name} has a non-normalized input")
            renderer = entry.get("renderer")
            if (
                not isinstance(renderer, dict)
                or set(renderer) != {"image", "image_id", "base_image", "renderer_contract"}
                or not isinstance(renderer.get("image"), str)
                or not IMAGE_ID_RE.fullmatch(str(renderer.get("image_id") or ""))
                or not isinstance(renderer.get("base_image"), str)
                or not SHA256_RE.fullmatch(str(renderer.get("renderer_contract") or ""))
            ):
                raise ManifestError(f"lock entry {name} has invalid renderer provenance")
            try:
                profile_name, _, _ = self._load_profile(entry["profile"])
                family_name, _, _, _ = self._load_theme(entry["family"])
            except ValidationError as exc:
                raise ManifestError(f"lock entry {name} references an invalid render contract: {exc}") from exc
            if profile_name != entry["profile"] or family_name != entry["family"]:
                raise ManifestError(f"lock entry {name} has non-normalized profile or family names")
        return value, snapshot

    def _load_lock(self) -> dict[str, Any]:
        return self._load_lock_snapshot()[0]

    def _write_lock(self, lock: dict[str, Any]) -> None:
        publication = self._replace_project_bytes(LOCK_NAME, self._lock_bytes(lock))
        publication.commit()

    def _lock_bytes(
        self,
        lock: dict[str, Any],
        *,
        max_bytes: int = MAX_LOCK_BYTES,
        description: str = "visualization lock",
    ) -> bytes:
        data = json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > max_bytes:
            raise ManifestError(f"{description} exceeds {max_bytes} bytes")
        return data

    def _publish_artifact_and_lock(
        self,
        output_relative: str,
        artifact_data: bytes,
        lock: dict[str, Any],
        *,
        expected_output: _FileSnapshot,
        expected_lock: _FileSnapshot,
        verify_inputs: Callable[[], None] | None = None,
    ) -> None:
        publications: list[_ProjectPublication] = []
        try:
            if verify_inputs is not None:
                verify_inputs()
            self._require_recovery_filesystem(output_relative)
            self._replace_project_bytes(
                output_relative,
                artifact_data,
                expected=expected_output,
                transaction_log=publications,
            )
            self._replace_project_bytes(
                LOCK_NAME,
                self._lock_bytes(lock),
                expected=expected_lock,
                transaction_log=publications,
            )
            if verify_inputs is not None:
                verify_inputs()
        except BaseException as publication_error:
            rollback_error: BaseException | None = None
            for publication in reversed(publications):
                try:
                    publication.rollback()
                except BaseException as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise rollback_error from publication_error
            raise
        commit_error: BaseException | None = None
        for publication in reversed(publications):
            try:
                publication.commit()
            except BaseException as exc:
                commit_error = commit_error or exc
        if commit_error is not None:
            raise commit_error

    def _publish_cache_pair(
        self,
        cache_relative: str,
        artifact_data: bytes,
        metadata_relative: str,
        metadata: dict[str, Any],
    ) -> None:
        publications: list[_ProjectPublication] = []
        try:
            self._replace_project_bytes(
                cache_relative,
                artifact_data,
                transaction_log=publications,
            )
            self._replace_project_bytes(
                metadata_relative,
                self._lock_bytes(metadata, max_bytes=65536, description="inline cache metadata"),
                transaction_log=publications,
            )
        except BaseException as publication_error:
            rollback_error: BaseException | None = None
            for publication in reversed(publications):
                try:
                    publication.rollback()
                except BaseException as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise rollback_error from publication_error
            raise
        commit_error: BaseException | None = None
        for publication in reversed(publications):
            try:
                publication.commit()
            except BaseException as exc:
                commit_error = commit_error or exc
        if commit_error is not None:
            raise commit_error

    def _lock_key(
        self,
        entries: dict[str, Any],
        output_relative: str,
        preferred: str | None,
    ) -> str:
        owners = [name for name, entry in entries.items() if entry.get("output") == output_relative]
        if len(owners) > 1:
            raise ManifestError(f"multiple lock entries manage output: {output_relative}")
        if preferred:
            if not SAFE_VISUALIZATION_NAME.fullmatch(preferred):
                raise ManifestError(f"invalid visualization lock name: {preferred}")
            if owners and owners[0] != preferred:
                raise ManifestError(f"output {output_relative} is already managed by lock entry {owners[0]}")
            return preferred
        if owners:
            return owners[0]
        digest = hashlib.sha256(output_relative.encode("utf-8")).hexdigest()[:16]
        return f"direct-{digest}"

    def _output_state(
        self,
        output_relative: str,
        *,
        fingerprint: str,
        entry: dict[str, Any] | None,
        max_bytes: int,
    ) -> tuple[str, _FileSnapshot]:
        snapshot = self._project_file_snapshot(
            output_relative,
            max_bytes=max_bytes,
            description="visualization output",
        )
        if not snapshot.exists:
            return "missing", snapshot
        actual_hash = snapshot.sha256
        assert actual_hash is not None
        if entry is None:
            return "unmanaged", snapshot
        if actual_hash != entry.get("output_sha256"):
            return "modified", snapshot
        if fingerprint == entry.get("fingerprint"):
            return "fresh", snapshot
        return "stale", snapshot

    def _dry_run_command(
        self,
        *,
        engine: str,
        output_format: str,
        family: str,
        vega_lite_version: str | None,
        renderer_image: str,
        profile_data: dict[str, Any],
    ) -> list[str]:
        return self._docker_command(
            staging=pathlib.Path("/tmp/vegavisuals-staging"),
            staged_source_name="spec.json",
            output_name=f"artifact.{output_format}",
            container_name="vegavisuals-dry-run",
            renderer_image=renderer_image,
            engine=engine,
            output_format=output_format,
            family=family,
            vega_lite_version=vega_lite_version,
            profile_data=profile_data,
        )

    def _execute_render(
        self,
        *,
        prepared_spec_bytes: bytes,
        engine: str,
        output_format: str,
        family: str,
        vega_lite_version: str | None,
        profile_data: dict[str, Any],
        renderer: dict[str, Any],
    ) -> dict[str, Any]:
        timeout = int(profile_data["runtime_limits"]["timeout_seconds"])
        max_output = int(profile_data["runtime_limits"]["max_output_bytes"])
        with tempfile.TemporaryDirectory(prefix="vegavisuals-") as temporary_directory:
            staging = pathlib.Path(temporary_directory).resolve()
            uid, gid = self._container_user()
            staged_source = staging / "spec.json"
            staged_source.write_bytes(prepared_spec_bytes)
            os.chmod(staged_source, 0o644)
            if hasattr(os, "getuid") and os.getuid() == 0 and uid != 0:
                os.chown(staging, uid, gid)
                os.chmod(staging, 0o700)
            staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            staged_output = staging / f"artifact.{output_format}"
            container_name = f"vegavisuals-{os.getpid()}-{secrets.token_hex(8)}"
            command = self._docker_command(
                staging=staging,
                staged_source_name=staged_source.name,
                output_name=staged_output.name,
                container_name=container_name,
                renderer_image=str(renderer["image_id"]),
                engine=engine,
                output_format=output_format,
                family=family,
                vega_lite_version=vega_lite_version,
                profile_data=profile_data,
            )
            try:
                result = self._runner(command, cwd=self.project_root, timeout=timeout)
                if result.get("returncode") != 0:
                    cleanup = self._runner(
                        ["docker", "container", "rm", "--force", container_name],
                        cwd=self.project_root,
                        timeout=60,
                    )
                    message = str(result.get("stderr") or result.get("stdout") or "renderer failed").strip()
                    raise RenderError(
                        f"Docker renderer failed: {message}; cleanup returncode={cleanup.get('returncode')}"
                    )
                artifact_check, artifact_data = self._validate_artifact(
                    staging_fd,
                    staged_output.name,
                    output_format,
                    max_bytes=max_output,
                )
            finally:
                os.close(staging_fd)
            return {
                "command": command,
                "image": self._public_renderer(renderer),
                "result": result,
                "artifact_check": artifact_check,
                "_artifact_data": artifact_data,
            }

    def render_visualization(
        self,
        source_path: str,
        output_path: str,
        *,
        engine: str = "auto",
        output_format: str | None = None,
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
        inputs: Iterable[str] = (),
        include_data: bool = False,
        confirm_replace: bool = False,
        force: bool = False,
        dry_run: bool = False,
        lock_name: str | None = None,
    ) -> dict[str, Any]:
        output_relative = self._project_write_relative(output_path, description="visualization output")
        output = self.project_root / output_relative
        resolved_format = self._resolve_format(output, output_format)
        validation = self._validate_file(
            source_path,
            engine=engine,
            profile=profile,
            family=family,
            inputs=inputs,
        )
        if output_relative == validation["source"]:
            raise PolicyError("visualization output cannot replace its source")

        renderer = (
            self._inspect_renderer(validation["profile"], validation["_profile_data"], validation["_profile_path"])
            if dry_run
            else self.ensure_renderer(validation["profile"])
        )
        if not dry_run and not renderer.get("ok"):
            raise RenderError(f"renderer image is unavailable: {json_dumps(renderer)}")
        fingerprint = self._fingerprint(
            validation,
            output_format=resolved_format,
        )
        limits = validation["_profile_data"]["runtime_limits"]
        max_output = int(limits["max_output_bytes"])

        with self._project_lock():
            lock = self._load_lock()
            entries = lock["visualizations"]
            key = self._lock_key(entries, output_relative, lock_name)
            entry = entries.get(key)
            if entry is not None and entry.get("output") != output_relative:
                entry = None
            state, output_snapshot = self._output_state(
                output_relative,
                fingerprint=fingerprint,
                entry=entry,
                max_bytes=max_output,
            )
            if state in {"unmanaged", "modified"} and not confirm_replace:
                qualifier = "unmanaged" if state == "unmanaged" else "modified since its managed render"
                raise PolicyError(
                    f"refusing to replace {qualifier} output {output_relative}; pass explicit replacement confirmation"
                )
            state_token = (state, output_snapshot, copy.deepcopy(entry))

        base_payload: dict[str, Any] = {
            "ok": True,
            "project": str(self.project_root),
            "source": validation["source"],
            "output": output_relative,
            "engine": validation["engine"],
            "vega_lite_version": validation["vega_lite_version"],
            "format": resolved_format,
            "profile": validation["profile"],
            "family": validation["family"],
            "fingerprint": fingerprint,
            "state_before": state,
            "lock_entry": key,
            "dependencies": self._public_validation(validation)["dependencies"],
            "renderer": self._public_renderer(renderer),
        }
        if state == "fresh" and not force:
            with self._project_lock():
                fresh_lock = self._load_lock()
                fresh_entry = fresh_lock["visualizations"].get(key)
                fresh_state, fresh_snapshot = self._output_state(
                    output_relative,
                    fingerprint=fingerprint,
                    entry=fresh_entry,
                    max_bytes=max_output,
                )
                if (fresh_state, fresh_snapshot, fresh_entry) != state_token:
                    raise RenderError("fresh visualization output or lock changed while it was being read")
                self._verify_dependencies(validation)
                return {
                    **base_payload,
                    "rendered": False,
                    "skipped": True,
                    "state": "fresh",
                    "artifact": self._artifact_payload(
                        output_relative,
                        resolved_format,
                        include_data=include_data,
                        response_limit=int(limits["max_inline_response_bytes"]),
                        max_bytes=max_output,
                    ),
                }
        if dry_run:
            return {
                **base_payload,
                "rendered": False,
                "skipped": False,
                "dry_run": True,
                "action": "render",
                "command": self._dry_run_command(
                    engine=validation["engine"],
                    output_format=resolved_format,
                    family=validation["family"],
                    vega_lite_version=validation["vega_lite_version"],
                    renderer_image=str(renderer.get("image_id") or renderer["image"]),
                    profile_data=validation["_profile_data"],
                ),
            }

        execution = self._execute_render(
            prepared_spec_bytes=validation["_prepared_spec_bytes"],
            engine=validation["engine"],
            output_format=resolved_format,
            family=validation["family"],
            vega_lite_version=validation["vega_lite_version"],
            profile_data=validation["_profile_data"],
            renderer=renderer,
        )
        artifact_data = execution.pop("_artifact_data")
        artifact_hash = _sha256_bytes(artifact_data)
        with self._project_lock():
            lock, lock_snapshot = self._load_lock_snapshot()
            entries = lock["visualizations"]
            final_key = self._lock_key(entries, output_relative, lock_name)
            if final_key != key:
                raise RenderError("visualization lock ownership changed while rendering")
            final_entry = entries.get(key)
            if final_entry is not None and final_entry.get("output") != output_relative:
                final_entry = None
            final_state, final_snapshot = self._output_state(
                output_relative,
                fingerprint=fingerprint,
                entry=final_entry,
                max_bytes=max_output,
            )
            if (final_state, final_snapshot, final_entry) != state_token:
                raise RenderError("visualization output or lock changed while rendering; retry without overwriting it")
            entries[key] = {
                "source": validation["source"],
                "output": output_relative,
                "engine": validation["engine"],
                "format": resolved_format,
                "profile": validation["profile"],
                "family": validation["family"],
                "vega_lite_version": validation["vega_lite_version"],
                "fingerprint": fingerprint,
                "output_sha256": artifact_hash,
                "inputs": [item["path"] for item in self._public_validation(validation)["dependencies"]],
                "renderer": self._lock_renderer(renderer),
            }
            self._publish_artifact_and_lock(
                output_relative,
                artifact_data,
                lock,
                expected_output=final_snapshot,
                expected_lock=lock_snapshot,
                verify_inputs=lambda: self._verify_dependencies(validation),
            )
            artifact = self._artifact_payload(
                output_relative,
                resolved_format,
                include_data=include_data,
                response_limit=int(limits["max_inline_response_bytes"]),
                max_bytes=max_output,
            )
        return {
            **base_payload,
            **execution,
            "rendered": True,
            "skipped": False,
            "state": "fresh",
            "artifact": artifact,
        }

    def _inline_validation(
        self,
        visualization_text: str,
        *,
        engine: str,
        profile: str,
        family: str,
    ) -> dict[str, Any]:
        profile_name, profile_data, profile_path = self._load_profile(profile)
        family_name, _, theme_path, _ = self._load_theme(family)
        raw = visualization_text.encode("utf-8")
        maximum = int(profile_data["runtime_limits"]["max_inline_source_bytes"])
        if len(raw) > maximum:
            raise ValidationError(f"inline visualization exceeds {maximum} bytes")
        spec = _load_json_text(visualization_text, "inline visualization")
        if not isinstance(spec, dict):
            raise ValidationError("inline visualization must contain a JSON object")
        resolved_engine = self._normalize_engine(engine, spec=spec)
        vega_lite_version = self._select_vega_lite_version(spec, resolved_engine, profile_data)
        self._validate_semantic_basics(spec, resolved_engine)
        prepared, dependencies = self._prepare_data_urls(
            spec,
            source=None,
            inline=True,
            max_dependency_bytes=0,
        )
        prepared_bytes = _json_bytes(prepared)
        if len(prepared_bytes) > int(profile_data["runtime_limits"]["max_prepared_spec_bytes"]):
            raise ValidationError(
                f"prepared inline visualization exceeds {profile_data['runtime_limits']['max_prepared_spec_bytes']} bytes"
            )
        source_hash = _sha256_bytes(raw)
        return {
            "ok": True,
            "source": f"inline:{source_hash}",
            "source_sha256": source_hash,
            "source_bytes": len(raw),
            "engine": resolved_engine,
            "vega_lite_version": vega_lite_version,
            "family": family_name,
            "profile": profile_name,
            "dependencies": dependencies,
            "_prepared_spec_bytes": prepared_bytes,
            "_profile_data": profile_data,
            "_profile_path": profile_path,
            "_theme_path": theme_path,
        }

    def _cache_is_valid(
        self,
        output_relative: str,
        metadata_relative: str,
        *,
        fingerprint: str,
        output_format: str,
        max_bytes: int,
        renderer: dict[str, str],
    ) -> bool:
        try:
            if (
                self._project_file_hash(
                    output_relative,
                    max_bytes=max_bytes,
                    description="inline cache artifact",
                )
                is None
                or self._project_file_hash(
                    metadata_relative,
                    max_bytes=65536,
                    description="inline cache metadata",
                )
                is None
            ):
                return False
            artifact = self._read_project_bytes(
                output_relative,
                description="inline cache artifact",
                max_bytes=max_bytes,
            )
            metadata_bytes = self._read_project_bytes(
                metadata_relative,
                description="inline cache metadata",
                max_bytes=65536,
            )
            value = _load_json_text(metadata_bytes.decode("utf-8"), "inline cache metadata")
            check = self._validate_artifact_data(artifact, output_format, max_bytes=max_bytes)
        except (UnicodeDecodeError, ValidationError, RenderError):
            return False
        return bool(
            isinstance(value, dict)
            and set(value)
            == {
                "version",
                "fingerprint",
                "output",
                "output_sha256",
                "engine",
                "format",
                "profile",
                "family",
                "vega_lite_version",
                "renderer",
            }
            and type(value.get("version")) is int
            and value.get("version") == CACHE_VERSION
            and value.get("fingerprint") == fingerprint
            and value.get("output") == output_relative
            and value.get("output_sha256") == check["sha256"]
            and value.get("format") == output_format
            and value.get("renderer") == renderer
        )

    def render_visualization_text(
        self,
        visualization_text: str,
        *,
        output_path: str | None = None,
        engine: str = "auto",
        output_format: str | None = None,
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
        include_data: bool = False,
        confirm_replace: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        explicit_relative = (
            self._project_write_relative(output_path, description="visualization output") if output_path else None
        )
        explicit_output = self.project_root / explicit_relative if explicit_relative else None
        resolved_format = self._resolve_format(explicit_output, output_format)
        validation = self._inline_validation(
            visualization_text,
            engine=engine,
            profile=profile,
            family=family,
        )
        renderer = (
            self._inspect_renderer(validation["profile"], validation["_profile_data"], validation["_profile_path"])
            if dry_run
            else self.ensure_renderer(validation["profile"])
        )
        if not dry_run and not renderer.get("ok"):
            raise RenderError(f"renderer image is unavailable: {json_dumps(renderer)}")
        fingerprint = self._fingerprint(
            validation,
            output_format=resolved_format,
        )
        cache_relative = f".cache/vegavisuals/text/{fingerprint}.{resolved_format}"
        metadata_relative = f".cache/vegavisuals/text/{fingerprint}.json"
        cache_renderer = self._lock_renderer(renderer) if renderer.get("image_id") else {
            "image": str(renderer["image"]),
            "image_id": "",
            "base_image": str(renderer["base_image"]),
            "renderer_contract": str(renderer["renderer_contract"]),
        }
        limits = validation["_profile_data"]["runtime_limits"]
        max_output = int(limits["max_output_bytes"])

        key: str | None = None
        state = "cache"
        state_token: tuple[str, _FileSnapshot, dict[str, Any] | None] | None = None
        with self._project_lock():
            cached = self._cache_is_valid(
                cache_relative,
                metadata_relative,
                fingerprint=fingerprint,
                output_format=resolved_format,
                max_bytes=max_output,
                renderer=cache_renderer,
            )
            if explicit_relative is not None and explicit_relative != cache_relative:
                lock = self._load_lock()
                key = self._lock_key(lock["visualizations"], explicit_relative, None)
                entry = lock["visualizations"].get(key)
                state, output_snapshot = self._output_state(
                    explicit_relative,
                    fingerprint=fingerprint,
                    entry=entry,
                    max_bytes=max_output,
                )
                if state in {"unmanaged", "modified"} and not confirm_replace:
                    raise PolicyError(
                        f"refusing to replace {state} output {explicit_relative}; pass explicit replacement confirmation"
                    )
                state_token = (state, output_snapshot, copy.deepcopy(entry))
                if state == "fresh" and not force:
                    return {
                        "ok": True,
                        "inline": True,
                        "cached": cached,
                        "rendered": False,
                        "skipped": True,
                        "state": "fresh",
                        "engine": validation["engine"],
                        "vega_lite_version": validation["vega_lite_version"],
                        "format": resolved_format,
                        "profile": validation["profile"],
                        "family": validation["family"],
                        "fingerprint": fingerprint,
                        "renderer": self._public_renderer(renderer),
                        "artifact": self._artifact_payload(
                            explicit_relative,
                            resolved_format,
                            include_data=include_data,
                            response_limit=int(limits["max_inline_response_bytes"]),
                            max_bytes=max_output,
                        ),
                    }
        if dry_run:
            return {
                "ok": True,
                "inline": True,
                "cached": cached,
                "rendered": False,
                "skipped": cached and not force,
                "dry_run": True,
                "action": "cache-hit" if cached and not force else "render",
                "engine": validation["engine"],
                "vega_lite_version": validation["vega_lite_version"],
                "format": resolved_format,
                "profile": validation["profile"],
                "family": validation["family"],
                "fingerprint": fingerprint,
                "cache": cache_relative,
                "output": explicit_relative or cache_relative,
                "renderer": self._public_renderer(renderer),
                "command": self._dry_run_command(
                    engine=validation["engine"],
                    output_format=resolved_format,
                    family=validation["family"],
                    vega_lite_version=validation["vega_lite_version"],
                    renderer_image=str(renderer.get("image_id") or renderer["image"]),
                    profile_data=validation["_profile_data"],
                ),
            }

        execution: dict[str, Any] = {}
        rendered_data: bytes | None = None
        if not cached or force:
            execution = self._execute_render(
                prepared_spec_bytes=validation["_prepared_spec_bytes"],
                engine=validation["engine"],
                output_format=resolved_format,
                family=validation["family"],
                vega_lite_version=validation["vega_lite_version"],
                profile_data=validation["_profile_data"],
                renderer=renderer,
            )
            rendered_data = execution.pop("_artifact_data")

        with self._project_lock():
            cached_now = self._cache_is_valid(
                cache_relative,
                metadata_relative,
                fingerprint=fingerprint,
                output_format=resolved_format,
                max_bytes=max_output,
                renderer=cache_renderer,
            )
            if rendered_data is not None and (force or not cached_now):
                cache_hash = _sha256_bytes(rendered_data)
                self._publish_cache_pair(
                    cache_relative,
                    rendered_data,
                    metadata_relative,
                    {
                        "version": CACHE_VERSION,
                        "fingerprint": fingerprint,
                        "output": cache_relative,
                        "output_sha256": cache_hash,
                        "engine": validation["engine"],
                        "format": resolved_format,
                        "profile": validation["profile"],
                        "family": validation["family"],
                        "vega_lite_version": validation["vega_lite_version"],
                        "renderer": cache_renderer,
                    },
                )
            elif not cached_now:
                raise RenderError("inline cache changed while rendering; retry")
            cache_data = self._read_project_bytes(
                cache_relative,
                description="inline cache artifact",
                max_bytes=max_output,
            )

            final_relative = cache_relative
            if explicit_relative is not None and explicit_relative != cache_relative:
                if key is None or state_token is None:
                    raise RenderError("internal lock state is unavailable")
                lock, lock_snapshot = self._load_lock_snapshot()
                final_key = self._lock_key(lock["visualizations"], explicit_relative, None)
                if final_key != key:
                    raise RenderError("inline output lock ownership changed while rendering")
                entry = lock["visualizations"].get(key)
                final_state, final_snapshot = self._output_state(
                    explicit_relative,
                    fingerprint=fingerprint,
                    entry=entry,
                    max_bytes=max_output,
                )
                if (final_state, final_snapshot, entry) != state_token:
                    raise RenderError("inline output or lock changed while rendering; retry without overwriting it")
                lock["visualizations"][key] = {
                    "source": validation["source"],
                    "output": explicit_relative,
                    "engine": validation["engine"],
                    "format": resolved_format,
                    "profile": validation["profile"],
                    "family": validation["family"],
                    "vega_lite_version": validation["vega_lite_version"],
                    "fingerprint": fingerprint,
                    "output_sha256": _sha256_bytes(cache_data),
                    "inputs": [],
                    "renderer": self._lock_renderer(renderer),
                }
                self._publish_artifact_and_lock(
                    explicit_relative,
                    cache_data,
                    lock,
                    expected_output=final_snapshot,
                    expected_lock=lock_snapshot,
                )
                final_relative = explicit_relative
            artifact = self._artifact_payload(
                final_relative,
                resolved_format,
                include_data=include_data,
                response_limit=int(limits["max_inline_response_bytes"]),
                max_bytes=max_output,
            )

        return {
            "ok": True,
            "inline": True,
            "cached": cached and not force,
            "rendered": not cached or force,
            "skipped": cached and not force and explicit_output is None,
            "state_before": state,
            "state": "fresh",
            "engine": validation["engine"],
            "vega_lite_version": validation["vega_lite_version"],
            "format": resolved_format,
            "profile": validation["profile"],
            "family": validation["family"],
            "fingerprint": fingerprint,
            "cache": cache_relative,
            "renderer": self._public_renderer(renderer),
            **execution,
            "artifact": artifact,
        }

    def _load_manifest(self, manifest_path: str = MANIFEST_NAME) -> dict[str, Any]:
        relative = self._project_write_relative(manifest_path, description="visualization manifest")
        try:
            raw = self._read_project_bytes(
                relative,
                description="visualization manifest",
                max_bytes=1024 * 1024,
            )
            text = raw.decode("utf-8")
            for token in yaml.scan(text):
                if isinstance(token, yaml.tokens.AliasToken):
                    raise ManifestError("YAML aliases are not allowed in the visualization manifest")
            value = yaml.load(text, Loader=_UniqueKeyLoader)
        except ManifestError:
            raise
        except (OSError, RecursionError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ManifestError(f"invalid visualization manifest: {exc}") from exc
        if not isinstance(value, dict):
            raise ManifestError("visualization manifest must be an object")
        required_fields = {"version", "profile", "family", "visualizations"}
        if not required_fields <= set(value) or set(value) - {*required_fields, "inputs"}:
            raise ManifestError("visualization manifest has missing or unknown root fields")
        if type(value.get("version")) is not int or value.get("version") != 1:
            raise ManifestError("visualization manifest version must be 1")
        profile = value.get("profile")
        family = value.get("family")
        visualizations = value.get("visualizations")
        if not isinstance(profile, str) or not profile:
            raise ManifestError("visualization manifest requires a profile")
        if not isinstance(family, str) or not family:
            raise ManifestError("visualization manifest requires a family")
        _, profile_data, _ = self._load_profile(profile)
        self._load_theme(family)
        limits = profile_data["runtime_limits"]
        if not isinstance(visualizations, list):
            raise ManifestError("visualization manifest visualizations must be a list")

        def normalize_inputs(raw_inputs: Any, context: str) -> list[str]:
            if not isinstance(raw_inputs, list) or not all(isinstance(entry, str) and entry for entry in raw_inputs):
                raise ManifestError(f"{context} inputs must be a list of paths")
            try:
                normalized = [
                    self._dependency_project_relative(entry, description=f"input for {context}")
                    for entry in raw_inputs
                ]
            except ValidationError as exc:
                raise ManifestError(str(exc)) from exc
            if len(normalized) != len(set(normalized)):
                raise ManifestError(f"{context} has duplicate inputs")
            total = 0
            for entry in normalized:
                result = self._project_file_hash(
                    entry,
                    max_bytes=int(limits["max_dependency_bytes"]),
                    description=f"input for {context}",
                )
                if result is None:
                    raise ManifestError(f"{context} references a missing input")
                total += result[1]
                if total > int(limits["max_dependency_bytes"]):
                    raise ManifestError(f"{context} inputs exceed the dependency byte limit")
            return normalized

        manifest_inputs = normalize_inputs(value.get("inputs", []), "visualization manifest")

        normalized_items: list[dict[str, Any]] = []
        names: set[str] = set()
        sources: set[str] = set()
        outputs: set[str] = set()
        for index, item in enumerate(visualizations):
            if not isinstance(item, dict):
                raise ManifestError(f"visualization at index {index} must be an object")
            unknown_fields = set(item) - {"name", "source", "output", "engine", "format", "inputs"}
            if unknown_fields:
                raise ManifestError(
                    f"visualization at index {index} has unknown fields: {', '.join(sorted(unknown_fields))}"
                )
            name = item.get("name")
            source = item.get("source")
            output = item.get("output")
            if not isinstance(name, str) or not SAFE_VISUALIZATION_NAME.fullmatch(name):
                raise ManifestError(f"visualization at index {index} has an invalid name")
            if name in names:
                raise ManifestError(f"duplicate visualization name: {name}")
            if name.startswith("direct-"):
                raise ManifestError("manifest visualization names may not use the reserved direct- prefix")
            if not isinstance(source, str) or not source:
                raise ManifestError(f"visualization {name} requires a source")
            if not isinstance(output, str) or not output:
                raise ManifestError(f"visualization {name} requires an output")
            source_relative = self._manifest_project_relative(source, description=f"source for {name}")
            if self._project_file_hash(
                source_relative,
                max_bytes=int(limits["max_source_bytes"]),
                description=f"source for {name}",
            ) is None:
                raise ManifestError(f"source for {name} is not a file: {source}")
            output_relative = self._manifest_project_relative(output, description=f"output for {name}")
            output_resolved = self.project_root / output_relative
            if source_relative == output_relative:
                raise ManifestError(f"visualization {name} output cannot replace its source")
            if source_relative in sources:
                raise ManifestError(f"duplicate visualization source is ambiguous: {source_relative}")
            if output_relative in outputs:
                raise ManifestError(f"duplicate visualization output: {output_relative}")
            item_engine = item.get("engine", "auto")
            if item_engine not in {"auto", "vega-lite", "vega"}:
                raise ManifestError(f"visualization {name} engine must be auto, vega-lite, or vega")
            item_format = item.get("format")
            if item_format is not None and not isinstance(item_format, str):
                raise ManifestError(f"visualization {name} format must be a string")
            resolved_format = self._resolve_format(output_resolved, item_format)
            normalized_inputs = normalize_inputs(item.get("inputs", []), f"visualization {name}")
            names.add(name)
            sources.add(source_relative)
            outputs.add(output_relative)
            normalized_items.append(
                {
                    "name": name,
                    "source": source_relative,
                    "output": output_relative,
                    "engine": item_engine,
                    "format": resolved_format,
                    "inputs": sorted(set(manifest_inputs) | set(normalized_inputs)),
                }
            )
        return {
            "version": 1,
            "path": relative,
            "sha256": _sha256_bytes(raw),
            "profile": profile,
            "family": family,
            "inputs": manifest_inputs,
            "visualizations": normalized_items,
        }

    def _receipt_source_paths(self, manifest: dict[str, Any]) -> list[str]:
        sources = {
            manifest["path"],
            *(item["source"] for item in manifest["visualizations"]),
        }
        if len(sources) > MAX_RECEIPT_FILES + 1:
            raise ValidationError(f"receipt request exceeds {MAX_RECEIPT_FILES} Vega sources")
        try:
            assets_stat = os.stat("assets", dir_fd=self._project_root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return sorted(sources)
        if stat.S_ISLNK(assets_stat.st_mode):
            raise PolicyError("receipt source root must not be a symlink: assets")
        if not stat.S_ISDIR(assets_stat.st_mode):
            return sorted(sources)

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            assets_fd = os.open("assets", flags, dir_fd=self._project_root_fd)
        except OSError as exc:
            raise PolicyError("receipt source root cannot be opened safely: assets") from exc
        nodes = 0

        def walk(directory_fd: int, prefix: pathlib.PurePosixPath, depth: int) -> None:
            nonlocal nodes
            if depth > RECEIPT_JSON_MAX_DEPTH:
                raise ValidationError("receipt source discovery exceeds its directory depth limit")
            try:
                names = sorted(os.listdir(directory_fd))
            except OSError as exc:
                raise ValidationError(f"cannot list receipt source directory: {prefix}") from exc
            for name in names:
                nodes += 1
                if nodes > RECEIPT_JSON_MAX_NODES:
                    raise ValidationError("receipt source discovery exceeds its entry limit")
                relative = (prefix / name).as_posix()
                try:
                    entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise ValidationError(f"cannot inspect receipt source path: {relative}") from exc
                if stat.S_ISLNK(entry.st_mode):
                    if name.lower().endswith(VEGA_SOURCE_SUFFIXES):
                        raise PolicyError(f"receipt source must not be a symlink: {relative}")
                    continue
                if stat.S_ISDIR(entry.st_mode):
                    try:
                        child_fd = os.open(name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise PolicyError(f"receipt source directory cannot be opened safely: {relative}") from exc
                    try:
                        walk(child_fd, pathlib.PurePosixPath(relative), depth + 1)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(entry.st_mode) and name.lower().endswith(VEGA_SOURCE_SUFFIXES):
                    sources.add(relative)
                    if len(sources) > MAX_RECEIPT_FILES + 1:
                        raise ValidationError(f"receipt request exceeds {MAX_RECEIPT_FILES} Vega sources")

        try:
            walk(assets_fd, pathlib.PurePosixPath("assets"), 0)
        finally:
            os.close(assets_fd)
        return sorted(sources)

    def _receipt_data_paths(self, source: str, raw: bytes) -> list[str]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"receipt source must be UTF-8 JSON: {source}") from exc
        value = _load_json_text(text, source)
        if not isinstance(value, dict):
            raise ValidationError(f"receipt source must contain a JSON object: {source}")
        paths: set[str] = set()
        nodes = 0
        stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
        while stack:
            node, depth, data_context = stack.pop()
            nodes += 1
            if nodes > RECEIPT_JSON_MAX_NODES:
                raise ValidationError(f"receipt source exceeds the {RECEIPT_JSON_MAX_NODES}-node limit: {source}")
            if depth > RECEIPT_JSON_MAX_DEPTH:
                raise ValidationError(f"receipt source exceeds the {RECEIPT_JSON_MAX_DEPTH}-level limit: {source}")
            if isinstance(node, dict):
                if data_context and "url" in node:
                    paths.add(
                        self._dependency_project_relative(
                            node["url"],
                            description=f"data.url in {source}",
                        )
                    )
                for key, child in node.items():
                    stack.append((child, depth + 1, key == "data"))
            elif isinstance(node, list):
                for child in node:
                    stack.append((child, depth + 1, data_context))
        return sorted(paths)

    def _receipt_payload(
        self,
        manifest: dict[str, Any],
        status: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, _FileSnapshot]]:
        snapshots: dict[str, _FileSnapshot] = {}
        source_contents: dict[str, bytes] = {}
        total_bytes = 0

        def read_snapshot(relative: str, description: str, *, collect: bool) -> bytes | None:
            nonlocal total_bytes
            if relative in snapshots:
                return source_contents.get(relative) if collect else None
            result = self._read_project_snapshot(
                relative,
                description=description,
                max_bytes=MAX_RECEIPT_FILE_BYTES,
                collect=collect,
                missing_ok=False,
            )
            assert result is not None
            snapshot, content = result
            total_bytes += snapshot.size
            if total_bytes > MAX_RECEIPT_TOTAL_BYTES:
                raise ValidationError(f"receipt files exceed {MAX_RECEIPT_TOTAL_BYTES} total bytes")
            snapshots[relative] = snapshot
            if content is not None:
                source_contents[relative] = content
            return content

        request_sources = self._receipt_source_paths(manifest)
        digest = hashlib.sha256()
        digest.update(b"unaltraweb-companion-receipt-v1\0vegavisuals\0")
        for relative in request_sources:
            content = read_snapshot(relative, "receipt request source", collect=True)
            assert content is not None
            encoded = relative.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        if manifest["sha256"] != status["manifest_sha256"] or snapshots[manifest["path"]].sha256 != manifest["sha256"]:
            raise RenderError("visualization manifest changed after freshness validation")

        input_paths = set(manifest["inputs"])
        for item in manifest["visualizations"]:
            input_paths.update(item["inputs"])
        for item in status["visualizations"]:
            input_paths.update(dependency["path"] for dependency in item.get("dependencies", []))
        for relative in request_sources:
            if relative.lower().endswith(VEGA_SOURCE_SUFFIXES):
                input_paths.update(self._receipt_data_paths(relative, source_contents[relative]))
        if len(input_paths) > MAX_RECEIPT_FILES:
            raise ValidationError(f"receipt input inventory exceeds {MAX_RECEIPT_FILES} files")

        receipt_inputs: list[dict[str, str]] = []
        input_hashes: dict[str, str] = {}
        for relative in sorted(input_paths):
            read_snapshot(relative, "receipt input", collect=False)
            sha256 = snapshots[relative].sha256
            assert sha256 is not None
            input_hashes[relative] = sha256
            receipt_inputs.append({"path": relative, "sha256": sha256})

        status_items = {item["name"]: item for item in status["visualizations"]}
        for item in manifest["visualizations"]:
            read_snapshot(item["source"], "receipt visualization source", collect=False)
            if snapshots[item["source"]].sha256 != status_items[item["name"]].get("source_sha256"):
                raise RenderError(f"receipt source changed after validation: {item['source']}")
        for item in status["visualizations"]:
            for dependency in item.get("dependencies", []):
                path = dependency["path"]
                if path in input_hashes and dependency["sha256"] != input_hashes[path]:
                    raise RenderError(f"receipt input changed after validation: {path}")

        if len(manifest["visualizations"]) > MAX_RECEIPT_FILES:
            raise ValidationError(f"receipt artifact inventory exceeds {MAX_RECEIPT_FILES} files")
        artifacts: list[dict[str, str]] = []
        for item in sorted(manifest["visualizations"], key=lambda value: value["output"]):
            relative = item["output"]
            read_snapshot(relative, "receipt artifact", collect=False)
            sha256 = snapshots[relative].sha256
            assert sha256 is not None
            checked_hash = status_items[item["name"]].get("output_sha256")
            if checked_hash != sha256:
                raise RenderError(f"receipt artifact changed after freshness validation: {relative}")
            artifacts.append({"path": relative, "sha256": sha256})

        return (
            {
                "schema_version": 1,
                "provider": "vegavisuals",
                "provider_version": __version__,
                "release": DEFAULT_RELEASE,
                "request_sha256": digest.hexdigest(),
                "ok": True,
                "inputs": receipt_inputs,
                "artifacts": artifacts,
            },
            snapshots,
        )

    def _verify_receipt_snapshots(self, snapshots: dict[str, _FileSnapshot]) -> None:
        for relative, expected in snapshots.items():
            current = self._project_file_snapshot(
                relative,
                max_bytes=expected.size,
                description="receipt file",
            )
            if current != expected:
                raise RenderError(f"receipt file changed during publication: {relative}")

    def visualization_status(self, manifest_path: str = MANIFEST_NAME) -> dict[str, Any]:
        manifest = self._load_manifest(manifest_path)
        lock = self._load_lock()
        entries = lock["visualizations"]
        profile_name, profile_data, profile_path = self._load_profile(manifest["profile"])
        renderer = self._inspect_renderer(profile_name, profile_data, profile_path)
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for item in manifest["visualizations"]:
            try:
                validation = self._validate_file(
                    item["source"],
                    engine=item["engine"],
                    profile=manifest["profile"],
                    family=manifest["family"],
                    inputs=item["inputs"],
                )
                entry = entries.get(item["name"])
                if entry is not None and entry.get("output") != item["output"]:
                    entry = None
                fingerprint = self._fingerprint(
                    validation,
                    output_format=item["format"],
                )
                state, output_snapshot = self._output_state(
                    item["output"],
                    fingerprint=fingerprint,
                    entry=entry,
                    max_bytes=int(validation["_profile_data"]["runtime_limits"]["max_output_bytes"]),
                )
                result = {
                    **item,
                    "ok": True,
                    "state": state,
                    "fingerprint": fingerprint,
                    "source_sha256": validation["source_sha256"],
                    "output_exists": output_snapshot.exists,
                    "output_sha256": output_snapshot.sha256,
                    "managed": entry is not None,
                    "dependencies": self._public_validation(validation)["dependencies"],
                }
            except (ValidationError, OSError) as exc:
                state = "invalid"
                result = {**item, "ok": False, "state": state, "error": str(exc)}
            counts[state] = counts.get(state, 0) + 1
            items.append(result)
        names = {item["name"] for item in manifest["visualizations"]}
        orphaned = sorted(name for name in entries if name not in names and not name.startswith("direct-"))
        return {
            "ok": counts.get("invalid", 0) == 0,
            "project": str(self.project_root),
            "manifest": manifest["path"],
            "manifest_sha256": manifest["sha256"],
            "profile": manifest["profile"],
            "family": manifest["family"],
            "inputs": manifest["inputs"],
            "renderer": self._public_renderer(renderer),
            "lock": {
                "path": LOCK_NAME,
                "version": LOCK_VERSION,
                "exists": self._project_file_hash(
                    LOCK_NAME,
                    max_bytes=MAX_LOCK_BYTES,
                    description="visualization lock",
                )
                is not None,
            },
            "visualizations": items,
            "counts": counts,
            "orphaned_lock_entries": orphaned,
        }

    def visualization_check(self, manifest_path: str = MANIFEST_NAME) -> dict[str, Any]:
        with self._project_lock():
            self._invalidate_receipt()
            status = self.visualization_status(manifest_path)
            issues = [
                f"{item['name']}: {item['state']}"
                for item in status["visualizations"]
                if item["state"] != "fresh"
            ]
            issues.extend(f"orphaned lock entry: {name}" for name in status["orphaned_lock_entries"])
            if issues:
                return {**status, "ok": False, "issues": issues}
            manifest = self._load_manifest(manifest_path)
            receipt, snapshots = self._receipt_payload(manifest, status)
            data = json.dumps(
                receipt,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            self._verify_receipt_snapshots(snapshots)
            try:
                self._publish_owned_project_bytes(RECEIPT_PATH, data, max_bytes=MAX_RECEIPT_BYTES)
                self._verify_receipt_snapshots(snapshots)
            except BaseException:
                self._invalidate_receipt()
                raise
            return {
                **status,
                "ok": True,
                "issues": [],
                "receipt": {
                    "path": RECEIPT_PATH,
                    "schema_version": 1,
                    "request_sha256": receipt["request_sha256"],
                    "inputs": len(receipt["inputs"]),
                    "artifacts": len(receipt["artifacts"]),
                },
            }

    def render_visualizations(
        self,
        manifest_path: str = MANIFEST_NAME,
        *,
        include_data: bool = False,
        confirm_replace: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        manifest = self._load_manifest(manifest_path)
        results: list[dict[str, Any]] = []
        for item in manifest["visualizations"]:
            try:
                result = self.render_visualization(
                    item["source"],
                    item["output"],
                    engine=item["engine"],
                    output_format=item["format"],
                    profile=manifest["profile"],
                    family=manifest["family"],
                    inputs=item["inputs"],
                    include_data=include_data,
                    confirm_replace=confirm_replace,
                    force=force,
                    dry_run=dry_run,
                    lock_name=item["name"],
                )
            except (ValidationError, RenderError) as exc:
                result = {
                    "ok": False,
                    "name": item["name"],
                    "source": item["source"],
                    "output": item["output"],
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            else:
                result = {"name": item["name"], **result}
            results.append(result)
        return {
            "ok": all(result.get("ok", False) for result in results),
            "project": str(self.project_root),
            "manifest": manifest["path"],
            "profile": manifest["profile"],
            "family": manifest["family"],
            "results": results,
            "rendered": sum(bool(result.get("rendered")) for result in results),
            "skipped": sum(bool(result.get("skipped")) for result in results),
            "failed": sum(not bool(result.get("ok")) for result in results),
            "dry_run": dry_run,
        }

    def release_status(self, release: str = DEFAULT_RELEASE) -> dict[str, Any]:
        checkout = source_checkout()
        version_matches = release == DEFAULT_RELEASE
        if checkout is None:
            return {
                "ok": version_matches,
                "source": "package",
                "requested": release,
                "package_version": __version__,
                "current_tag": DEFAULT_RELEASE,
                "current_matches_release": version_matches,
            }
        tag = _run_command(
            ["git", "-C", str(checkout), "rev-parse", "--verify", f"refs/tags/{release}^{{commit}}"],
            cwd=checkout,
            timeout=30,
        )
        head = _run_command(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            cwd=checkout,
            timeout=30,
        )
        current = _run_command(
            ["git", "-C", str(checkout), "describe", "--tags", "--exact-match"],
            cwd=checkout,
            timeout=30,
        )
        status = _run_command(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            cwd=checkout,
            timeout=30,
        )
        current_tag = current["stdout"].strip() if current["returncode"] == 0 else None
        requested_sha = tag["stdout"].strip() if tag["returncode"] == 0 else None
        current_head = head["stdout"].strip() if head["returncode"] == 0 else None
        clean = status["returncode"] == 0 and not status["stdout"].strip()
        current_matches = current_tag == release and current_head == requested_sha
        return {
            "ok": version_matches and current_matches and clean,
            "source": "checkout",
            "requested": release,
            "package_version": __version__,
            "requested_sha": requested_sha,
            "current_head": current_head,
            "current_tag": current_tag,
            "current_matches_release": current_matches,
            "clean": clean,
        }

    def update_factory(self, *, dry_run: bool = False) -> dict[str, Any]:
        checkout = source_checkout()
        if checkout is None:
            command = [sys.executable, "-m", "pip", "install", "--upgrade", "vegavisuals[mcp]"]
            return {
                "ok": True,
                "source": "package",
                "dry_run": True,
                "message": "Installed packages are not mutated by the MCP; run the upgrade command explicitly.",
                "command": command,
            }
        status = _run_command(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            cwd=checkout,
            timeout=30,
        )
        if status["returncode"] != 0 or status["stdout"].strip():
            return {
                "ok": False,
                "source": "checkout",
                "message": "Refusing to update a dirty or unreadable vegavisuals checkout.",
                "status": status,
            }
        command = ["git", "-C", str(checkout), "pull", "--ff-only"]
        if dry_run:
            return {
                "ok": True,
                "source": "checkout",
                "dry_run": True,
                "repo": str(checkout),
                "command": command,
            }
        result = _run_command(command, cwd=checkout, timeout=300)
        return {
            "ok": result["returncode"] == 0,
            "source": "checkout",
            "result": result,
        }

    def install_check(self, command: str = "") -> dict[str, Any]:
        resolved = shutil.which(command) if command else None
        if command and not resolved:
            candidate = pathlib.Path(command).expanduser()
            if candidate.is_file():
                resolved = str(candidate.resolve())
        invoked = pathlib.Path(sys.argv[0])
        if not resolved and invoked.name == "vegavisuals" and invoked.is_file():
            resolved = str(invoked.resolve())
        if resolved:
            resolved = str(pathlib.Path(resolved).resolve())
        launcher = [resolved] if resolved else ([sys.executable, "-m", "vegavisuals.cli"] if not command else [])
        version_result = None
        mcp_dependency = None
        if launcher:
            version_result = _run_command([*launcher, "--version"], cwd=self.project_root, timeout=30)
            python = _entrypoint_python(resolved) if resolved else sys.executable
            if python:
                mcp_dependency = _run_command(
                    [
                        python,
                        "-c",
                        "from importlib.metadata import version; "
                        "from mcp.server.fastmcp import FastMCP; print(version('mcp'))",
                    ],
                    cwd=self.project_root,
                    timeout=30,
                )
            else:
                mcp_dependency = {
                    "command": [resolved],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "Could not determine the Python interpreter used by the CLI entrypoint.",
                }
        manifest = factory_metadata_root() / "mcp-factory.yml"
        cli_version_matches = bool(
            version_result
            and version_result["returncode"] == 0
            and version_result["stdout"].strip() == f"vegavisuals {__version__}"
        )
        mcp_version = mcp_dependency["stdout"].strip() if mcp_dependency else None
        mcp_version_matches = bool(
            mcp_dependency and mcp_dependency["returncode"] == 0 and mcp_version == MCP_VERSION
        )
        ok = bool(
            cli_version_matches
            and mcp_version_matches
            and manifest.is_file()
        )
        return {
            "ok": ok,
            "command": command or "vegavisuals",
            "resolved": resolved,
            "launcher": launcher,
            "version_result": version_result,
            "cli_version_matches": cli_version_matches,
            "mcp_dependency": mcp_dependency,
            "mcp_version": mcp_version,
            "mcp_version_matches": mcp_version_matches,
            "factory_manifest": str(manifest),
            "factory_manifest_exists": manifest.is_file(),
            "package_version": __version__,
            "install_hints": [
                "python3 -m pip install 'vegavisuals[mcp]'",
                f"uv tool install 'vegavisuals[mcp] @ git+{REPOSITORY_URL}.git@{DEFAULT_RELEASE}'",
            ],
        }

    def lifecycle_check(self, command: str = "") -> dict[str, Any]:
        factory_lifecycle = self.factory_lifecycle_check(command)
        project = self.visualization_check()
        return {
            "ok": factory_lifecycle["ok"] and project["ok"],
            "install": factory_lifecycle["install"],
            "factory": factory_lifecycle["factory"],
            "project": project,
        }

    def factory_lifecycle_check(
        self,
        command: str = "",
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
    ) -> dict[str, Any]:
        install = self.install_check(command)
        factory = self.factory_check(profile=profile, family=family)
        return {
            "ok": install["ok"] and factory["ok"],
            "install": install,
            "factory": factory,
        }

    def self_test(self) -> dict[str, Any]:
        factory = self.factory_check()
        cases = [
            (
                "vega-lite",
                {
                    "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
                    "data": {"values": [{"x": "A", "y": 1}]},
                    "mark": "bar",
                    "encoding": {
                        "x": {"field": "x", "type": "nominal"},
                        "y": {"field": "y", "type": "quantitative"},
                    },
                },
            ),
            (
                "vega",
                {
                    "$schema": "https://vega.github.io/schema/vega/v6.json",
                    "width": 100,
                    "height": 80,
                    "data": [{"name": "table", "values": [{"x": 1}]}],
                    "marks": [{"type": "rect"}],
                },
            ),
        ]
        validations = [
            self.render_visualization_text(
                json.dumps(spec),
                engine=engine,
                output_format="svg",
                dry_run=True,
            )
            for engine, spec in cases
        ]
        return {
            "ok": factory["ok"] and all(result.get("ok", False) for result in validations),
            "factory": factory,
            "validations": validations,
        }

    def _down(self, *, all_workspaces: bool) -> dict[str, Any]:
        project_id = workspace_id(self.project_root)
        docker = shutil.which("docker")
        if not docker:
            return {
                "ok": True,
                "containers": [],
                "workspace": None if all_workspaces else project_id,
                "all_workspaces": all_workspaces,
                "message": "Docker is unavailable",
            }
        filters = ["--filter", f"label={CONTAINER_LABEL}"]
        if not all_workspaces:
            filters.extend(
                ["--filter", f"label={CONTAINER_WORKSPACE_LABEL}={project_id}"]
            )
        listed = _run_command(
            [
                docker,
                "container",
                "ls",
                "--all",
                "--quiet",
                *filters,
            ],
            cwd=self.project_root,
            timeout=30,
        )
        if listed["returncode"] != 0:
            return {
                "ok": False,
                "containers": [],
                "workspace": None if all_workspaces else project_id,
                "all_workspaces": all_workspaces,
                "result": listed,
            }
        containers = listed["stdout"].split()
        if any(not re.fullmatch(r"[0-9a-f]{12,64}", container) for container in containers):
            return {
                "ok": False,
                "containers": [],
                "workspace": None if all_workspaces else project_id,
                "all_workspaces": all_workspaces,
                "message": "Docker returned an invalid container identifier; refusing cleanup.",
            }
        if not containers:
            return {
                "ok": True,
                "containers": [],
                "workspace": None if all_workspaces else project_id,
                "all_workspaces": all_workspaces,
            }
        removed = _run_command(
            [docker, "container", "rm", "--force", *containers],
            cwd=self.project_root,
            timeout=60,
        )
        return {
            "ok": removed["returncode"] == 0,
            "containers": containers,
            "workspace": None if all_workspaces else project_id,
            "all_workspaces": all_workspaces,
            "result": removed,
        }

    def down(self) -> dict[str, Any]:
        return self._down(all_workspaces=False)

    def down_all(self) -> dict[str, Any]:
        return self._down(all_workspaces=True)

    def install_codex_mcp(
        self,
        *,
        server_name: str = "vegavisuals",
        codex_bin: str = "codex",
        command: str = "",
        project: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        resolved_codex = shutil.which(codex_bin) or (codex_bin if dry_run else None)
        if not resolved_codex:
            return {"ok": False, "message": f"Codex binary not found: {codex_bin}"}
        server = self.client_config(
            workspace_placeholder=project or str(self.project_root),
            command=command,
        )["mcpServers"]["vegavisuals"]
        server_parts = [server["command"], *server.get("args", [])]
        server_env = server.get("env", {})
        env_args = [value for key, value in server_env.items() for value in ("--env", f"{key}={value}")]
        list_command = [resolved_codex, "mcp", "list", "--json"]
        add = [resolved_codex, "mcp", "add", server_name, *env_args, "--", *server_parts]
        payload: dict[str, Any] = {
            "ok": True,
            "server_name": server_name,
            "list": list_command,
            "add": add,
            "dry_run": dry_run,
        }
        if dry_run:
            return payload
        list_result = _run_command(list_command, cwd=self.project_root, timeout=60)
        payload["list_result"] = {
            "returncode": list_result["returncode"],
        }
        if list_result["returncode"] != 0:
            payload["ok"] = False
            payload["message"] = "Codex MCP registrations could not be listed safely; refusing to make changes."
            return payload
        try:
            listed = json.loads(list_result["stdout"])
            if isinstance(listed, list):
                servers = listed
            elif isinstance(listed, dict) and isinstance(listed.get("servers"), list):
                servers = listed["servers"]
            elif isinstance(listed, dict) and server_name in listed and isinstance(listed[server_name], dict):
                servers = [{"name": server_name, **listed[server_name]}]
            else:
                raise ValueError("unexpected Codex MCP list shape")
            existing = next((item for item in servers if isinstance(item, dict) and item.get("name") == server_name), None)
        except (TypeError, ValueError):
            payload["ok"] = False
            payload["message"] = "Codex MCP registrations could not be parsed safely; refusing to make changes."
            return payload
        if existing is not None:
            transport = existing.get("transport", existing)
            if not isinstance(transport, dict):
                payload["ok"] = False
                payload["message"] = "The existing Codex MCP transport is invalid; refusing to replace it."
                return payload
            existing_command = transport.get("command")
            existing_args = transport.get("args", [])
            if not isinstance(existing_command, str) or not isinstance(existing_args, list) or not all(
                isinstance(value, str) for value in existing_args
            ):
                payload["ok"] = False
                payload["message"] = "The existing Codex MCP command is invalid; refusing to replace it."
                return payload
            existing_parts = [existing_command, *existing_args]
            behavior = {
                key: value
                for key, value in transport.items()
                if key not in {"type", "command", "args"} and value not in (None, "", [], {})
            }
            expected_behavior = {"env": server_env} if server_env else {}
            outer_behavior = {
                key: value
                for key, value in existing.items()
                if key not in {"name", "transport", "enabled", "disabled_reason"}
                and value not in (None, "", [], {})
            }
            enabled = existing.get("enabled", True) is True
            disabled_reason = existing.get("disabled_reason")
            if (
                transport.get("type", "stdio") == "stdio"
                and existing_parts == server_parts
                and behavior == expected_behavior
                and not outer_behavior
                and enabled
                and disabled_reason in (None, "")
            ):
                payload["already_configured"] = True
                return payload
            payload["ok"] = False
            payload["message"] = (
                "A different Codex MCP registration already exists; refusing to replace it without explicit removal."
            )
            return payload
        payload["add_result"] = _run_command(add, cwd=self.project_root, timeout=60)
        payload["ok"] = payload["add_result"]["returncode"] == 0
        return payload

    def client_config(
        self,
        *,
        workspace_placeholder: str = "${workspaceFolder}",
        command: str = "",
        vscode: bool = False,
    ) -> dict[str, Any]:
        server_command = command or sys.executable
        args = ["mcp", "serve"]
        if not command:
            args = ["-m", "vegavisuals.cli", *args]
        if vscode:
            return {
                "servers": {
                    "vegavisuals": {
                        "type": "stdio",
                        "command": server_command,
                        "args": args,
                        "env": {"MCP_CONSUMER_WORKSPACE": workspace_placeholder},
                    }
                }
            }
        return {
            "mcpServers": {
                "vegavisuals": {
                    "command": server_command,
                    "args": args,
                    "env": {"MCP_CONSUMER_WORKSPACE": workspace_placeholder},
                }
            }
        }

    def factory_manifest(self) -> dict[str, Any]:
        checkout = source_checkout()
        factory_root = factory_metadata_root()
        client = self.client_config()["mcpServers"]["vegavisuals"]
        if checkout is not None:
            factory_launcher = [
                "bash",
                "${factoryRoot}/scripts/factory-launcher",
            ]
            project = "${workspaceFolder}"
            transport = ["make", "--no-print-directory", "-C", "${factoryRoot}", "mcp-stdio"]
            commands = {
                "build": ["make", "mcp-build"],
                "init": [*factory_launcher, "init", project],
                "check": ["make", "mcp-check"],
                "tests": ["make", "tests"],
                "smoke": ["make", "mcp-smoke"],
                "down": [*factory_launcher, "down", project],
                "update": [*factory_launcher, "update"],
                "release_status": [*factory_launcher, "release-status"],
                "install_check": [*factory_launcher, "install-check"],
                "install_codex_mcp": [*factory_launcher, "install-codex-mcp", project],
                "factory_check": [*factory_launcher, "factory-check"],
                "serve": [*factory_launcher, "serve", project],
                "manifest": [*factory_launcher, "manifest"],
                "render": [*factory_launcher, "render", project],
                "render_all": [*factory_launcher, "render-all", project],
            }
        else:
            package_cli = [sys.executable, "-m", "vegavisuals.cli"]
            project_cli = [*package_cli, "--project", "${workspaceFolder}"]
            transport = [*package_cli, "mcp", "serve"]
            commands = {
                "build": [*package_cli, "ensure-renderer"],
                "init": [*project_cli, "init"],
                "check": [*package_cli, "factory-lifecycle-check"],
                "tests": [*package_cli, "self-test"],
                "smoke": [*package_cli, "mcp-smoke"],
                "down": [*project_cli, "down"],
                "update": [*package_cli, "update"],
                "release_status": [*package_cli, "release-status"],
                "install_check": [*package_cli, "install-check"],
                "install_codex_mcp": [
                    *project_cli,
                    "install-codex-mcp",
                    "--workspace",
                    "${workspaceFolder}",
                ],
                "factory_check": [*package_cli, "factory-check"],
                "serve": [*project_cli, "mcp", "serve"],
                "client_config": [*package_cli, "mcp", "client-config"],
                "manifest": [*package_cli, "factory-manifest"],
                "render": [*project_cli, "render"],
                "render_all": [*project_cli, "render-all"],
            }
        return {
            "ok": True,
            "schema_version": 1,
            "name": "vegavisuals",
            "version": __version__,
            "kind": "codex-mcp-factory",
            "description": "Hardened Docker rendering for themed Vega-Lite and Vega visualizations.",
            "license": "GPL-3.0-only",
            "repository": REPOSITORY_URL,
            "factory": str(factory_root),
            "factory_assets": str(self.assets),
            "workspace_rule": {
                "binding": "consumer",
                "consumer_root": ".",
                "source_paths": [MANIFEST_NAME],
                "generated_paths": [".cache/vegavisuals", LOCK_NAME, RECEIPT_PATH],
                "init_creates": [".cache/vegavisuals", MANIFEST_NAME],
                "allowed_external_writes": [],
            },
            "runtime": {
                "kind": "python",
                "package_manager": "pip",
                "package": "vegavisuals[mcp]",
                "module": "vegavisuals",
                "mcp_version": MCP_VERSION,
            },
            "transport": {
                "type": "stdio",
                "command": transport,
                "env": {"MCP_CONSUMER_WORKSPACE": "${workspaceFolder}"},
            },
            "commands": commands,
            "mcp": {
                "server_name": "vegavisuals",
                "transport": "stdio",
                "consumer_root_fixed_at_startup": True,
                "command": [client["command"], *client["args"]],
                "env": client["env"],
                "tools": list(MCP_TOOL_NAMES),
                "resources": list(MCP_RESOURCE_URIS),
            },
            "discovery": {
                "file": "mcp-factory.yml",
                "suggested_scan_roots": ["~/git"],
                "checkout_required_for_make_lifecycle": checkout is not None,
            },
            "release": {
                "default": DEFAULT_RELEASE,
                "profile": DEFAULT_PROFILE,
                "family": DEFAULT_FAMILY,
            },
            "defaults": {
                "profile": DEFAULT_PROFILE,
                "family": DEFAULT_FAMILY,
            },
            "contracts": {
                "manifest": 1,
                "lock": LOCK_VERSION,
                "inline_cache": CACHE_VERSION,
                "receipt": 1,
                "mcp_errors": "typed-application-result",
            },
        }

    def version_status(self) -> dict[str, Any]:
        return {"ok": True, "name": "vegavisuals", "version": __version__}
