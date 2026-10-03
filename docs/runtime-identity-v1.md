# Runtime identity, native schema 1 (Vegavisuals 0.5.1)

## Interfaces and purpose

- CLI: `python -m vegavisuals.cli [--renderer-image-id ID] [--project ROOT] runtime-identity [--profile PROFILE]`.
- Python: `Registry(root, renderer_image_id=ID).runtime_identity(profile="vl-convert-1.9.0")`.
- MCP tool: `runtime_identity`, optional string argument `profile`, default `vl-convert-1.9.0`.
- MCP resource: `vegavisuals://runtime/identity`, using that default profile.

Call the tool/resource on the **existing MCP connection** to identify the server
that actually answers it. Running the CLI starts a new process and reports that
process; neither the CLI nor disk inspection proves the identity of a previously
launched server. Each `Registry` gets its own instance ID/start time at creation;
the normal stdio server has one startup-bound registry.

The query is diagnostic and read-only. It does not call init, ensure, build,
render, freshness/receipt publication, an installer, a launcher bootstrap or a
remote release API. It does not change a Docker alias or global registration.
Provider metadata is read locally; Docker is called only with `image inspect`.
Source HEAD/refs are read as bounded data, without Git execution, hooks, index
refresh or inherited `GIT_*` redirection. No Docker image preparation is implicit.

An unavailable image/daemon or a mismatch yields `ok: false`, retains the
instance/package fields, and records stable mismatch codes. CLI exits 1 for
such a report. MCP returns the ordinary typed application result (clients must
inspect `ok`, not only transport success); the resource contains the same JSON
shape. Successful CLI reports exit 0. Existing rendering and immutable-selection
APIs retain their 0.5.0 semantics.

## Response fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer **1**. |
| `kind` | Literal `vegavisuals-runtime-identity`. |
| `ok` | True only when no recorded discrepancies/unavailable observations remain. |
| `observed_at` | UTC completion timestamp for this query. Observations are bounded reads, not an atomic filesystem/daemon transaction. |
| `instance.id` | Opaque random 32-hex identifier, fixed for the registry lifetime; changes on a new instance/restart. |
| `instance.started_at` | UTC time of registry creation, not an inferred OS boot time. |
| `instance.pid` | Current answering Python process PID in its PID namespace. |
| `instance.startup_pid` | PID captured at registry creation; a later process change is a mismatch. |
| `interpreter.executable` | `sys.executable` captured at startup. |
| `interpreter.resolved_executable` | Its startup-resolved filesystem path. |
| `interpreter.implementation`, `.version` | Loaded interpreter implementation and exact major/minor/patch version. |
| `workspace.root` | Canonical startup consumer root. It is not re-derived from cwd/environment/source checkout. |
| `workspace.binding_matches_startup` | Whether current root paths still identify the held startup directory descriptor. |
| `package.name` | Literal `vegavisuals`. |
| `package.loaded_version` | Version imported into Python memory, **not** `importlib.metadata.version()` or a reloaded version file. |
| `package.mode` | Startup classification: `installed`, `development` (source checkout/editable), or `unmanaged` when installation provenance cannot be associated with the loaded package. |
| `package.root`, `.source_root` | Loaded package directory and optional detected provider checkout root. |
| `package.startup` | Frozen disk observations captured at registry creation. |
| `package.current_disk` | Fresh observations of the same package and startup-selected distribution search roots. |
| `renderer` | Query-profile and image-inspection fields described below. |
| `mismatches` | List of objects with stable `component` and `code` strings. No arbitrary error text is reflected. |

### Disk observations

Both `package.startup` and `package.current_disk` contain:

| Field | Meaning |
| --- | --- |
| `version_file` | Version parsed from `_version.py` as data; the module is never executed/reloaded. Null when unreadable/invalid. |
| `package_files_sha256` | SHA-256 of the fixed, complete provider package inventory for this release: Python modules, packaged descriptor and renderer resources. Each UTF-8 name and file payload has an 8-byte big-endian length prefix. Bytecode caches are excluded. |
| `distribution` | Whitelisted installer metadata object, or null when absent/unavailable. |
| `source_revision` | Optional local Git HEAD/ref observation. This does not certify a clean source tree or that a commit contains every working-file byte. |
| `errors` | Stable codes for unavailable snapshot components. |

The distribution object contains `metadata_root`, `version`, `metadata_sha256`,
optional `record_sha256` and `direct_url_sha256`, optional `archive_sha256` and
`vcs_commit_id`, and boolean `editable`. The archive/VCS identity comes from
installer-recorded PEP 610 metadata. The archive can be a wheel or source archive;
it is not a reconstructed wheel hash, renderer image ID or registry RepoDigest.
Missing provenance remains null. Origin URLs, requested refs and arbitrary
metadata headers are not returned.

The startup snapshot describes disk bytes **observed at registry creation**,
not an attestation of all Python bytecode in memory. Only `loaded_version` is the
imported version constant. Updating an installation in place can create a mixed
running process; the query diagnoses drift without reloading or pretending that
new disk metadata is already running. H2 must reconnect/restart according to its
own lifecycle and compare the new instance with its verified installation receipt.

Distribution discovery is restricted to the loaded package's parent and the
interpreter's purelib/platlib roots, captured at startup. Consumer cwd and later
`sys.path` entries cannot shadow the selected installation's metadata. Reads are
bounded, regular-file, no-follow reads; the query does not inspect consumer
manifests, data, outputs, locks or receipts.

### Renderer observations

| Field | Meaning |
| --- | --- |
| `profile`, `profile_scope` | Normalized requested profile; scope is **`query`**, not an assertion of a global last-used profile. Rendering profiles remain per-operation options. |
| `profile_sha256` | Current profile bytes' SHA-256. |
| `selection_mode` | `explicit-image-id` or normal `profile` mode. |
| `requested_image` | Startup explicit ID, or current query-profile reference in normal mode. |
| `expected_image_id` | Startup explicit full local ID; null in normal profile mode. |
| `expected_contract_sha256` | Contract computed from current packaged profile/renderer resources. |
| `observed_image_id` | Valid `.Id` returned by a successful Docker inspection, even if it disagrees with the explicit expectation. Null for an unsuccessful observation. |
| `observed_contract_sha256` | Valid contract-label SHA-256, or null for absent/invalid labels. |
| `repo_digests` | Canonical recorded repository-digest references; `[]` means no recorded digests, null means not successfully observed/validated. These are distinct from `.Id`. |
| `inspection_returncode` | Docker return code when available. Nonzero results never validate partial stdout. |
| `available` | Successfully observed image identity/contract match. Other package/workspace mismatches can still make the top-level `ok` false. |

Inspection output is limited to these fields. Raw Docker stdout/stderr, image
environment/configuration, host environment, command-line arguments and consumer
contents are not returned. Malformed references or unexpected inspection data
produce a failure code rather than reflecting arbitrary text or credentials.

## Mismatch codes

- Snapshot errors: `package-files-unavailable`, `distribution-metadata-unavailable`,
  `source-revision-unavailable` (component identifies startup/current snapshot).
- Package: `unmanaged-installation`, `version-file-differs-from-loaded`,
  `distribution-version-differs-from-loaded`, `package-files-changed`,
  `distribution-metadata-changed`, `source-revision-changed`.
- Instance/workspace: `process-changed`, `workspace-binding-changed`.
- Renderer: `renderer-profile-unavailable`, `renderer-inspection-failed`,
  `renderer-inspection-invalid`, `renderer-image-id-mismatch`,
  `renderer-contract-mismatch`.

A failed inspection never builds, pulls, retags, probes an alternative image or
accepts cached rendering evidence. Profile mode also remains observational; it
does not run its ordinary build-capable ensure path. Metadata/query failures do
not change the existing rendering APIs or invalidate a consumer receipt.

## Hub integration and compatibility

0.5.1 is a backward-compatible diagnostic addition. Tool/resource inventories
grow from 15/8 to **16/9**; prior names and arguments are retained. The response's
schema version is independent of the package version and of MCP protocol version.

0.5.0 remains a separately published immutable-selection release. It lacks this
query: discover capabilities and report that limitation instead of treating a
new CLI invocation or new on-disk package as proof of the old running server.
The installed coexistence/reconnection/rollback proof must exercise both released
packages with explicit image IDs and independent consumers, without changing
global registrations or mutable aliases.

Use the same MCP connection to compare loaded version, installer/source identity,
instance ID, workspace and inspected image with the hub's expected selection.
These are self-reported diagnostic observations, not remote peer authentication
or cryptographic memory attestation. No additional architecture or compatibility
interval follows from two successful release/runtime points.
