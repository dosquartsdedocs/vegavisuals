# Owner closeout: Vegavisuals 0.5.0 — 2026-09-30

## Delivered state

**[v0.5.0 is published](https://github.com/dosquartsdedocs/vegavisuals/releases/tag/v0.5.0)
and its downloaded delivery has passed installed acceptance.** This closes the
native immutable-image control-plane increment; it does not activate clients or
upgrade real manuals/helpers.

| Item | Final identity / state |
| --- | --- |
| Authoritative checkout | `/home/benizar/git/vegavisuals`; one primary checkout throughout. |
| Implementation branch | `feat/native-renderer-selection`, integrated through [PR #10](https://github.com/dosquartsdedocs/vegavisuals/pull/10). |
| Preserved implementation checkpoint | `41dfe9388eef2adbdc0d4857b156646bbec793da`. |
| Final-version reviewed commit | `d537545dcac8e12478ea4b95043ba316591318da`. |
| Approved release source / integrated HEAD | **`2b34c338ebb229bbe9b4def2a141f0176217ee5a`**. |
| Source tree | `7e1e84b6ddca9cb0e61fc5a0742b0cfd7a865741`, identical between final reviewed commit and merge. |
| Annotated tag | `v0.5.0`, tag object `c87cc5079e040249ddc4e468470cc772ecad5dad`, peeling to the approved source above. |
| Publication | Stable, not draft/prerelease; published `2026-09-30T21:05:46Z`. |
| Delivery verification | [Machine-readable verification](release-verification-v0.5.0.json), SHA-256 `baff30e1dabec2e55be6877f1b0ae9e6c896783131a3fe61d094639db67695e8`. |

This report and its verification JSON are a **post-release documentation-only
follow-up** on `docs/release-0.5.0-closeout`. They do not change the release tag,
the source revision used to build the published artifacts, or any published
asset. Distinguish later documentation HEADs from the production revision above.

The initial preflight found only the expected dirty `main`, with no active
cooperating session. The user explicitly authorized the complete branch,
commit/push/PR/integration/tag/publication flow. Existing work was preserved in
the implementation branch and committed; a fresh preflight was eligible before
final edits. A fresh clean-branch preflight also preceded this documentation.
Diagnostics were not represented as held session leases. No worktrees or other
mutable source checkouts were created.

## Review and resolution

The owner reviewed both commits and the complete PR diff, including the new
selector tests and historical reports. The
[recorded review](https://github.com/dosquartsdedocs/vegavisuals/pull/10#issuecomment-5919584452)
confirms:

- `--renderer-image-id`, `VEGAVISUALS_RENDERER_IMAGE_ID` and
  `Registry(..., renderer_image_id=...)` accept only full lowercase local Docker
  image IDs. Explicit API/CLI values override the environment and are fixed at
  startup. Tags, shortened IDs and RepoDigests are rejected.
- Inspection checks the exact ID and published contract label. Explicit ensure
  inspects only; explicit build, including dry run, is rejected. Missing,
  incompatible and failed inspections cannot become fallback builds/pulls or
  cache hits. A new regression covers nonzero/timeout/missing-command statuses
  even when stdout contains an apparently valid identity and contract.
- Selection propagates to CLI/MCP launch configuration, fingerprints, freshness,
  cache paths and native/bundle provenance. Receipt v1 retains its existing
  source/artifact schema and is issued only after selected-runtime freshness.
  Retained bundle verification remains independent of local image availability.
- Normal profile-based behavior, publication protections and caller-confirmed
  replacement of modified outputs are preserved.
- Published renderer resources/profile bytes remain untouched. Both example
  SVGs are byte-identical; their lock changed only the two host fingerprints.

Finalization coherently set package/native manifest version and release metadata
to `0.5.0`, updated installation/release/security documentation, and made CI
reuse the verified published renderer archive in its fresh runner. No renderer
rebuild or central H1 fields were needed.

GitHub required successful **Python 3.10 / Python 3.14** checks and conversation
resolution, with **zero required approving reviews**. Normal protected-branch
merge was used, without administrative bypass; no review threads remained.
Copilot reported a quota limit and did **not** review. It is not counted as
external approval. Owner self-review and the explicit user authorization are
recorded separately from CI.

## Final CI and local matrix

All three release-related CI runs completed successfully:

- [PR CI — 36775876602](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/36775876602)
  on `d537545dcac8e12478ea4b95043ba316591318da`.
- [Integrated-source CI — 36776129323](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/36776129323)
  on the approved release revision.
- [Tag CI — 36776372521](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/36776372521)
  on the same revision under `v0.5.0`.

| Executed gate | Result |
| --- | --- |
| Host Python **3.10.20** | 160 discovered, **156 passed / 4 runtime-gated**. |
| Host Python **3.12.3** | Same result. |
| Host Python **3.14.5** | Same result. |
| Central integration cases | Explicit `VEGAVISUALS_HANDOFF_REFERENCE` and `VEGAVISUALS_FACTORY_MANAGER` enabled in all three local host runs. |
| `make check` | Passed, including as a prerequisite of every host run. |
| `make tests-install`, Python 3.14.5 | Passed: final wheel/sdist, sdist-built non-editable wheel, resources/platform/metadata checks and installed stdio/local-data/bundles. |
| `make mcp-build mcp-check`, Python 3.14.5 | Passed. |
| `make mcp-smoke REQUIRE_DOCKER=1`, Python 3.14.5 | CLI stdio, Make stdio and all three Docker cases passed. |
| `make docker-smoke REQUIRE_DOCKER=1`, Python 3.14.5 | Three passed: both engines/all formats, repeatability, scoped cleanup and retained-data relocation. |
| Installed final-wheel acceptance, Python 3.14.5 | Native A/B/A, all formats, receipts, cache reuse, CLI/SDK selection, negative images and 12 post-retirement bundle checks passed. |
| Integrated-source rebuild | Exact wheel **and sdist** hashes reproduced from the approved merge. |
| Downloaded delivery | All five checksum entries and the checksum-file hash matched; receipt artifact hashes/sizes matched. The downloaded wheel was reinstalled and the complete native acceptance rerun passed. |

The four locally gated tests are exercised by the runtime gates; they are not
Docker-unavailable skips. Hosted CI uses its configured 3.10/3.14 axes; the exact
local patch versions above are separate evidence. The 0.4.0 and `.dev0` tests
were retained as history, not counted as final-package gates.

Exact local gate commands, from the owner checkout:

```bash
make check
VEGAVISUALS_HANDOFF_REFERENCE=/home/benizar/git/gacontext \
VEGAVISUALS_FACTORY_MANAGER=/home/benizar/git/gacontext/src/bash/mcp_factories/mcp-factory-manager.py \
  make tests PYTHON=/home/benizar/git/vegavisuals/.tmp/workspace-policy-py310/bin/python
# Repeat that environment with PYTHON=python3 for 3.12.3, and with:
# PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python for 3.14.5.
make tests-install PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python
make mcp-build mcp-check PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python
make mcp-smoke REQUIRE_DOCKER=1 PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python
make docker-smoke REQUIRE_DOCKER=1 PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python
make build PYTHON=/tmp/opencode/vegavisuals-release-050/host314/bin/python
```

The session's `run_gate.py` retained exact argv, cwd, source revision, result and
stdout/stderr for each invocation under
`/tmp/opencode/vegavisuals-release-050/gates/`. `acceptance.json` in the published
release records the successful gate evidence hashes and the actual source/CI
identities. The final acceptance harness SHA-256 is
`3598fdf2c5c35442db5bbd359778b93b52b766fe9953306e0a2151730075cab7`.

## Published and downloaded hashes

These are the **published bytes**, not provisional `.dev0` artifacts. All six
downloaded files matched the publication packet exactly:

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `vegavisuals-0.5.0-py3-none-linux_x86_64.whl` | 92,682 | `da7c6ba33b618eeacc40813c76bfc4295fdacda667873ffcfb242864c21f387f` |
| `vegavisuals-0.5.0.tar.gz` | 169,617 | `6f8e00e36d9eaea45c557ab8490acc53b854eb682c47598c734949b9989b8ec4` |
| `vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz` | 81,194,322 | `382d2d5dc25d8539787d8089ff7c9fc553d0e8f26d1289f728d87ba4bb090f1f` |
| `release.json` | 3,253 | `7dab5dbcce5a665d4fa30e980629db35ecfda05ed67344aef7dbd014646b179b` |
| `acceptance.json` | 25,333 | `41ce3ee90cfa7dfe5507eb20e4b53895dc365da5a19177f02fe9252a665e6558` |
| `SHA256SUMS` | 483 | `32792e8314a06d87ee2327f8816ca369e2d3d754d43b80ca0e540ebbb91ffcda` |

The new receipt binds these distribution identities to the approved source,
PR/CI, native selector, profile bytes, renderer contract and expected image ID.
The renderer archive is reused byte-for-byte from 0.4.0; no 0.4.0 asset or receipt
was replaced. No published 0.5.0 asset was rewritten after delivery verification.

Delivery commands included:

```bash
gh release download v0.5.0 --repo dosquartsdedocs/vegavisuals \
  --dir /tmp/opencode/vegavisuals-release-050/download
# Working directory: that download directory
sha256sum --check SHA256SUMS
sha256sum SHA256SUMS
```

The downloaded wheel was installed into the independent `delivered/venv`, with
Python 3.14.5. The acceptance invocation, from the owner checkout, was:

```bash
env -u PYTHONPATH -u VEGAVISUALS_RENDERER_IMAGE_ID PATH=/usr/bin:/bin \
  /tmp/opencode/vegavisuals-release-050/delivered/venv/bin/python \
  /tmp/opencode/vegavisuals-release-050/acceptance.py \
  /tmp/opencode/vegavisuals-release-050/delivered \
  /tmp/opencode/vegavisuals-release-050/download/vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz \
  /tmp/opencode/vegavisuals-release-050/download/vegavisuals-0.5.0-py3-none-linux_x86_64.whl
```

## Runtime acceptance and cleanup

Both exact compatible points were newly tested with final **0.5.0** on one daemon:

```text
A: sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e
B: sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98
profile: vl-convert-1.9.0
profile SHA-256: 12c55cc892b3040df7653a1fbc06f5c467e5c72edf67c5b53b1f7f192bc988cf
renderer contract: 28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800
```

A was selected by its immutable ID without altering its existing alias. B was
loaded from the verified archive without RepoTags; config/layers and image ID
remained unchanged. `/usr/bin/docker` was used directly throughout, with no guard
or alias-remapping wrapper. A separate untagged empty image supplied the
incompatible-label negative case and was never executed.

Each installed proof used a fresh synthetic consumer, local CSV/JSON, both
engines and SVG/PNG/PDF. A initially reported six missing outputs; B and the
return to A each reported six stale outputs. Each render/check produced six fresh
outputs and a receipt covering two inputs/six artifacts. A's inline cache was
preserved and reused on return. CLI-option child stdio and environment-selected
SDK stdio verified both effective IDs. Absent/incompatible selections failed
closed, and explicit build was rejected.

After producer teardown and relocation, only session-owned untagged images were
removed using exact identities, container-absence checks and no force. **All 12
bundles verified after runtime retirement**, including verification with B
selected but absent. The candidate and downloaded-delivery runs produced the
same twelve bundle-manifest hashes. Inspecting those hashes is not a substitute
for the completed full bundle checks recorded in the per-call evidence.

The shared Vega alias retained A. TIGIT compute images retained:

```text
tigit-compute-python:local
  sha256:22ed1731046c75bc968890159d944de07d59904efc44a7d3d7b1550944fcbd76
tigit-compute-r:local
  sha256:6192f629aff627eb438d02bbe700aa95f2b2a5408f1c15a240ef101af3cf42f2
```

No Vega test containers remained. No manual source, consumer pin, active client
registration, compute image or existing alias was changed. Both PNGs were
visually checked for the expected labelled bars (4, 7, 5).

The first final-wheel attempt's event audit rounded its start to whole seconds
and therefore included its own preparation load. All functional probes had
passed. The audit was corrected to a nanosecond window, failed-attempt owned
images were retired, and the **entire** proof reran successfully in a fresh
consumer. A further complete run used the downloaded release. This was a harness
correction, not a provider fallback or waived gate. Nonblocking pip cache and
`pydantic-settings` warnings are retained in the logs.

Retained evidence under `/tmp/opencode/vegavisuals-release-050/`:

| Evidence | SHA-256 |
| --- | --- |
| `candidate-final/evidence/summary.json` | `15c329eb6836bd2d71155413e20540690d48098675fac362e1184b7f4db0a73d` |
| `delivered/evidence/summary.json` | `8fb7425444178fcd533e985027eb362fdfde9b5552c4762395e5c39175e9524e` |
| `delivery-verification.json` (identical to the versioned verification JSON) | `baff30e1dabec2e55be6877f1b0ae9e6c896783131a3fe61d094639db67695e8` |

Full delivered bundle trees are in `delivered/relocated/retained/`. Local test
environments and evidence are not published packages; the assets linked above
are the verified release delivery.

## H2 handoff and limits

H2 can now select the released control plane using the hashed wheel/receipt.
For runtime selection it **must pass a full lowercase local image ID**. Resolve
tags/RepoDigests and acquire/verify images during preparation, preserve existing
aliases, then pass the expected ID via CLI/environment/API. Do not implement
selection by rewriting packaged profiles or by remapping Docker commands.

Tested host points are Python 3.10.20/3.12.3/3.14.5 on Linux x86_64. Installed
acceptance used Python 3.14.5, MCP 1.29.0 and Docker 27.2.0 linux/amd64, with Vega
6.2.0 and default Vega-Lite 6.4. These tests do not certify another architecture,
a continuous Python/helper-version interval, arbitrary images carrying a label,
or downstream web/PDF/slide importer combinations. Other Vega-Lite versions in
the unchanged profile are not newly claimed as compiled final-package points.

There is no remaining owner publication authorization blocker. H1 metadata
adoption, H2 client activation and real consumer upgrades remain their separately
reviewed operations. Historical 0.4.0 evidence remains valid for that release,
not as a substitute for the final 0.5.0 gates recorded here.
