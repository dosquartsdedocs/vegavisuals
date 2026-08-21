from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._version import __version__
from .errors import ValidationError, VegavisualsError
from .registry import DEFAULT_FAMILY, DEFAULT_PROFILE, MANIFEST_NAME, Registry, json_dumps


def _print(payload: Any) -> None:
    print(json_dumps(payload))


def _result(payload: dict[str, Any]) -> int:
    _print(payload)
    return 0 if payload.get("ok", False) else 1


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _print({"ok": False, "error": {"type": "ArgumentError", "message": message}})
        raise SystemExit(2)


def _add_contract_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--family", default=DEFAULT_FAMILY)


def _add_render_policy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include-data", action="store_true", help="Include SVG text or base64 binary data")
    parser.add_argument("--replace", action="store_true", help="Confirm replacement of unmanaged or modified output")
    parser.add_argument("--force", action="store_true", help="Render even when the managed output is fresh")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(prog="vegavisuals")
    parser.add_argument("--project", default=".", help="Consumer project root, fixed when the process starts")
    parser.add_argument("--version", action="version", version=f"vegavisuals {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("version", help="Return package version metadata")
    commands.add_parser("profile-inventory", help="List JSON compatibility profiles")
    theme = commands.add_parser("theme-inventory", help="List theme families and token assets")
    theme.add_argument("--family", default="")
    compatibility = commands.add_parser("compatibility-status", help="Inspect one compatibility profile")
    compatibility.add_argument("--profile", default=DEFAULT_PROFILE)
    commands.add_parser("factory-check", help="Validate packaged profiles, themes, and renderer assets")

    validate = commands.add_parser("validate", help="Validate one .vl.json or .vg.json source")
    validate.add_argument("source")
    validate.add_argument("--engine", choices=["auto", "vega-lite", "vega"], default="auto")
    validate.add_argument("--input", dest="inputs", action="append", default=[])
    _add_contract_options(validate)

    render = commands.add_parser("render", help="Render one source file through hardened Docker")
    render.add_argument("source")
    render.add_argument("output")
    render.add_argument("--engine", choices=["auto", "vega-lite", "vega"], default="auto")
    render.add_argument("--format", choices=["svg", "png", "pdf"])
    render.add_argument("--input", dest="inputs", action="append", default=[])
    render.add_argument("--name", help="Optional stable lock entry name")
    _add_contract_options(render)
    _add_render_policy_options(render)

    render_text = commands.add_parser("render-text", help="Render a JSON spec supplied as text or stdin")
    render_text.add_argument("--text", help="JSON source text; stdin is read when omitted")
    render_text.add_argument("--output", help="Optional output path; cache path is returned when omitted")
    render_text.add_argument("--engine", choices=["auto", "vega-lite", "vega"], default="auto")
    render_text.add_argument("--format", choices=["svg", "png", "pdf"])
    _add_contract_options(render_text)
    _add_render_policy_options(render_text)

    status = commands.add_parser("status", help="Report manifest and lock freshness")
    status.add_argument("--manifest", default=MANIFEST_NAME)
    check = commands.add_parser("check", help="Require all manifest outputs to be fresh and managed")
    check.add_argument("--manifest", default=MANIFEST_NAME)
    render_all = commands.add_parser("render-all", help="Render every visualization in the project manifest")
    render_all.add_argument("--manifest", default=MANIFEST_NAME)
    _add_render_policy_options(render_all)

    commands.add_parser("factory-manifest", help="Return the factory discovery contract")
    build_renderer = commands.add_parser("build-renderer", help="Build the pinned renderer image")
    build_renderer.add_argument("--profile", default=DEFAULT_PROFILE)
    build_renderer.add_argument("--dry-run", action="store_true")
    ensure_renderer = commands.add_parser("ensure-renderer", help="Build the renderer image only when absent")
    ensure_renderer.add_argument("--profile", default=DEFAULT_PROFILE)

    mcp = commands.add_parser("mcp", help="MCP stdio server and client metadata")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_commands.add_parser("serve", help="Run the FastMCP server over stdio")
    client = mcp_commands.add_parser("client-config", help="Return an MCP client configuration")
    client.add_argument("--workspace-placeholder", default="${workspaceFolder}")
    client.add_argument("--command", dest="mcp_executable", default="vegavisuals")
    client.add_argument("--format", choices=["generic", "vscode-workspace"], default="generic")
    mcp_commands.add_parser("list-tools", help="List the stable MCP tool and resource contract")
    return parser


def dispatch(args: argparse.Namespace, registry: Registry) -> int:
    if args.command == "version":
        return _result(registry.version_status())
    if args.command == "profile-inventory":
        return _result(registry.profile_inventory())
    if args.command == "theme-inventory":
        return _result(registry.theme_inventory(args.family))
    if args.command == "compatibility-status":
        return _result(registry.compatibility_status(args.profile))
    if args.command == "factory-check":
        return _result(registry.factory_check())
    if args.command == "validate":
        return _result(
            registry.validate_visualization(
                args.source,
                engine=args.engine,
                profile=args.profile,
                family=args.family,
                inputs=args.inputs,
            )
        )
    if args.command == "render":
        return _result(
            registry.render_visualization(
                args.source,
                args.output,
                engine=args.engine,
                output_format=args.format,
                profile=args.profile,
                family=args.family,
                inputs=args.inputs,
                include_data=args.include_data,
                confirm_replace=args.replace,
                force=args.force,
                dry_run=args.dry_run,
                lock_name=args.name,
            )
        )
    if args.command == "render-text":
        if args.text is not None:
            text = args.text
        else:
            _, profile_data, _ = registry._load_profile(args.profile)
            maximum = int(profile_data["runtime_limits"]["max_inline_source_bytes"])
            raw = sys.stdin.buffer.read(maximum + 1)
            if len(raw) > maximum:
                raise ValidationError(f"inline visualization exceeds {maximum} bytes")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError("inline visualization must be UTF-8 JSON") from exc
        return _result(
            registry.render_visualization_text(
                text,
                output_path=args.output,
                engine=args.engine,
                output_format=args.format,
                profile=args.profile,
                family=args.family,
                include_data=args.include_data,
                confirm_replace=args.replace,
                force=args.force,
                dry_run=args.dry_run,
            )
        )
    if args.command == "status":
        return _result(registry.visualization_status(args.manifest))
    if args.command == "check":
        return _result(registry.visualization_check(args.manifest))
    if args.command == "render-all":
        return _result(
            registry.render_visualizations(
                args.manifest,
                include_data=args.include_data,
                confirm_replace=args.replace,
                force=args.force,
                dry_run=args.dry_run,
            )
        )
    if args.command == "factory-manifest":
        return _result(registry.factory_manifest())
    if args.command == "build-renderer":
        return _result(registry.build_renderer(args.profile, dry_run=args.dry_run))
    if args.command == "ensure-renderer":
        return _result(registry.ensure_renderer(args.profile))
    if args.command == "mcp":
        if args.mcp_command == "serve":
            from .mcp_server import run_server

            run_server(registry)
            return 0
        if args.mcp_command == "client-config":
            return _result(
                {
                    "ok": True,
                    **registry.client_config(
                        workspace_placeholder=args.workspace_placeholder,
                        command=args.mcp_executable,
                        vscode=args.format == "vscode-workspace",
                    ),
                }
            )
        if args.mcp_command == "list-tools":
            from .mcp_server import RESOURCE_URIS, TOOL_NAMES

            return _result({"ok": True, "tools": list(TOOL_NAMES), "resources": list(RESOURCE_URIS)})
    raise RuntimeError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        registry = Registry(args.project)
        return dispatch(args, registry)
    except RecursionError as exc:
        _print(
            {
                "ok": False,
                "error": {"type": "ValidationError", "message": f"input nesting exceeds parser limits: {exc}"},
            }
        )
        return 1
    except (VegavisualsError, OSError, ValueError) as exc:
        _print({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "interrupted"}}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
