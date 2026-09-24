# vegavisuals

`vegavisuals` is a reusable Vega-Lite and raw Vega visualization factory. It
ships one central theme registry, one project manifest/lock contract, a stdio
FastMCP adapter, and one Docker renderer based on
`vl-convert-python==1.9.0.post1`. Consumer projects do not need Node,
Chromium, or a host installation of Vega.

The host CLI requires Python 3.10 or newer and Linux because publication uses
descriptor-relative I/O, `flock`, and fail-closed `renameat2` operations.
Rendering also requires Docker. Linux x86_64 is the release-tested host;
source installations on other Linux architectures require compatible wheels
for every pinned dependency.

The default compatibility profile is `vl-convert-1.9.0`: Vega 6.2.0,
Vega-Lite 6.4 by default, SVG/PNG/PDF output, deterministic PDF normalization
with qpdf, and the explicitly installed DejaVu font family. The base image is
pinned by registry digest. The full supported Vega-Lite version set and runtime
policy are exposed by `vegavisuals compatibility-status`; the source data is in
[`src/vegavisuals/assets/compat/vl-convert-1.9.0.json`](src/vegavisuals/assets/compat/vl-convert-1.9.0.json).

## Quick Start

```bash
git clone https://github.com/dosquartsdedocs/vegavisuals.git
cd vegavisuals
python3 -m pip install '.[mcp]'
vegavisuals build-renderer

vegavisuals --project /path/to/consumer validate charts/summary.vl.json
vegavisuals --project /path/to/consumer render \
  charts/summary.vl.json public/summary.svg
vegavisuals --project /path/to/consumer render-all
vegavisuals --project /path/to/consumer check
```

The first renderer build needs access to Debian and PyPI repositories. Render
containers themselves run without network access and never pull images.
No prebuilt renderer image is published; each installation builds its local
image from the licensed source package and pinned compatibility profile.

The checkout also exposes a self-contained consumer lifecycle. It creates a
content-addressed, checkout-bound MCP environment under the factory, pins
`mcp==1.29.0`, and never writes tooling into the consumer. Concurrent cold
starts serialize setup with a factory-local lock and publish the completion
stamp only after the CLI is ready:

```bash
make mcp-build
make mcp-init PROJECT=/path/to/consumer
make mcp-check
MCP_CONSUMER_WORKSPACE=/path/to/consumer make --no-print-directory mcp-stdio
```

`mcp-init` creates an empty `.vegavisuals.yml` and the generated cache tree. It
preserves an existing manifest unless `vegavisuals init --force` is requested.
Build, factory check, and smoke preparation do not require a consumer project;
initialization, serving, rendering, and project cleanup always require one.

In a Git consumer, cover `.cache/vegavisuals/` with an ignore rule and add the
created `.vegavisuals.yml` to the index after reviewing it. Initialization does
not edit `.gitignore` or stage files. See [Workspace Path Policies](#workspace-path-policies)
for the read-only workspace contract and the consumer-owned Git choices.

The two repository examples cover a Vega-Lite bar chart with project-local CSV
and a raw Vega chart:

```bash
vegavisuals render examples/vega-lite/bar.vl.json dist/examples/bar.svg
vegavisuals render examples/vega/raw.vg.json dist/examples/raw-vega.svg
```

## Rendering Boundary

Every render uses a fixed worker entrypoint in the image. The host registry:

- Parses JSON itself and rejects duplicate keys and non-finite numbers.
- Confines source, data, input, manifest, cache, lock, and output paths to the consumer root.
- Publishes cache, lock, and output files through descriptor-relative, no-follow Linux operations.
- Serializes final commits with a project file lock, conditionally exchanges exact file snapshots with `renameat2`, and rolls output back if lock publication fails.
- Atomically moves every retired publication inode under the mode-`0700` `.cache/vegavisuals/replaced/` directory, so late writes through an already-open descriptor remain recoverable until explicit cache cleanup.
- Rejects HTTP/HTTPS data, image, hyperlink, and dynamic URL dependencies.
- Resolves local data relative to the consumer root and fingerprints every dependency.
- Never mounts the consumer project into the renderer.
- Mounts only a prepared spec and staged output in an isolated host temporary directory at `/output:rw`.
- Runs Docker with `--network none`, `--read-only`, all capabilities dropped, `no-new-privileges`, a non-root UID/GID, CPU/memory/PID/file limits, and a bounded tmpfs. Root callers use `65534:65534`.
- Labels every renderer container with the factory and a stable hash of the canonical consumer root so cleanup can stay project-scoped.
- Validates PNG chunks and CRCs, normalized PDF structure, recursive SVG safety, and output size.
- Copies the validated artifact to a temporary sibling and atomically replaces the destination from the host.

The container cannot publish directly into the consumer project. Failed renders
leave an existing destination untouched.

Recovery archives are generated cache data and are never removed automatically.
Inspect them after a reported publication conflict; `make clean` or manual cache
removal is the explicit point at which they are discarded.
The project lock, managed outputs, and `.cache/vegavisuals/replaced/` must
reside on the same filesystem so that publication and recovery remain atomic.

## Source And Data Policy

Automatic engine selection first uses exact `.vl.json` and `.vg.json` suffixes,
then a recognized `$schema`, and finally Vega-Lite `mark` or raw Vega `marks`
structure. Explicit `--engine vega-lite` or `--engine vega` also works for JSON
sources; a recognized suffix may not contradict the explicit engine.

File sources may use a static project-root-relative `data.url`. It must resolve
to a UTF-8 regular file inside the project and is staged as raw inline `values`
with its declared or inferred CSV, TSV, or JSON format. This avoids `file:`
loader ambiguity while preserving Vega's own format parser. Symlink and `..`
escapes are rejected. HTTP, HTTPS,
protocol-relative, `file:`, `data:`, and dynamic data URLs are rejected. Image
and hyperlink URL channels are also rejected so published SVG remains offline.

`render-text` applies the same dependency policy: every dependency `url` or
`href` key is rejected, so only inline values
are accepted. Input text is limited to 1 MiB. Its cache key includes source,
engine, format, profile, and theme.

## Project Manifest

`.vegavisuals.yml` is a versioned, explicit project contract:

```yaml
version: 1
profile: vl-convert-1.9.0
family: benizar
inputs:
  - charts/data/shared.csv
visualizations:
  - name: quarterly-bars
    source: charts/quarterly.vl.json
    output: public/quarterly.svg
    engine: vega-lite
    format: svg
    inputs:
      - charts/data/quarterly.csv
  - name: raw-overview
    source: charts/overview.vg.json
    output: public/overview.pdf
```

`engine`, `format`, and both top-level and per-visualization `inputs` are
optional. Inputs supplement data files discovered from the spec and participate
in the fingerprint.

On success, `check` and `visualization_check` atomically publish the bounded
unaltraweb companion receipt at `.unaltraweb/receipts/vegavisuals.json`. It
records the current package/release contract, the length-prefixed request hash,
all explicit and local `data.url` input hashes, and the exact manifest artifact
hashes. A failed check invalidates any prior owned receipt.

`.vegavisuals.lock.json` uses lock version 2. Each entry strictly records the
source, output, engine, selected Vega-Lite version, format, profile, family,
complete render fingerprint, output SHA-256, inputs, and immutable renderer
image provenance. `status` reports these states:

The portable fingerprint uses the renderer contract, not the local Docker image
ID: clean builds can have different image metadata IDs while using identical
pinned inputs. The observed image ID remains recorded as provenance, and the
image must carry the matching renderer-contract label before it can render.

| State | Meaning |
| --- | --- |
| `fresh` | Fingerprint and managed output hash both match. |
| `stale` | Inputs or render contract changed; the unmodified managed output may be replaced. |
| `missing` | No output exists; the first render may create it. |
| `unmanaged` | An output exists without a matching lock entry. |
| `modified` | A managed output changed after rendering. |
| `invalid` | Per-visualization source, dependency, or policy validation failed. |

Fresh outputs are skipped unless `--force` is passed. Existing unmanaged and
modified outputs are never replaced unless `--replace` is also passed. The
same publication rule applies to direct file renders and explicit outputs from
`render-text`.

An invalid manifest or lock aborts `status` and `check` instead of producing a
per-visualization `invalid` state.

## CLI

### Optional artifact bundles

After rendering, opt in to a complete portable bundle for one fresh managed
output:

```bash
vegavisuals --project /path/to/consumer export-bundle \
  public/summary.svg provenance/summary-v1
vegavisuals --project /path/to/consumer check-bundle \
  provenance/summary-v1/bundle.json --sha256 HASH_RETURNED_BY_EXPORT
```

The bundle retains exact sources (including inline text), local data, effective
options, theme/profile resources, renderer provenance, outputs and selected
author edits. Native locks and receipts retain their original semantics. The
first export prepares ignored private staging; the completed bundle is durable
and never automatically cleaned. See [artifact handoff v1](docs/artifact-handoff-v1.md)
for CLI/MCP arguments, verification, relocation and recovery, and the
[0.4.0 release/pin handoff](docs/release-handoff-v0.4.0.md) for adoption gates.

### Commands

JSON-producing operational commands return structured JSON. Their errors also
return JSON and a nonzero status. Help and `--version` use normal CLI text, and
`mcp serve` speaks the MCP stdio transport rather than JSON command output.

```text
vegavisuals [--project ROOT] version
vegavisuals [--project ROOT] profile-inventory
vegavisuals [--project ROOT] theme-inventory [--family FAMILY]
vegavisuals [--project ROOT] compatibility-status [--profile PROFILE]
vegavisuals [--project ROOT] factory-check [--profile PROFILE] [--family FAMILY]
vegavisuals [--project ROOT] init [--force]
vegavisuals [--project ROOT] install-check [--command EXECUTABLE]
vegavisuals [--project ROOT] factory-lifecycle-check [--command EXECUTABLE]
vegavisuals [--project ROOT] lifecycle-check [--command EXECUTABLE]
vegavisuals [--project ROOT] install-codex-mcp [--dry-run]
vegavisuals [--project ROOT] release-status [--release TAG]
vegavisuals [--project ROOT] update [--dry-run]
vegavisuals [--project ROOT] validate SOURCE [--engine auto|vega-lite|vega] [--input PATH]
vegavisuals [--project ROOT] render SOURCE OUTPUT [--format svg|png|pdf] [--name NAME]
vegavisuals [--project ROOT] render-text [--text JSON] [--output PATH]
vegavisuals [--project ROOT] export-bundle OUTPUT BUNDLE_DIR [--text JSON] [--manifest PATH] [--edited-output PATH] [--dry-run]
vegavisuals [--project ROOT] check-bundle BUNDLE_MANIFEST --sha256 HASH
vegavisuals [--project ROOT] status [--manifest .vegavisuals.yml]
vegavisuals [--project ROOT] check [--manifest .vegavisuals.yml]
vegavisuals [--project ROOT] render-all [--manifest .vegavisuals.yml]
vegavisuals [--project ROOT] factory-manifest
vegavisuals [--project ROOT] build-renderer [--profile PROFILE]
vegavisuals [--project ROOT] ensure-renderer [--profile PROFILE]
vegavisuals --project ROOT down
vegavisuals down-all
vegavisuals [--project ROOT] mcp serve
vegavisuals [--project ROOT] mcp client-config
vegavisuals [--project ROOT] mcp list-tools
```

Contract-aware commands also accept the documented `--profile`, `--family`,
input, manifest, and publication-policy options. Run
`vegavisuals COMMAND --help` for the complete synopsis.

`render`, `render-text`, and `render-all` accept `--include-data`, `--replace`,
`--force`, and `--dry-run`. Inline artifact data is omitted by default. When
requested, SVG is returned as `artifact.svg`; PNG and PDF are returned as
`artifact.data_base64`. The compatibility profile limits artifact and response
sizes.

`validate` performs strict JSON, depth, numeric, schema-version, URL-policy, and
basic Vega/Vega-Lite structural checks. It does not claim complete JSON Schema
or compiler validation; the pinned worker remains authoritative for full
renderer semantics.

## Python API

The public package exports `Registry`, `__version__`, and the typed exception
hierarchy. One `Registry` instance fixes the consumer root:

```python
from vegavisuals import Registry

registry = Registry("/path/to/consumer")
registry.validate_visualization("charts/chart.vl.json")
registry.render_visualization("charts/chart.vl.json", "public/chart.svg")
registry.render_visualization_text(spec_json, output_format="png")
bundle = registry.export_visualization_bundle("public/chart.svg", "provenance/chart-v1")
registry.check_visualization_bundle(bundle["path"], bundle["sha256"])
registry.visualization_status()
registry.visualization_check()
registry.render_visualizations()
registry.initialize_project()
registry.theme_inventory()
registry.compatibility_status()
registry.install_check()
registry.release_status()
registry.factory_manifest()
```

Renderer lifecycle methods are `build_renderer()` and `ensure_renderer()`.
Inventory helpers are `profile_inventory()`, `factory_check()`, and
`version_status()`.

## MCP

The consumer root is resolved once before the FastMCP server starts and is not
an MCP tool argument. Factory-generated stdio transports provide it through
`MCP_CONSUMER_WORKSPACE`:

```bash
vegavisuals --project /path/to/consumer mcp serve
MCP_CONSUMER_WORKSPACE=/path/to/consumer vegavisuals mcp serve
```

Tools:

```text
initialize_project
validate_visualization
render_visualization
render_visualization_text
export_visualization_bundle
check_visualization_bundle
visualization_status
visualization_check
render_visualizations
theme_inventory
compatibility_status
factory_check
release_status
update
factory_manifest
```

Resources:

```text
vegavisuals://agent-guide
vegavisuals://themes
vegavisuals://compatibility
vegavisuals://project/status
vegavisuals://project/check
vegavisuals://factory/check
vegavisuals://release
vegavisuals://factory-manifest
```

MCP tools preserve the documented dictionary result contract. Expected policy,
validation, and render failures are typed application results with `ok: false`
rather than MCP transport errors; clients must inspect `ok`.

Generate a client configuration template with:

```bash
vegavisuals mcp client-config --workspace-placeholder '${workspaceFolder}'
```

The default configuration launches `-m vegavisuals.cli` with the exact Python
interpreter running the installed CLI, so it does not depend on the client
`PATH`. It places the literal `${workspaceFolder}` placeholder in the transport
environment rather than in a shell or Make expression. Replace it with an
absolute consumer path when the client does not perform that expansion. Use
`--command /absolute/path/to/vegavisuals` to select an explicit launcher, and
`--format vscode-workspace` for VS Code's workspace shape.
`vegavisuals install-codex-mcp --workspace /absolute/root` preserves an
identical command and environment registration, adds a missing one, and refuses
to replace a different registration; inspect the commands first with
`--dry-run`.

The MCP `update` tool is deliberately non-mutating and returns the explicit
update command. An operator can run `vegavisuals update` directly to
fast-forward a clean checkout; installed wheels only report their explicit pip
upgrade.

`mcp-factory.yml` is the checkout discovery contract. Wheels and sdists carry a
separate package-native manifest that invokes the installed CLI directly and
provides `tests`, `smoke`, and project-scoped `down` without Make or checkout paths. Dynamic
metadata from `vegavisuals factory-manifest` uses the active Python interpreter
while preserving the same lifecycle contract. gContExt checkout commands omit
`${workspaceFolder}` for factory-only operations and pass it only to project
operations. `down` removes containers carrying both the factory and selected
workspace labels. The maintainer-only `down-all` CLI/Make operation remains an
explicit emergency command and is deliberately excluded from discovery lifecycle
commands so one IDE session cannot stop another.

## Workspace Path Policies

Checkout, packaged, and dynamic factory manifests share the following literal
`workspace_rule.path_policies`, retaining `schema_version: 1`, `binding: consumer`,
and `consumer_root: .`:

| Path | Type | Role | Git | Cleanup |
| --- | --- | --- | --- | --- |
| `.cache/vegavisuals` | directory | `render-cache-and-publication-recovery` | `ignored` | `explicit` |
| `.vegavisuals.yml` | file | `visualization-source-manifest` | `versioned` | `never` |
| `.vegavisuals.lock.json` | file | `managed-output-provenance` | `consumer` | `explicit` |
| `.unaltraweb/receipts/vegavisuals.json` | file | `companion-freshness-receipt` | `consumer` | `explicit` |

- **Cache and recovery:** this checkout's `.gitignore` covers the directory via
  `.cache/`, and none of its contents are tracked. Consumers must likewise ignore
  it, directly or through an ancestor rule. Inline render artifacts and metadata
  can be rebuilt, but the directory also holds the publication lock and
  `replaced/` recovery archives. Archives can contain late user edits through
  displaced file descriptors, so the whole directory is **not disposable**.
  Stop active operations and inspect recovery data before explicit cleanup.
  The checkout's MCP environments also live here; they are factory-local tooling.
- **Source manifest:** the existing project contract is versioned source, as
  demonstrated by this checkout's tracked `.vegavisuals.yml`. Keep it in the
  consumer index when present and never treat it as cleanup data. `init` creates
  or preserves it without staging it; a newly created, untracked manifest needs
  an explicit consumer `git add`. An absent default manifest is allowed by the
  path policy (direct rendering and custom `--manifest` paths remain supported).
  This is a Git workspace requirement, not a new prerequisite for the Git-free
  rendering API.
- **Output provenance lock:** the renderer publishes it with managed outputs;
  losing it makes existing outputs unmanaged and requires replacement
  confirmation. This checkout versions it with the example SVG fixtures, but
  that fixture choice is not a universal consumer Git requirement. Consumers
  decide whether to version or ignore their lock, retaining it alongside outputs
  whose management history they need. Cleanup therefore remains explicit.
- **Companion receipt:** a successful `check` publishes it and a failed check
  invalidates the previous receipt. It is provider-written freshness evidence,
  not authored source. This checkout ignores the exact file; initialization does
  not impose that ignore rule on consumers. Its Git lifecycle stays consumer-managed,
  and explicit removal requires a subsequent successful check to restore the
  evidence needed by companion workflows. `consumer` does not transfer receipt
  content ownership or change its existing publication/invalidation behavior.

`ignored` requires a matching Git ignore rule and no tracked content, including
force-added files beneath an ignored directory. `versioned` requires an existing
path to be non-ignored and present in the index; absence is allowed. `consumer`
reports Git state without imposing a tracking choice. Path type and confinement
checks still apply to every entry. `cleanup` is descriptive metadata, **never
authorization to delete**. Neither manager `workspace-check` nor provider `down`
removes consumer paths; `down` only removes matching labelled containers.

`generated_paths` describes origins, not ignore or cleanup policy. Dynamic
sources, inputs, and outputs remain governed by `.vegavisuals.yml` and the render
publication contract. Their paths vary by project, as do direct-render output
arguments and custom manifest paths, so they are outside this first literal-path
adoption. No output glob, placeholder, example-specific `examples/rendered/`,
or assumed `dist/` destination is declared. Individual content-addressed cache
files and recovery names are covered by their literal parent directory.

The central factory manager's `workspace-check` only reads factory metadata and
consumer filesystem/Git state; it does not run provider commands. This differs
from `vegavisuals check`, which checks visualization freshness and writes a receipt.

## Verification

```bash
python3 -m pip install -e '.[mcp,dev]'
make check
make tests
make tests-install
make mcp-check
make docker-smoke
make mcp-smoke
```

`make tests` keeps Docker mocked. `make docker-smoke` renders all formats for
both engines and checks delayed PDF byte repeatability. `make mcp-smoke` calls
both render engines through stdio. Wheel verification installs non-editably,
resolves assets from site-packages, and invokes both real render engines through
the installed-wheel MCP executable.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution checks and
[`SECURITY.md`](SECURITY.md) for supported versions and private vulnerability
reporting.

## License

`vegavisuals` is licensed under the GNU General Public License v3.0 only
(`GPL-3.0-only`). Vega, Vega-Lite, `vl-convert`, and the other runtime
dependencies retain their original licenses; see `THIRD_PARTY_NOTICES.md`.
Copyright (C) 2026 dosquartsdedocs.

Invoking the standalone CLI, Docker renderer, or MCP server does not by itself
change the license of a consumer project or of generated SVG, PNG, and PDF
artifacts. Applications that copy, modify, link, or directly distribute the
Python package must comply with the GPLv3 terms.
