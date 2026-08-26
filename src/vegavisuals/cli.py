from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._version import __version__
from .errors import ValidationError, VegavisualsError
from .registry import DEFAULT_FAMILY, DEFAULT_PROFILE, DEFAULT_RELEASE, MANIFEST_NAME, Registry, json_dumps


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
    factory_check = commands.add_parser("factory-check", help="Validate packaged profiles, themes, and renderer assets")
    _add_contract_options(factory_check)
    initialize = commands.add_parser("init", help="Initialize a consumer project without overwriting it")
    initialize.add_argument("--force", action="store_true")
    release_status = commands.add_parser("release-status", help="Inspect package or checkout release status")
    release_status.add_argument("--release", default=DEFAULT_RELEASE)
    update = commands.add_parser("update", help="Update a clean checkout or report the package upgrade command")
    update.add_argument("--dry-run", action="store_true")
    install_check = commands.add_parser("install-check", help="Validate CLI, MCP dependency, and discovery assets")
    install_check.add_argument("--command", dest="executable", default="")
    lifecycle_check = commands.add_parser("lifecycle-check", help="Validate installation, factory, and consumer")
    lifecycle_check.add_argument("--command", dest="executable", default="")
    commands.add_parser("self-test", help="Run package-native factory and validation checks")
    commands.add_parser("mcp-smoke", help="Probe MCP stdio and render both engines")
    commands.add_parser("down", help="Remove containers owned by the vegavisuals factory")
    install_codex = commands.add_parser("install-codex-mcp", help="Install the startup-fixed MCP in Codex")
    install_codex.add_argument("--name", default="vegavisuals")
    install_codex.add_argument("--codex-bin", default="codex")
    install_codex.add_argument("--command", dest="executable", default="")
    install_codex.add_argument("--workspace", default="")
    install_codex.add_argument("--dry-run", action="store_true")

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
    client.add_argument("--command", dest="mcp_executable", default="")
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
        return _result(registry.factory_check(profile=args.profile, family=args.family))
    if args.command == "init":
        return _result(registry.initialize_project(force=args.force))
    if args.command == "release-status":
        return _result(registry.release_status(args.release))
    if args.command == "update":
        return _result(registry.update_factory(dry_run=args.dry_run))
    if args.command == "install-check":
        return _result(registry.install_check(args.executable))
    if args.command == "lifecycle-check":
        return _result(registry.lifecycle_check(args.executable))
    if args.command == "self-test":
        return _result(registry.self_test())
    if args.command == "mcp-smoke":
        import asyncio
        import os

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ModuleNotFoundError as exc:
            return _result(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": "mcp-smoke requires the vegavisuals[mcp] extra",
                    },
                }
            )

        async def probe() -> dict[str, Any]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "vegavisuals.cli",
                    "--project",
                    str(registry.project_root),
                    "mcp",
                    "serve",
                ],
                env=dict(os.environ),
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    resources = await session.list_resources()
                    factory = await session.call_tool("factory_check", {})
                    factory_payload = json.loads("\n".join(getattr(item, "text", "") for item in factory.content))
                    rendered = []
                    for engine, spec in (
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
                    ):
                        result = await session.call_tool(
                            "render_visualization_text",
                            {
                                "visualization_text": json.dumps(spec),
                                "engine": engine,
                                "output_format": "svg",
                                "force": True,
                            },
                        )
                        payload = json.loads("\n".join(getattr(item, "text", "") for item in result.content))
                        rendered.append(
                            not result.isError
                            and payload.get("ok") is True
                            and payload.get("rendered") is True
                        )
                    tool_names = {tool.name for tool in tools.tools}
                    resource_uris = {str(resource.uri) for resource in resources.resources}
                    return {
                        "ok": factory_payload.get("ok") is True
                        and all(rendered)
                        and set(("factory_check", "render_visualization_text")) <= tool_names
                        and set(("vegavisuals://factory/check", "vegavisuals://factory-manifest")) <= resource_uris,
                        "tools": len(tool_names),
                        "resources": len(resource_uris),
                        "rendered": rendered,
                    }

        try:
            return _result(asyncio.run(probe()))
        except Exception as exc:
            return _result(
                {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
    if args.command == "down":
        return _result(registry.down())
    if args.command == "install-codex-mcp":
        return _result(
            registry.install_codex_mcp(
                server_name=args.name,
                codex_bin=args.codex_bin,
                command=args.executable,
                project=args.workspace or None,
                dry_run=args.dry_run,
            )
        )
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
