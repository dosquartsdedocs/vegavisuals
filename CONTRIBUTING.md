# Contributing

`vegavisuals` accepts focused fixes and compatibility updates that preserve its
reproducible rendering and fail-closed publication contracts.

## Development Environment

Development requires Linux, Python 3.10 or newer, Docker, GNU Make, and network
access for the initial dependency and renderer build.

```bash
python3 -m pip install -e '.[mcp,dev]'
make tests
```

## Required Checks

Run these checks before opening a pull request:

```bash
make tests
make tests-install
docker info
make docker-smoke
```

Use `make mcp-smoke` when changing the MCP adapter or factory contract. Do not
treat a Docker-unavailable skip as release validation.

## Generated Files

Do not commit caches, virtual environments, `dist/`, or `.tmp/`. The committed
example SVGs and `.vegavisuals.lock.json` are managed fixtures; regenerate them
with `vegavisuals --project . render-all` after a render-contract change rather
than editing them manually.

Inspect `.cache/vegavisuals/replaced/` before deleting the cache when a command
reports a publication or rollback conflict; it preserves displaced inodes for
manual recovery.

Keep the root and packaged Dockerfiles semantically synchronized. Compatibility
profiles, themes, tokens, static factory metadata, and dynamic factory metadata
must remain covered by contract tests.

## Pull Requests

Keep each pull request scoped to one behavior. Include regression tests for
correctness or security fixes and document any observable CLI, MCP, manifest,
lock, compatibility, or renderer-contract change.

Contributions are accepted under the repository's `GPL-3.0-only` license. By
submitting a contribution, you confirm that you have the right to license it on
those terms.
