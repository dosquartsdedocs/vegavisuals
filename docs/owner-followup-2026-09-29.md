# Owner follow-up: native immutable renderer selection — 2026-09-29

## Returned unit and release proposal

**The new control plane selects a full Docker image ID natively, outside the
compatibility profile.** Both real renderers passed on the same daemon, including
an untagged copy of the published renderer, without a Docker guard or alias
changes. Selection is fixed at startup and reaches rendering, freshness, inline
cache, locks, bundles, generated client configuration and lifecycle commands.

- Authoritative checkout: `/home/benizar/git/vegavisuals`, branch `main`.
- Base HEAD: `68c0b231402ae9485cc34ce530dc5239cb0ec194` (published `v0.4.0`).
- Candidate: **`0.5.0.dev0`**, local and uncommitted. Proposed reviewed release:
  **`v0.5.0` of the Python/MCP control plane**. No issue, PR, commit, tag or
  publication was created in this session.
- Existing published `v0.4.0` remains evidence of the original installed native
  workflow. It does not contain this selector and needs no replacement assets.
- The earlier [preparation report](owner-preparation-2026-09-29.md) is preserved
  byte-for-byte: SHA-256
  `7c9e09affac2a5f90970a5c4806da12bb5c059bc0a5ecf005684872b441428ae`.
- Read the owner instructions, that report, the hub's
  `handoffs/owner-followups/README.md` and its verified preparation roundup.
  No new central H1 schema/range fields were introduced.

This document is the owner return artifact for hub intake. The next publication
blocker is source review/integration and the final `0.5.0` distribution/CI gates,
not a renderer rebuild or a real-manual upgrade.

## Native interface and reviewed behavior

```bash
# Installed candidate interpreter; option precedes the subcommand.
PYTHON=/absolute/path/to/candidate/bin/python
IMAGE_ID=sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98
"$PYTHON" -m vegavisuals.cli --renderer-image-id "$IMAGE_ID" ensure-renderer
"$PYTHON" -m vegavisuals.cli --renderer-image-id "$IMAGE_ID" \
  --project /absolute/consumer render-all
VEGAVISUALS_RENDERER_IMAGE_ID="$IMAGE_ID" \
  MCP_CONSUMER_WORKSPACE=/absolute/consumer "$PYTHON" -m vegavisuals.cli mcp serve
```

Python: `Registry(root, renderer_image_id=IMAGE_ID)`. The explicit API/CLI option
wins over `VEGAVISUALS_RENDERER_IMAGE_ID`; the resolved value is retained once in
the registry. Changing the environment later does not redirect that registry.
An empty or malformed selection is rejected before Docker invocation.

The supported selector is deliberately bounded: a **full lowercase
`sha256:<64 hex digits>` local Docker image ID**. That ID is also the expected
identity. Tags, shortened IDs and registry `repository@sha256:…` digests are not
accepted by this increment. Archive acquisition or registry-digest resolution
belongs to preparation; neither is an implicit render operation.

Owner self-review covered the following invariants, with regressions:

1. Inspection uses the selected ID directly, compares Docker's returned `.Id`
   to it, and retains the existing exact renderer-contract label comparison.
   A correct label with the wrong ID is rejected, as is a correct ID with the
   wrong/missing label. The profile's `image` field and resource bytes are not
   rewritten to implement selection.
2. Explicit `ensure-renderer` only inspects/reuses and returns `built: false`.
   Missing/incompatible selection fails. `build-renderer`, including its dry run,
   is disabled. Render, validation, freshness and bundle export fail closed when
   they require an unavailable selected renderer; there is no fallback, build,
   pull or retagging path. Image-inspection failure cannot become a cache hit.
3. Actual containers still execute the inspected immutable ID with the existing
   offline, read-only, confined staging and resource-limit boundary. The consumer
   is not mounted into the renderer.
4. Explicit selection adds the ID to the render fingerprint. A → B changes
   managed-output freshness and inline cache names. A's cache survives B and is
   reused on return to A. A matching fingerprint cannot hide conflicting native
   renderer provenance. User-edited output still needs replacement confirmation.
5. Lock/cache schemas remain version 2, carrying the selected ID in existing
   `renderer.image` and `renderer.image_id` fields. File/inline bundle requests
   and producer runtime revisions retain that effective identity. Export under
   another selection rejects stale evidence instead of rewriting its history.
6. Receipt v1 keeps its established source/artifact hash schema. Selected-runtime
   freshness is checked before issuance; switching to B invalidates the prior
   receipt until B's outputs are fresh. If A and B produce identical bytes,
   receipt content can also be identical; runtime provenance is in the native
   lock and bundle, not a new receipt field.
7. Metadata/client-configuration generation and retained-bundle verification
   remain usable without a running renderer. Bundle verification checks archived
   identity/resources, rather than requiring the locally selected image to be
   the producer image. Eight bundles passed after the test renderer was removed.
8. Generated package commands retain the explicit interpreter and selector flag;
   checkout commands retain the selector through an explicit environment prefix.
   Client configurations carry the selector in the server environment. Stdio
   tools have no per-call runtime-switch argument. CLI `mcp-smoke` forwards the
   selector to its child process and verifies both returned image IDs.
9. With the selector unset, the existing profile alias, ensure/build behavior,
   portable contract-based freshness and offline historical-bundle semantics
   remain intact. Existing regression cases for compatible rebuilt images and
   unavailable-Docker normal freshness still pass.

The review is an owner self-review and test result, not external PR approval.

## Changed paths and immutable resources

The prior README preparation edits were retained. This follow-up changes:

| Path | Purpose |
| --- | --- |
| `src/vegavisuals/registry.py` | Startup selection, exact inspection, explicit no-build behavior, fingerprint/cache/freshness and effective discovery/configuration. |
| `src/vegavisuals/cli.py` | Global selector option and selected-runtime child smoke verification. |
| `src/vegavisuals/handoff.py` | Bind export to selected native provenance; keep retained verification offline. |
| `src/vegavisuals/_version.py` | Distinct unpublished `0.5.0.dev0` control-plane identity. |
| `mcp-factory.yml`, `src/vegavisuals/factory/mcp-factory.yml` | Matching candidate version/release metadata; schema and native contract versions unchanged. |
| `Makefile` | Check wheel/descriptor version parity instead of hardcoding 0.4.0; test prerelease wheel platform rejection explicitly. |
| `tests/test_renderer_selection.py` | Nine focused mocked-Docker regressions covering the invariants above. |
| `tests/test_registry.py` | Update CLI constructor assertions for the explicit option. |
| `tests/test_mcp_stdio.py` | Local data for raw Vega as well as Vega-Lite; inspect effective selector and returned runtime IDs in real stdio proofs. |
| `.vegavisuals.lock.json` | Two regenerated host fingerprints for the committed examples; generated through normal-mode `render-all`. |
| `README.md` | Document the new native interface, fail-closed behavior, limits and upgrade/freshness implications. |
| `docs/owner-followup-2026-09-29.md` | This return report. |

`git diff --exit-code -- src/vegavisuals/assets Dockerfile examples/rendered`
passed. All published renderer assets/profile/theme/token/worker/guide bytes are
unchanged. Both example SVGs remained byte-identical; their lock diff contains
only two fingerprint replacements. Host code already participates in the native
fingerprint, so upgrading this control plane requires managed-output refresh even
in normal mode. Consumer outputs were not regenerated in this session.

Selected reviewed source/descriptor identities:

| Path | SHA-256 |
| --- | --- |
| `src/vegavisuals/registry.py` | `5770a675211cd83025102b8639fdf79fcd07734748867f6581149f1359bafca4` |
| `src/vegavisuals/cli.py` | `abd64678f6db9a1e1ac4d2209a2bd425ea49fc6402d52059ef581aa7f77074cb` |
| `src/vegavisuals/handoff.py` | `11b07058900bcf06556fc87e500b95c30bcc3da891c606df17006b24d4c1f533` |
| Checkout `mcp-factory.yml` | `9f19942872282ba8444de9176ee62d5fce6763dcf7f2165646b69256ba9ee4d3` |
| Package `mcp-factory.yml` | `d6dce67e57c87a98d99f4a2f89ab76c879d1ff9dbf7377fa4c67b85efe448cd3` |
| `tests/test_renderer_selection.py` | `785b158c184aaae44268f2d8f9c23e0df432bcbb097bd0608d00cf4ca535f3d7` |

## Same-daemon installed acceptance

All new acceptance used `/usr/bin/docker` directly, with no alias-mapping guard
or patched runner. The independent non-editable wheel installation and retained
evidence are under:

```text
/tmp/opencode/vegavisuals-selector-20260929/
  venv/
  preparation.json
  evidence-native/
  relocated/retained/
```

Tested runtime selections on the same Docker daemon:

| Selection | Exact image ID | Preparation / alias behavior |
| --- | --- | --- |
| A | `sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e` | Existing compatible renderer, selected directly by ID. Its shared alias was not modified. |
| B | `sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98` | Original v0.4.0 published image, loaded **without any RepoTags**. Native selection used its unchanged ID. |

Both carry renderer contract
`28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800`.
The published archive was rehashed as
`382d2d5dc25d8539787d8089ff7c9fc553d0e8f26d1289f728d87ba4bb090f1f`.
Its config and layer bytes were transported unchanged through a tagless Docker
load stream; original alias metadata was omitted, not applied to the daemon.
This changes neither the published archive nor the runtime image identity.

The installed CLI proof used synthetic CSV and JSON inputs with both engines in
SVG, PNG and PDF. A → B → A produced these observations:

| Stage | Before render | After render/check | Inline cache |
| --- | --- | --- | --- |
| A | 6 missing | 6 fresh | Created A caches for both engines. |
| B | 6 stale | 6 fresh | Created separate B caches; retained A caches. |
| A again | 6 stale | 6 fresh | Reused A caches without rendering inline again. |

Each fresh receipt covered two local inputs and six artifacts. Both image IDs
also passed installed CLI-option `mcp-smoke`, and installed SDK stdio tests
including local-data file bundles and inline bundles. The source Make transport
additionally passed with B selected through the environment.

Negative native probes used an absent full ID and a separately prepared,
untagged empty image with no contract label
(`sha256:78b5f7e6ecb67dc26ee443e9dfbb4a445b7c318a540a7b1ae8a31fe367e6f49e`).
Ensure, render and inline dry-run failed; explicit build was rejected. That
negative fixture was never executed. Wrong-ID-with-correct-label behavior was
tested with mocked Docker, since a real Docker image cannot resolve to a different
config ID under the same full content ID.

Docker reported no image events for A/B during the successful proof. B had no
aliases before or after it. Before/after inspections preserved these IDs:

```text
vegavisuals/render:vl-convert-1.9.0
  sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e
tigit-compute-python:local
  sha256:22ed1731046c75bc968890159d944de07d59904efc44a7d3d7b1550944fcbd76
tigit-compute-r:local
  sha256:6192f629aff627eb438d02bbe700aa95f2b2a5408f1c15a240ef101af3cf42f2
```

After all gates, only the two session-owned untagged images (B and the negative
fixture) were removed, with exact-ID/container-absence checks and `--no-prune`,
without force. The eight retained bundles passed with B selected but absent;
`ensure-renderer` still failed closed after that retirement. Manual sources,
registrations, helper installations and compute images were not targeted.

## Commands and results

From the owner checkout, with ordinary Docker PATH and the selector unset:

```bash
env -u VEGAVISUALS_RENDERER_IMAGE_ID PYTHONPATH=src \
  python3 -m vegavisuals.cli --project . render-all
make check
VEGAVISUALS_HANDOFF_REFERENCE=/home/benizar/git/gacontext \
VEGAVISUALS_FACTORY_MANAGER=/home/benizar/git/gacontext/src/bash/mcp_factories/mcp-factory-manager.py \
  make tests
VEGAVISUALS_HANDOFF_REFERENCE=/home/benizar/git/gacontext \
VEGAVISUALS_FACTORY_MANAGER=/home/benizar/git/gacontext/src/bash/mcp_factories/mcp-factory-manager.py \
  make tests PYTHON=/home/benizar/git/vegavisuals/.tmp/workspace-policy-py310/bin/python
make tests-install
make mcp-build mcp-check
make mcp-smoke REQUIRE_DOCKER=1
make docker-smoke REQUIRE_DOCKER=1
```

Installed native proof, working directory
`/tmp/opencode/vegavisuals-selector-20260929`:

```bash
python3 prepare.py
python3 prepare-incompatible.py
env -u PYTHONPATH -u VEGAVISUALS_RENDERER_IMAGE_ID PATH=/usr/bin:/bin \
  /tmp/opencode/vegavisuals-selector-20260929/venv/bin/python proof.py
python3 finish.py
```

The venv was created with `python3 -m venv` and installed using its interpreter:

```bash
python -m pip install --disable-pip-version-check --quiet \
  '/home/benizar/git/vegavisuals/dist/vegavisuals-0.5.0.dev0-py3-none-linux_x86_64.whl[mcp]'
```

The installed SDK test was run from the owner checkout for each exact A/B ID:

```bash
env -u PYTHONPATH PATH=/usr/bin:/bin \
  VEGAVISUALS_MCP_SMOKE=1 VEGAVISUALS_RENDERER_IMAGE_ID="$IMAGE_ID" \
  VEGAVISUALS_MCP_COMMAND=/tmp/opencode/vegavisuals-selector-20260929/venv/bin/vegavisuals \
  /tmp/opencode/vegavisuals-selector-20260929/venv/bin/python -m unittest tests.test_mcp_stdio
```

For the selected Make-transport case, the exact command was:

```bash
env VEGAVISUALS_RENDERER_IMAGE_ID=sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98 \
  VEGAVISUALS_MCP_SMOKE=1 VEGAVISUALS_MCP_FACTORY_ROOT=/home/benizar/git/vegavisuals \
  .cache/vegavisuals/mcp-venvs/f6468d012316e587d2e96143/bin/python \
  -m unittest tests.test_mcp_stdio
```

| Gate | Result |
| --- | --- |
| `make check` | Passed; both committed SVGs regenerated byte-identically. |
| Host suite, Python **3.12.3** | 159 discovered, 155 passed, 4 runtime-gated cases exercised separately. |
| Host suite, Python **3.10.20** | Same result, with the explicit central fixture and workspace-manager integrations. |
| Nine selector regressions | Passed; Docker execution mocked. |
| `make tests-install` | Passed: wheel/sdist, sdist-built wheel, package/native metadata, platform rejection and real installed stdio/export checks. |
| `make mcp-build mcp-check` | Passed; normal alias reused. |
| `make mcp-smoke REQUIRE_DOCKER=1` | CLI and Make stdio cases plus all three Docker cases passed. |
| `make docker-smoke REQUIRE_DOCKER=1` | Three passed, including both engines/all formats and retained-data relocation. |
| Independent installed A/B proof | Passed: all formats, A/B/A freshness/cache transitions, typed failures, receipts and eight retained bundles. |
| Installed SDK tests with A and B | One complete stdio/export case passed for each ID. |
| Source Make stdio with B | Passed. |
| Visual review | Both PNGs show the expected three labelled bars (4, 7, 5). |
| Immutable resources and whitespace | Asset/Dockerfile/SVG diff empty; `git diff --check` passed. |

Initial test development corrected a mock-factory keyword and existing CLI call
assertions. The first real proof reached the negative probe but found an assumed
old Alpine image absent; the fixture was replaced by an independently prepared
empty untagged image, and the complete proof passed in fresh synthetic paths.
These were test-harness issues, not relaxed runtime checks. The existing
`pydantic-settings` startup warning remained nonblocking.

## Artifacts, coverage and hub continuation

Local candidate artifacts, **not published**, built and tested after the version
change and all functional edits:

| Artifact | SHA-256 |
| --- | --- |
| `dist/vegavisuals-0.5.0.dev0-py3-none-linux_x86_64.whl` | `d5180412e51df46d9e6c32e6861a7dc0f17f6a4d5426378c77a1dac3db435cbb` |
| `dist/vegavisuals-0.5.0.dev0.tar.gz` | `7ec1ee908b7e16ba3b8e4c97210739ede92be12dd0e88a88ab07349da705dae5` |

This final report was written after those gate artifacts; it is supplemental
evidence and is not embedded in that sdist. The published 0.4.0 artifacts and
their hashes remain as recorded in the earlier report.

Evidence files relative to `/tmp/opencode/vegavisuals-selector-20260929/`:

| Evidence | SHA-256 |
| --- | --- |
| `evidence-native/summary.json` | `8f8c81208432465e3074b6d3ea2bc3a167dbe3a2147703c2f2d9025262677a65` |
| `evidence-native/retirement.json` | `2dd312f190fb1f9ae17861d341a33d9d3f5c8e1e444fe7d3205440b0b8bbc007` |
| `preparation.json` | `e929eed33686cbad2cf24ec9962e0b21a34c7176d35778e55e8b414178e59409` |
| `proof.py` | `ba27e00d054ae1b48bf519fa20aaeaca9ad19368c69d3aa20657ff3374b476b9` |

The summary lists all eight relocated bundle paths and manifest hashes; the
retirement report contains their successful post-removal verification results.
Per-call evidence retains full native command/result JSON, receipts and locks.

Newly tested points are the **0.5.0.dev0** control plane on Linux x86_64 with
Python 3.10.20 (host suite) and 3.12.3 (host/installed runtime), MCP 1.29.0, Docker
27.2.0 linux/amd64, the unchanged `vl-convert-1.9.0` profile and exact A/B image
IDs above. No broad helper/version interval, RepoDigest selector, alternative
architecture or new Python-3.14 candidate run is claimed. Earlier 0.4.0 evidence
continues to describe that released producer, not this uncommitted candidate.

Release proposal and next steps:

1. Review/integrate this control-plane unit, then set coherent **0.5.0** package
   and native manifest metadata. Build and gate the exact final wheel/sdist and
   run the owner CI matrix, including Python 3.14. Publish new immutable artifacts;
   never replace the existing 0.4.0 assets or publish this `.dev0` wheel as final.
2. Reuse the published renderer's unchanged config/layers, profile, resources and
   contract. A new renderer image build is not required. The release receipt can
   bind the new control plane to the existing archive hash and exact image ID.
3. H2 may select prepared image IDs through the native option/environment once
   this feature is released. Resolve/download/verify artifacts separately and
   preserve existing aliases. Native runtime selection no longer needs a guard.
4. Adopt only the integrated H1 metadata/range contract later. Keep final client
   switching, manual/helper pin updates and downstream importer acceptance in
   their own reviewed consumer operations.
