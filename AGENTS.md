# Notes for agents

This repository is the shared Vega-Lite and Vega theme/renderer factory. Keep
consumer-specific chart generation and document pipelines in consumer projects.
Keep renderer, policy, profile, theme, manifest, lock, CLI, and MCP behavior
central here.

## Safety contract

- Never make the consumer project writable inside the renderer container.
- Keep runtime networking disabled and reject remote data before Docker runs.
- Resolve every caller-controlled path against the startup consumer root.
- Render only into an isolated host staging directory, validate there, then publish with descriptor-relative no-follow operations from the host.
- Serialize final output/cache/lock commits, recheck state, and roll back output if lock publication fails.
- Never replace unmanaged or user-modified output without explicit confirmation.
- Keep inline rendering URL-free and content-addressed.
- Parse compatibility profiles as JSON data. Never source profiles in a shell.
- Keep CLI and MCP behavior behind the same `Registry` implementation.
- Keep package resources usable from a non-editable wheel installation.

## Development

Run `make check` and `make tests` after host-side changes. Unit tests must mock
Docker execution. Run `make docker-smoke` for renderer, theme, font, or worker
changes; it exercises both Vega-Lite and raw Vega when Docker is available.
Run `make tests-install` after packaging changes and `make mcp-smoke` after MCP
contract changes.

Do not commit generated cache files, rendered distribution outputs, virtual
environments, or renderer containers.

## Factory lifecycle

- `make mcp-build` bootstraps the pinned private MCP environment and renderer image without a consumer project.
- `make mcp-init PROJECT=/consumer/root` initializes the consumer idempotently.
- `make mcp-check` validates the installation and factory without a consumer project.
- `make mcp-stdio PROJECT=/consumer/root` fixes that root and serves stdio.
- `make mcp-smoke` checks protocol discovery and both render engines.
- `make mcp-down PROJECT=/consumer/root` removes only that consumer's labelled renderer containers.
- `make mcp-down-all` is the explicit emergency cleanup for renderer containers from every consumer.
