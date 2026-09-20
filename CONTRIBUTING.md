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

For workspace path contract changes, also run the integration cases against an
explicitly selected checkout of the central manager with `path_policies` support:

```bash
VEGAVISUALS_FACTORY_MANAGER=/absolute/path/to/my-scripts-factory/src/bash/mcp_factories/mcp-factory-manager.py make tests
python3 /absolute/path/to/my-scripts-factory/src/bash/mcp_factories/mcp-factory-manager.py \
  validate --dir /absolute/path/to/factories --factory vegavisuals --json
```

`tests/test_workspace_policies.py` always checks manifest parity, actual Git
ignore/index behavior, initialization, lock/receipt lifecycles, recovery archives,
and container-only `down`. With `VEGAVISUALS_FACTORY_MANAGER` set it also runs the
real manager against temporary Git consumers and the current factory manifest.
It covers direct/ancestor ignore rules, absent/tracked/untracked/ignored paths,
and rejects force-added cache content. Provider-command sentinels must remain
untouched. HEAD, index bytes and metadata, status, worktree registry, and consumer
file bytes/metadata must match before and after every `workspace-check`, including
failing checks. Without the variable only the external-manager integration cases
are skipped; no sibling checkout is required by the ordinary host suite.

Keep these tests compatible with Python 3.10 through 3.14, the declared host
versions. Do not copy or modify the central manager to make an integration pass.

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
