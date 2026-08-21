from __future__ import annotations

from typing import Any

from .errors import VegavisualsError
from .registry import DEFAULT_FAMILY, DEFAULT_PROFILE, MANIFEST_NAME, Registry, json_dumps


TOOL_NAMES = (
    "validate_visualization",
    "render_visualization",
    "render_visualization_text",
    "visualization_status",
    "visualization_check",
    "render_visualizations",
    "theme_inventory",
    "compatibility_status",
    "factory_manifest",
)

RESOURCE_URIS = (
    "vegavisuals://agent-guide",
    "vegavisuals://themes",
    "vegavisuals://compatibility",
    "vegavisuals://project/status",
    "vegavisuals://project/check",
    "vegavisuals://factory-manifest",
)


def _safe(call: Any) -> dict[str, Any]:
    try:
        return call()
    except (VegavisualsError, OSError, ValueError) as exc:
        return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}


def create_server(registry: Registry, fastmcp_class: Any | None = None) -> Any:
    if fastmcp_class is None:
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SystemExit(
                "The MCP server requires the optional dependency: pip install 'vegavisuals[mcp]'"
            ) from exc
        fastmcp_class = FastMCP

    mcp = fastmcp_class("vegavisuals")

    @mcp.resource("vegavisuals://agent-guide")
    def agent_guide() -> str:
        """Usage and safety guidance shipped with the installed package."""
        path = registry.assets / "AGENTS.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    @mcp.resource("vegavisuals://themes")
    def themes_resource() -> str:
        """Theme and design-token inventory."""
        return json_dumps(_safe(registry.theme_inventory))

    @mcp.resource("vegavisuals://compatibility")
    def compatibility_resource() -> str:
        """Pinned renderer compatibility profile."""
        return json_dumps(_safe(registry.compatibility_status))

    @mcp.resource("vegavisuals://project/status")
    def project_status_resource() -> str:
        """Current manifest and lock status for the startup consumer root."""
        return json_dumps(_safe(registry.visualization_status))

    @mcp.resource("vegavisuals://project/check")
    def project_check_resource() -> str:
        """Strict freshness check for the startup consumer root."""
        return json_dumps(_safe(registry.visualization_check))

    @mcp.resource("vegavisuals://factory-manifest")
    def factory_manifest_resource() -> str:
        """Factory discovery contract."""
        return json_dumps(registry.factory_manifest())

    @mcp.tool()
    def validate_visualization(
        source_path: str,
        engine: str = "auto",
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
        inputs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Validate a project-confined Vega-Lite or Vega source and its local data dependencies."""
        return _safe(
            lambda: registry.validate_visualization(
                source_path,
                engine=engine,
                profile=profile,
                family=family,
                inputs=inputs or [],
            )
        )

    @mcp.tool()
    def render_visualization(
        source_path: str,
        output_path: str,
        engine: str = "auto",
        output_format: str = "",
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
        inputs: list[str] | None = None,
        include_data: bool = False,
        confirm_replace: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Render one project file through the hardened Docker renderer and atomically publish it."""
        return _safe(
            lambda: registry.render_visualization(
                source_path,
                output_path,
                engine=engine,
                output_format=output_format or None,
                profile=profile,
                family=family,
                inputs=inputs or [],
                include_data=include_data,
                confirm_replace=confirm_replace,
                force=force,
                dry_run=dry_run,
            )
        )

    @mcp.tool()
    def render_visualization_text(
        visualization_text: str,
        engine: str = "auto",
        output_format: str = "",
        output_path: str = "",
        profile: str = DEFAULT_PROFILE,
        family: str = DEFAULT_FAMILY,
        include_data: bool = False,
        confirm_replace: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Render inline-only JSON through a content-addressed cache."""
        return _safe(
            lambda: registry.render_visualization_text(
                visualization_text,
                output_path=output_path or None,
                engine=engine,
                output_format=output_format or None,
                profile=profile,
                family=family,
                include_data=include_data,
                confirm_replace=confirm_replace,
                force=force,
                dry_run=dry_run,
            )
        )

    @mcp.tool()
    def visualization_status(manifest_path: str = MANIFEST_NAME) -> dict[str, Any]:
        """Report fresh, stale, missing, modified, and unmanaged manifest outputs."""
        return _safe(lambda: registry.visualization_status(manifest_path))

    @mcp.tool()
    def visualization_check(manifest_path: str = MANIFEST_NAME) -> dict[str, Any]:
        """Require every manifest output to be fresh and managed."""
        return _safe(lambda: registry.visualization_check(manifest_path))

    @mcp.tool()
    def render_visualizations(
        manifest_path: str = MANIFEST_NAME,
        include_data: bool = False,
        confirm_replace: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Render every visualization declared in the project manifest."""
        return _safe(
            lambda: registry.render_visualizations(
                manifest_path,
                include_data=include_data,
                confirm_replace=confirm_replace,
                force=force,
                dry_run=dry_run,
            )
        )

    @mcp.tool()
    def theme_inventory(family: str = "") -> dict[str, Any]:
        """List central theme families and their packaged token assets."""
        return _safe(lambda: registry.theme_inventory(family))

    @mcp.tool()
    def compatibility_status(profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
        """Inspect the data-parsed renderer compatibility profile."""
        return _safe(lambda: registry.compatibility_status(profile))

    @mcp.tool()
    def factory_manifest() -> dict[str, Any]:
        """Return the factory discovery manifest."""
        return registry.factory_manifest()

    return mcp


def run_server(registry: Registry) -> None:
    server = create_server(registry)
    server.run(transport="stdio")
