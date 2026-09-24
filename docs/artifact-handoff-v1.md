# Artifact handoff v1

Vegavisuals 0.4.0 adds an **opt-in leaf producer** for one already rendered,
fresh Vega-Lite or raw Vega product. The same `Registry` implements the Python,
CLI and MCP APIs. Ordinary `render`, `render-text`, `render-all`, `status` and
`check` retain their native behavior and formats.

## Export and verification

```bash
vegavisuals --project /absolute/consumer render charts/quarter.vl.json out/quarter.svg
vegavisuals --project /absolute/consumer export-bundle \
  out/quarter.svg provenance/quarter-v1 --dry-run
vegavisuals --project /absolute/consumer export-bundle \
  out/quarter.svg provenance/quarter-v1 --edited-output out/quarter.edited.svg
vegavisuals --project /absolute/consumer check-bundle \
  provenance/quarter-v1/bundle.json --sha256 HASH_RETURNED_BY_EXPORT
```

MCP tools are `export_visualization_bundle(output_path, bundle_path,
visualization_text="", manifest_path="", edited_output_path="",
dry_run=False)` and `check_visualization_bundle(bundle_path, sha256)`.
The Python API uses `None` for omitted optional strings. MCP uses plain string
annotations so the pinned FastMCP adapter preserves JSON specification text
verbatim instead of pre-decoding an optional-string union.
The startup consumer root is fixed; all arguments are normalized relative POSIX
paths. Export's `bundle_path` is a **new directory**; check's `bundle_path` is the
returned **manifest path**. A successful export returns `ok`, `schema_version`,
`path`, `sha256`, `verified_files` and `domain: vegavisuals-leaf-v1`. The hash
covers the exact UTF-8 `bundle.json` bytes, including its newline. A dry run
returns the proposed identity without sealing a directory.

Render first, then export. Export never invokes Docker, pulls an image or silently
rerenders a stale or modified product. It requires the exact managed output path
in lock v2, or a verified content-addressed inline cache v2 entry. It uses the
**actual image ID in that native entry**, even when a different compatible local
image is now installed. A copied native lock is not an integration record for a
moved output. A new export path is required for each sealed version; an existing
directory is never merged or replaced.

For inline rendering, pass the **exact original text**, including whitespace,
back to the exporter. Both explicit managed outputs and cache-only artifacts are
supported. CLI `--text -` reads at most 1 MiB from stdin. A digest alone cannot
recover an inline specification. The URL-free inline rule is unchanged.

For manifest rendering, the default `.vegavisuals.yml` is discovered when it
exists; `--manifest PATH` selects a custom manifest. Its selected visualization
must reproduce the native fingerprint. Named file renders require their
manifest selection. The complete original manifest is opaque evidence, and a
bounded one-visualization projection records effective options and inputs. The
request identifies the original/projection relationship explicitly.

## Retained product

The sealed tree contains only `bundle.json` and inventoried files beneath
`payload/`. This producer exports no child dependencies. It retains:

- Exact original specification bytes, or materialized original inline text.
- All discovered local `data.url` files and all declared inputs, including
  top-level and selected manifest inputs; missing or changed inputs fail export.
- A provider-defined effective request: engine, Vega-Lite version, output format,
  theme family, profile, complete inputs, fingerprint, renderer and selection.
- The exact host-prepared specification with local data inlined, separately from
  its original. Verification reconstructs it from the retained inputs.
- Profile, theme(s), tokens, Dockerfile and worker bytes. All themes participating
  in the renderer contract are retained, not only the selected theme.
- The validated SVG, PNG or PDF, plus an explicitly selected author-owned variant
  of the same format with `variant_of: output`. No `.edited` file is auto-selected.
- A content-addressed inventory of the installed producer's Python/package files
  and resources. `producer.revision` is `sha256:` of these exact inventory bytes;
  it never falsely labels a dirty checkout as its HEAD commit. Each export also
  pins the original renderer image ID as the `vl-convert` runtime revision and
  retains the base-image digest and renderer-contract hash.
- Native lock/cache metadata and any present companion receipt, **byte-identical
  and opaque**. These snapshots are historical evidence, not a freshness claim at
  the eventual integration destination.

`payload/project/` preserves the original project-relative source/data layout.
Local URLs are resolved against that retained project root, not against the
source's containing directory. `payload/effective-manifest.json` is interpreted
with that root; its output path describes a future render, while the sealed
delivered artifact is `payload/output/visualization.FORMAT`. To reproduce, copy
the retained project inputs to a separate working directory and render with the
recorded released producer/runtime. Never render into the sealed archive.

The checker validates exact inventories and hashes, reference completeness,
prepared-spec equivalence, selected resource/producer identities, renderer
contract and SVG/PNG/PDF domain safety. It is read-only and works after the
producer job is removed and the consumer relocated. It supports this factory's
leaf format only; composite/foreign bundles and upstream `from` relationships
require an integrator's v1 verifier. Remote, dynamic, image and hyperlink URL
dependencies remain unsupported. Arbitrary executable input pipelines are not
run or inferred by the exporter.

## Publication and retention

Export holds the existing project coordination lock throughout capture, staging
and publication, and rechecks original bytes/identities before and after the
commit. It never rewrites a native output, lock or receipt. Inline rendering now
coordinates cache artifact, cache metadata, explicit output and lock as one
rollback transaction. Concurrent changes are rejected; displaced native inodes
remain in the existing `replaced/` recovery archive.

The first real export prepares mode-0700 `.cache/vegavisuals/handoff/`. In a Git
consumer it verifies that the cache contains no tracked files and has effective
ignore coverage. When coverage is missing, it appends a narrowly scoped
`/.cache/vegavisuals/` rule to `.gitignore` using compare-against-snapshot
publication, preserving custom bytes. Ordinary initialization still leaves Git
choices alone. No new literal `path_policies` entry is needed: the work area is
covered by the existing cache/recovery policy. Bundle destinations are chosen by
the caller outside `.cache` and `.unaltraweb`; retain and review them as durable
provenance with the sources.

The private candidate is fully sealed, fsynced and domain-checked, then published
with descriptor-relative `renameat2(RENAME_NOREPLACE)` on the same filesystem.
Reads reject links (including hardlinks and workspace-ancestor symlinks), special
files, aliases and escapes. The shared v1 limits apply: 1 MiB manifest, 512 MiB
per file, 2 GiB cumulative inspected bytes, 10,000 entries, 64 path components,
1,024 path characters and bounded strict JSON. Renderer profile limits remain
in force for rendering.

An exception/interruption keeps the candidate at the reported random `.pending`
path. A failed post-publication check moves the same installed directory back
to recovery when possible, preserving all bytes, including concurrent edits.
If rollback cannot safely recover it, the error identifies its retained final
path. A process killed after the single directory rename can leave a complete
sealed tree; verify it against the expected identity before explicitly adopting
it. Partial candidates never count as completed bundles. Retry with a new path,
or manually copy and verify a complete recovery tree after reviewing the cause.
Recovery directories and sealed bundles are never automatically deleted.

## Integrator responsibilities

The client/agent carries the returned manifest path/hash to the integrator.
Cross-workspace transport copies the **whole sealed directory**, keeping manifest
bytes unchanged. The integrator verifies it, retains the tree durably, selects
exact file mappings, updates content references and publishes a recoverable v1
integration record. Keep originals and selected author edits together. Generate
fresh provider-native locks/receipts at new managed paths through Vegavisuals;
never edit retained evidence into a destination receipt. Runtime teardown and
explicit job cleanup follow verified integration, not export alone.

## Independent conformance reference

The shared schema/verifier/fixtures are pinned to central implementation
`9167e3efb5968a64bb9100792163a179c1491860`, merged by
`fc8745db950b04013c73eb49acf6781a26eb83f8`. They are not runtime dependencies.

```bash
VEGAVISUALS_HANDOFF_REFERENCE=/absolute/my-scripts-factory make tests
make docker-smoke REQUIRE_DOCKER=1
make tests-install
make mcp-build
make mcp-check
make mcp-smoke REQUIRE_DOCKER=1
```

The optional reference test extracts only blobs from the pinned Git revision
into a temporary fixture directory. It verifies the reviewed figure/deck
fixtures, checks adapter-produced bundles with the independent verifier,
retires the test producer and checks relocation and missing-data failures.
Ordinary unit tests have no sibling dependency and mock Docker. Real Docker
smoke exports both engines with local data, retires their producer job, verifies
relocation and reproduces the SVG bytes from retained sources. MCP smoke also
covers both engines' file and inline export using the installed wheel.
