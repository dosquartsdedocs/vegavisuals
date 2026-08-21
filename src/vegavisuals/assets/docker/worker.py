#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import math
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

import vl_convert


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def check_depth(value: Any, maximum: int = 128) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum:
            raise ValueError(f"JSON nesting exceeds {maximum} levels")
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
            parse_float=parse_float,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the worker parser limit") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    check_depth(value)
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def confined(path: pathlib.Path, roots: tuple[pathlib.Path, ...]) -> pathlib.Path:
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError(f"path is outside worker roots: {path}")
    return resolved


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Render one host-validated Vega/Vega-Lite spec")
    result.add_argument("--input", required=True)
    result.add_argument("--output", required=True)
    result.add_argument("--engine", choices=["vega-lite", "vega"], required=True)
    result.add_argument("--format", choices=["svg", "png", "pdf"], required=True)
    result.add_argument("--theme", required=True)
    result.add_argument("--vl-version", required=True)
    result.add_argument("--expected-vl-convert", required=True)
    result.add_argument("--expected-vega", required=True)
    result.add_argument("--max-output-bytes", required=True, type=int)
    return result


def normalize_pdf(artifact: bytes, maximum: int) -> bytes:
    if len(artifact) > maximum:
        raise ValueError(f"renderer output exceeds {maximum} bytes before PDF normalization")
    with tempfile.TemporaryDirectory(prefix="vegavisuals-pdf-", dir="/tmp") as temporary:
        root = pathlib.Path(temporary)
        source = root / "source.pdf"
        output = root / "normalized.pdf"
        source.write_bytes(artifact)
        completed = subprocess.run(
            [
                "qpdf",
                "--deterministic-id",
                "--object-streams=disable",
                str(source),
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-8192:]
            raise RuntimeError(f"qpdf normalization failed: {message.strip()}")
        checked = subprocess.run(
            ["qpdf", "--check", str(output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        if checked.returncode != 0:
            message = (checked.stderr or checked.stdout).decode("utf-8", errors="replace")[-8192:]
            raise RuntimeError(f"qpdf validation failed: {message.strip()}")
        normalized = output.read_bytes()
        if len(normalized) > maximum:
            raise ValueError(f"normalized PDF exceeds {maximum} bytes")
        return normalized


def render(args: argparse.Namespace) -> dict[str, Any]:
    output_root = pathlib.Path("/output").resolve()
    theme_root = pathlib.Path("/opt/vegavisuals/themes").resolve()
    source = confined(pathlib.Path(args.input), (output_root,))
    output = confined(pathlib.Path(args.output), (output_root,))
    theme_path = confined(pathlib.Path(args.theme), (theme_root,))
    if output.suffix.lower() != f".{args.format}":
        raise ValueError("output suffix and requested format differ")

    installed = importlib.metadata.version("vl-convert-python")
    if installed != args.expected_vl_convert:
        raise RuntimeError(
            f"vl-convert-python version mismatch: expected {args.expected_vl_convert}, got {installed}"
        )
    runtime = vl_convert.get_vega_version()
    if runtime != args.expected_vega:
        raise RuntimeError(f"Vega runtime mismatch: expected {args.expected_vega}, got {runtime}")
    versions = vl_convert.get_vegalite_versions()
    if args.vl_version not in versions:
        raise RuntimeError(f"unsupported bundled Vega-Lite version: {args.vl_version}")

    spec = load_json(source)
    theme = load_json(theme_path)
    allowed_base_urls: list[str] = []
    if args.engine == "vega-lite":
        source_config = spec.pop("config", {})
        if not isinstance(source_config, dict):
            raise ValueError("Vega-Lite config must be an object")
        config = deep_merge(theme, source_config)
        function = getattr(vl_convert, f"vegalite_to_{args.format}")
        artifact = function(
            spec,
            vl_version=args.vl_version,
            config=config,
            allowed_base_urls=allowed_base_urls,
        )
    else:
        source_config = spec.get("config", {})
        if not isinstance(source_config, dict):
            raise ValueError("Vega config must be an object")
        spec["config"] = deep_merge(theme, source_config)
        function = getattr(vl_convert, f"vega_to_{args.format}")
        artifact = function(spec, allowed_base_urls=allowed_base_urls)

    artifact_bytes = artifact.encode("utf-8") if isinstance(artifact, str) else bytes(artifact)
    if args.format == "pdf":
        artifact_bytes = normalize_pdf(artifact_bytes, args.max_output_bytes)
    if len(artifact_bytes) > args.max_output_bytes:
        raise ValueError(f"renderer output exceeds {args.max_output_bytes} bytes")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(artifact_bytes)
    return {
        "ok": True,
        "engine": args.engine,
        "format": args.format,
        "bytes": len(artifact_bytes),
        "vega": runtime,
        "vega_lite": args.vl_version if args.engine == "vega-lite" else None,
        "vl_convert_python": installed,
    }


def main() -> int:
    args = parser().parse_args()
    try:
        payload = render(args)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
