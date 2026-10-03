# Owner return: published runtime identity 0.5.1

## Release and source

**[Vegavisuals v0.5.1 is published and its downloaded delivery is verified](https://github.com/dosquartsdedocs/vegavisuals/releases/tag/v0.5.1).**
It provides the native live-process identity query required for H2B/H2C while
preserving 0.5.0's immutable renderer selection and existing tool behavior.

| Item | Identity / result |
| --- | --- |
| Owner checkout | `/home/benizar/git/vegavisuals`, one primary checkout, no worktrees. |
| Implementation | `feat/runtime-identity`; [PR #12](https://github.com/dosquartsdedocs/vegavisuals/pull/12), merged through normal branch protection. |
| Reviewed commit | `5619ec8c9aaf7e362f09c23731218c3eac4d5371`. |
| Published artifact source | **`a448cc50a5f511743a6fcfc213baeb9be80f4a27`**. |
| Source tree | `6753f979c80321b25f6fb67fbd2f1d90733bc0b0`, identical between reviewed commit and merge. |
| Tag | `v0.5.1`, annotated object `cbfb6ef11d9e6741bc4d24e71e6e7326713a5025`, peeling to the source above. |
| Publication | Stable, not draft/prerelease; `2026-10-03T22:50:56Z`. |
| Delivery verification | [JSON](release-verification-v0.5.1.json), SHA-256 `3a89389ac4016135a30fb5ffc632513de9b54a1d259ee09cb01cc04ce7678312`. |

This report/verification JSON are post-release documentation on
`docs/release-0.5.1-closeout`; subsequent documentation HEADs do not change the
source or bytes of the published delivery. All six 0.5.0 asset IDs and their
digests were rechecked against the downloaded baseline, and its annotated tag
remains `c87cc5079e040249ddc4e468470cc772ecad5dad`. No 0.5.0 asset was replaced.

Initial inspection found a clean synchronized `main`, no active cooperating
session/lease and one primary checkout. The protected-branch blocker was resolved
by creating the short-lived implementation branch; a clean preflight preceded
edits. The user explicitly authorized the complete 0.5.1 Git/PR/tag/publication
flow. Git operations were serial, no protection bypass was used, and diagnostic
preflights were not represented as held leases. gacontext remained read-only
context; no consumer or global registration was edited.

## Exact API for the hub

| Surface | API |
| --- | --- |
| CLI | `python -m vegavisuals.cli [--renderer-image-id ID] [--project ROOT] runtime-identity [--profile PROFILE]` |
| MCP tool | `runtime_identity(profile="vl-convert-1.9.0")` |
| MCP resource | `vegavisuals://runtime/identity` |
| Python | `Registry(root, renderer_image_id=ID).runtime_identity(profile=...)` |

Response: `schema_version: 1`, kind `vegavisuals-runtime-identity`. The complete
[schema/semantics](runtime-identity-v1.md) has SHA-256
`c3b724aef6ddcd085f6a573319d04d5154920847537e2227377687db40213c52`.

The key fields are:

- `package.loaded_version`: imported Python version constant, distinct from
  `package.startup` and `package.current_disk` observations. Those snapshots
  include version-file data, a bounded provider-file inventory hash, whitelisted
  distribution metadata/provenance and an optional source HEAD observation.
- `package.mode`: startup `installed`, `development` or explicitly `unmanaged`;
  package/source roots and effective interpreter are reported separately.
- `instance.id`, `.started_at`, `.pid`, `.startup_pid`: fixed registry identity
  and start observations, with a mismatch if its process changes.
- `workspace.root` and `.binding_matches_startup`: the held startup binding,
  never inferred from a later cwd or environment.
- `renderer`: query-scoped profile, requested selection, expected/observed image
  IDs and contract hashes, recorded `repo_digests`, inspection status/availability.
- `ok` and `mismatches`: explicit drift/unavailable components; no raw errors,
  origin URLs, full environment or consumer contents are reflected.

Use the **existing MCP connection** for a running-server identity. A new CLI
invocation has a new PID/instance and cannot certify a previously launched MCP.
Disk snapshots are bounded observations, not cryptographic memory attestation or
a claim of a clean source tree. The selected profile is a query parameter, not a
global last-rendered profile. The immutable explicit image selection stays fixed
at instance creation.

Failure reports keep schema/instance/package fields and `ok: false`; CLI exits 1.
MCP clients must inspect `ok`, not only transport success. The query does not call
init, ensure, build, pull, retag, freshness/receipt publication or launcher setup.
Source refs/version files are parsed as data without Git execution or Python
reload. Distribution discovery excludes consumer cwd and later sys.path entries.

Inventories are now **16 tools / 9 resources**, retaining the previous names and
arguments. 0.5.0 remains a separate published immutable-selection release but
does **not** have this identity capability. H2 must discover/report that absence
instead of substituting a fresh CLI or current disk metadata for the old server.

## Review, tests and CI

The [owner review](https://github.com/dosquartsdedocs/vegavisuals/pull/12#issuecomment-5974274544)
covered the complete diff, privacy boundary, startup/disc distinction, inspection
failures and preservation of the 0.5.0 API. Fourteen new regression cases cover
stable/copy-safe identities, metadata/code drift, no execution/disclosure,
missing/wrong renderer identities/contracts, separate RepoDigests, workspace/PID
changes, consumer metadata shadowing, source refs, CLI/tool/resource parity and
bounded metadata reads. During development an extra Git subprocess interfered
with an existing pinned-root handoff test; pure HEAD/ref reads removed that
side effect. The final suite passes unchanged handoff protections.

Copilot reported a quota limit and did not review; no external approval is claimed.
Branch protection required successful Python 3.10/3.14 checks and resolved
conversations, with zero mandatory approving reviews. No review threads remained.

| Final gate | Executed result |
| --- | --- |
| Local Python **3.10.20** | 174 discovered, **170 passed / 4 runtime-gated**. |
| Local Python **3.12.3** | Same result. |
| Local Python **3.14.5** | Same result. |
| Central integrations | Explicit reference and workspace-manager variables enabled in all final local host runs. |
| `make check` | Passed, including as a prerequisite of host runs. |
| `make tests-install`, Python 3.14.5 | Final wheel/sdist, sdist-built installation, CLI identity, resources/platform/metadata and real stdio/tool/resource checks passed. |
| `make mcp-build mcp-check`, Python 3.14.5 | Passed. |
| `make mcp-smoke REQUIRE_DOCKER=1`, Python 3.14.5 | CLI and Make stdio plus three real Docker cases passed. |
| `make docker-smoke REQUIRE_DOCKER=1`, Python 3.14.5 | Three cases passed, both engines/all formats and retained-data relocation. |
| Installed 0.5.0/0.5.1 coexistence | Live independent sessions, loaded/disc drift, readonly queries, reconnect, rollback, negative images/offline and four retained bundle checks passed. |
| Approved-source rebuild | Reproduced the exact tested wheel and sdist hashes. |
| Downloaded delivery | All six files matched the publication packet; complete installed coexistence proof reran successfully. |

CI links, all successful:

- [PR — 37159357582](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/37159357582).
- [Integrated source — 37159458672](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/37159458672).
- [Tag — 37159560371](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/37159560371).

Hosted axes are 3.10/3.14; local exact patch versions are listed separately. The
four gated local cases were exercised in runtime gates, not counted as
Docker-unavailable acceptance. 0.5.0 historical results do not replace new gates.

Exact argv/stdout/stderr/revision records are retained under
`/tmp/opencode/vegavisuals-release-051/gates/`. Final host commands were `make tests`
with `PYTHON` set to the 3.10 environment, system `python3` (3.12.3), and the
3.14.5 environment, respectively, with:

```text
VEGAVISUALS_HANDOFF_REFERENCE=/home/benizar/git/gacontext
VEGAVISUALS_FACTORY_MANAGER=/home/benizar/git/gacontext/src/bash/mcp_factories/mcp-factory-manager.py
```

Install/MCP/Docker/build gates used
`PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python`.
Published `acceptance.json` retains successful gate commands and evidence hashes.

## Published and downloaded artifacts

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `vegavisuals-0.5.1-py3-none-linux_x86_64.whl` | 98,681 | `5aba4f267a37179c520b3cd164e1cec62ac5561872aa33b069cce73442dc52b1` |
| `vegavisuals-0.5.1.tar.gz` | 188,544 | `bb885661d7e2f285dd72157c2551e7aa64c2add55ccd5ae00d295c416dfe7855` |
| `vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz` | 81,194,322 | `382d2d5dc25d8539787d8089ff7c9fc553d0e8f26d1289f728d87ba4bb090f1f` |
| `release.json` | 3,822 | `9e7e07e2be99343534c8c585e00765608870a86572db96676322b09ba91e5128` |
| `acceptance.json` | 50,897 | `d3745cfae8dd57308728aa963543bd90fdf33bf932d95bb172ecfcd1af518b27` |
| `SHA256SUMS` | 483 | `62bbbe9b7eb28ac6c485a95e401b91ba13ec9f31aee1664714b1fc2b6ec4a255` |

The new receipt binds approved source/PR/CI, the native API/schema and artifacts
to the unchanged renderer. No published bytes were overwritten. Renderer assets,
profile and example SVGs are byte-identical to the baseline; only the example
lock's two registry-code fingerprints were regenerated through the provider.

## Installed proof and runtime identities

The published wheel was downloaded to `download/` below the session directory,
verified with `sha256sum --check SHA256SUMS` plus the checksum file's own hash,
and installed non-editably into `delivered/new`. Published 0.5.0 was independently
installed into `delivered/old`, retaining wheel hash
`da7c6ba33b618eeacc40813c76bfc4295fdacda667873ffcfb242864c21f387f`.

The full downloaded acceptance command was:

```bash
env -u PYTHONPATH -u VEGAVISUALS_RENDERER_IMAGE_ID PATH=/usr/bin:/bin \
  /tmp/opencode/vegavisuals-release-051/delivered/new/bin/python \
  /tmp/opencode/vegavisuals-release-051/acceptance.py \
  /tmp/opencode/vegavisuals-release-051/delivered \
  /tmp/opencode/vegavisuals-release-051/download/vegavisuals-0.5.1-py3-none-linux_x86_64.whl \
  /tmp/opencode/vegavisuals-release-051/previous/vegavisuals-0.5.0-py3-none-linux_x86_64.whl \
  /tmp/opencode/vegavisuals-release-051/download/vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz
```

Two installed MCPs coexisted with separate consumers. The new tool/resource
reported the same instance before any init/render writes. CLI reported its own
distinct instance. In the isolated **new test installation only**, `_version.py`
and METADATA were temporarily changed to 9.9.9: the answering MCP retained loaded
0.5.1, its PID/start/ID and original snapshot, while reporting current disk 9.9.9
and explicit mismatches. Exact original bytes were restored; the same instance
returned to a clean observation. Reconnection produced a new ID. Rollback launched
0.5.0 on the second consumer and rerendered it successfully, while the first
0.5.0 session stayed live/fresh. Old capability absence was explicitly checked.

Unreachable HTTP(S)/ALL proxies exercised operation without network services;
an unavailable Unix-socket daemon failed closed without reflecting its secret
test marker. CLI/MCP missing and incompatible image probes passed. Consumer file
bytes/mtimes were unchanged by identity queries, and test secrets were absent
from all returned identity payloads. Both engines rendered local CSV/JSON under
both releases. Four complete bundles verified with the daemon inaccessible after
producer teardown and retirement of the owned negative fixture.

The native installed coexistence image was:

```text
image ID: sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98
recorded RepoDigests: []
profile: vl-convert-1.9.0
profile SHA-256: 12c55cc892b3040df7653a1fbc06f5c467e5c72edf67c5b53b1f7f192bc988cf
contract: 28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800
```

The existing `diapora-composer/vega-proof:0.4.0` tag already referred to that
image. The first harness stopped before queries because it assumed an untagged
fixture. It was corrected to preserve pre-existing ownership, then rerun fully.
That image/tag was never retired or remapped. Only the separately imported,
untagged incompatible fixture was removed with exact-ID/container-absence checks.

Normal-mode owner gates also exercised local image
`sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e`.
The shared Vega alias stayed on that ID; TIGIT compute IDs stayed
`sha256:22ed1731046c75bc968890159d944de07d59904efc44a7d3d7b1550944fcbd76`
and `sha256:6192f629aff627eb438d02bbe700aa95f2b2a5408f1c15a240ef101af3cf42f2`.
Docker was used directly, without a guard; no selected-image events occurred
during queries/rendering. No registry RepoDigest is invented for the archive.

Evidence under `/tmp/opencode/vegavisuals-release-051/`:

| Evidence | SHA-256 |
| --- | --- |
| `candidate/evidence/summary.json` | `143456200146b3450ff4ac9180e40070c55f883fc6da5aa043eff6b0a873f275` |
| `delivered/evidence/summary.json` | `e3cc13c92b23976360f75fdb238df508f4f731809a6be094cdba2bc58d102ff5` |
| `delivery-verification.json`, identical to the versioned verification JSON | `3a89389ac4016135a30fb5ffc632513de9b54a1d259ee09cb01cc04ce7678312` |
| `acceptance.py` | `bf5727931858558b0708ae346df58711a0cb900b09f667dbae56a1808ebb5c18` |

Installed proof used Python 3.14.5, MCP 1.29.0 and Docker 27.2.0 linux/amd64.
Host/wheel coverage is Linux x86_64. No other architecture, continuous release/
Python interval or arbitrary-image compatibility is certified by these points.
The existing nonblocking `pydantic-settings` warning was recorded.

## Next hub step

H2 can consume the two immutable published releases and adapt the exact schema 1
API. Query the existing connection, compare loaded version, verified installation
identity, instance/workspace and observed image to the selected receipt, and
handle discrepancies/capability absence explicitly. Pass a full local image ID;
resolve/acquire references separately and preserve aliases. H2A references
`14c9e27bb8c047b2dae4ee2c11ae958888d194c2` and
`68d1dd705e777ca65ead09f4eaf233689e1f8a2c` were context, not edited/adopted schemas.
No gacontext, TIG/TIGIT/Geodisseny, global MCP configuration or client activation
was changed. Provider publication is complete; hub H2B/H2C integration remains
its own operation.
