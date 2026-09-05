from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

from yaml import safe_load

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


@unittest.skipUnless(
    os.environ.get("VEGAVISUALS_MCP_SMOKE") == "1" and importlib.util.find_spec("mcp") is not None,
    "set VEGAVISUALS_MCP_SMOKE=1 in an environment with the MCP dependency",
)
class MCPStdioSmokeTest(unittest.TestCase):
    def test_stdio_contract(self) -> None:
        async def smoke() -> None:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            with tempfile.TemporaryDirectory() as temporary:
                project = pathlib.Path(temporary) / (
                    "consumer $dollar $(shell printf make-expanded) `printf tick-expanded`"
                )
                project.mkdir()
                shutil.copytree(REPO_ROOT / "examples", project / "examples")
                executable = os.environ.get("VEGAVISUALS_MCP_COMMAND")
                arguments_json = os.environ.get("VEGAVISUALS_MCP_ARGS_JSON")
                factory_root = os.environ.get("VEGAVISUALS_MCP_FACTORY_ROOT")
                environment = dict(os.environ)
                environment["MCP_CONSUMER_WORKSPACE"] = str(project)
                if factory_root:
                    environment.pop("PYTHONPATH", None)
                    factory = pathlib.Path(factory_root).resolve()
                    manifest = safe_load((factory / "mcp-factory.yml").read_text(encoding="utf-8"))
                    transport = manifest["transport"]

                    def expand(value: object) -> str:
                        return str(value).replace("${factoryRoot}", str(factory)).replace(
                            "${workspaceFolder}", str(project)
                        )

                    transport_command = [expand(value) for value in transport["command"]]
                    command, *args = transport_command
                    environment.update({str(key): expand(value) for key, value in transport.get("env", {}).items()})
                elif executable and arguments_json:
                    environment.pop("PYTHONPATH", None)
                    command = executable
                    args = [
                        str(value).replace("{project}", str(project))
                        for value in json.loads(arguments_json)
                    ]
                elif executable:
                    environment.pop("PYTHONPATH", None)
                    command = executable
                    args = ["mcp", "serve"]
                else:
                    command = sys.executable
                    args = [
                        "-m",
                        "vegavisuals.cli",
                        "mcp",
                        "serve",
                    ]
                    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
                parameters = StdioServerParameters(command=command, args=args, env=environment)
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        self.assertIn("render_visualization", names)
                        self.assertIn("render_visualization_text", names)
                        self.assertIn("visualization_status", names)
                        self.assertIn("initialize_project", names)
                        self.assertIn("factory_check", names)
                        self.assertIn("release_status", names)
                        resources = await session.list_resources()
                        uris = {str(resource.uri) for resource in resources.resources}
                        self.assertIn("vegavisuals://themes", uris)
                        self.assertIn("vegavisuals://factory/check", uris)
                        self.assertIn("vegavisuals://release", uris)
                        self.assertIn("vegavisuals://factory-manifest", uris)
                        initialized = await session.call_tool("initialize_project", {})
                        initialized_payload = json.loads(
                            "\n".join(getattr(item, "text", "") for item in initialized.content)
                        )
                        self.assertTrue(initialized_payload["ok"], initialized_payload)
                        self.assertTrue((project / ".vegavisuals.yml").is_file())
                        result = await session.call_tool("theme_inventory", {})
                        text = "\n".join(getattr(item, "text", "") for item in result.content)
                        self.assertIn("benizar", text)

                        for source, output, engine in (
                            ("examples/vega-lite/bar.vl.json", "out/mcp-vega-lite.svg", "vega-lite"),
                            ("examples/vega/raw.vg.json", "out/mcp-vega.svg", "vega"),
                        ):
                            rendered = await session.call_tool(
                                "render_visualization",
                                {"source_path": source, "output_path": output},
                            )
                            payload = json.loads("\n".join(getattr(item, "text", "") for item in rendered.content))
                            self.assertFalse(rendered.isError)
                            self.assertTrue(payload["ok"], payload)
                            self.assertEqual(payload["engine"], engine)
                            self.assertTrue((project / output).is_file())

                        blocked = await session.call_tool(
                            "validate_visualization",
                            {"source_path": "../outside.vg.json"},
                        )
                        blocked_payload = json.loads(
                            "\n".join(getattr(item, "text", "") for item in blocked.content)
                        )
                        self.assertFalse(blocked.isError)
                        self.assertFalse(blocked_payload["ok"])
                        self.assertEqual(blocked_payload["error"]["type"], "PolicyError")

        asyncio.run(smoke())


if __name__ == "__main__":
    unittest.main()
