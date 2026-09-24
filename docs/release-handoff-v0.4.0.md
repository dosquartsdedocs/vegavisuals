# Owner handoff: artifact bundles, release candidate 0.4.0

## Scope and publication state

This change prepares the **Vegavisuals 0.4.0 source/wheel/sdist release** with
`contracts.artifact_handoff: 1`, `export_visualization_bundle` and
`check_visualization_bundle`. The precise API and retention contract are in
[artifact-handoff-v1.md](artifact-handoff-v1.md).

Implementation branch: `feat/artifact-handoff-v1` in the authoritative checkout.
Starting immutable revision: `e57435f65521f3e10b235e9f96dfdec53da2b87b`.
Central schema/verifier/fixtures: `9167e3efb5968a64bb9100792163a179c1491860`;
shared-contract merge: `fc8745db950b04013c73eb49acf6781a26eb83f8` (central PR #7).
The implementation is a release candidate. Its feature commit, review and CI
are tracked in the implementation PR. The release tag and published distribution
hashes must be recorded by the authorized publication session; the starting
revision is **not** a pin for this feature.

The QGIS and Diavisuals checkouts inspected for this owner task did not yet
contain reviewed v1 pilot findings. Incorporate their reviewed conclusions
before declaring coordinated rollout conformance. The pinned fixtures support
development/testing immediately; they do not establish real importer adoption.

## Components and pins

| Component | Action |
| --- | --- |
| Vegavisuals host package/MCP | Publish reviewed 0.4.0 source, Linux wheel and sdist; record full Git object ID and SHA-256 for each published distribution. Update checkout/package manifests together. |
| Renderer | Unchanged Dockerfile, worker, profile, themes and fonts. Keep the `vl-convert-1.9.0` renderer contract and existing immutable image when compatible. This project builds local images and publishes no prebuilt renderer image. |
| Base image | Keep `python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1`. |
| Render libraries | Keep `vl-convert-python==1.9.0.post1`, Vega 6.2.0, default Vega-Lite 6.4 and the existing supported version set. |
| MCP | Keep `mcp==1.29.0`. |
| Native contracts | Manifest 1, lock 2, inline cache 2 and companion receipt 1 remain unchanged. The host-registry fingerprint changes; regenerate managed examples/consumer outputs through the provider, not by patching locks. |

Bundle producer revisions hash the actual installed package inventory. Runtime
revisions identify the actual image used by the native render. A local image ID
is useful execution evidence, not a registry-distributable release. Real consumer
upgrades must install the published immutable producer artifact and use its
reviewed runtime contract; neither this branch nor a mutable sibling checkout is
a released dependency. Record the image ID actually used in each integration
proof; do not replace it with a generic tag.

## Coordinator and dependent owner changes

After release, prepare dependent pin-update PRs together with their importer
capabilities, retaining the **full existing `mcp_dependencies` closure**:

1. **Central coordinator (`my-scripts-factory`)**: record the Vegavisuals feature
   PR, full release revision, published artifact hashes, tested tool contract,
   pilot-review disposition and checks. Keep consumer binding at the startup
   root. Registration exposes capabilities; the client explicitly transports the
   returned bundle, rather than assuming server-to-server calls. Central roadmap
   changes belong to that owner session.
2. **Unaltraweb**: replace the existing Vegavisuals 0.3.1 dependency in
   `mcp-factory.yml` and its packaged/dynamic counterparts. Add
   `export_visualization_bundle` and `check_visualization_bundle` to the required
   tools for the feature. Update `components.vegavisuals` and
   `consumer_integration.vegavisuals_sha` in
   `src/unaltraweb_mcp/component-contract.json`; the inspected old SHA is
   `44a2753ffbb7c0694a459db9744342f69c6e78b9`. Use the **published 0.4.0 commit** in
   immutable installation pins, and refresh affected scaffold baselines/embedded
   component metadata using that owner's workflow. Preserve Diavisuals and all
   other dependencies, native receipts, computation/capture locks and release
   records. Rebuild only components actually embedding changed package/pin bytes.
3. **Diapora / Unaltrepaper**: the inspected dependency closure declares
   Diavisuals, not Vegavisuals. Add a pinned optional/supported Vega helper only
   with the owner's selected importer implementation and actual adoption proof;
   do not replace/remove the diagram dependency or assert automatic Vega support.
   Required Vega tools for that feature are render, inline render, export and
   check, plus the native freshness tools where managed final outputs are used.
4. **TIG/TIGIT and content consumers**: upgrade only after the relevant importer
   release. Exercise a real retained Vega bundle in web/PDF/slide content, verify
   final domain references and provider-native state, retire temporary jobs and
   relocate the final consumer. No content consumer pins are changed here.

No additional helper dependency is introduced by Vegavisuals: the central
reference suite is test-only. Unchanged renderer/worker digests remain valid.

## Release evidence procedure

Local owner validation completed on 2026-09-24:

| Check | Result |
| --- | --- |
| Central session preflight | Eligible, clean start on `feat/artifact-handoff-v1`, explicit root `/home/benizar/git/vegavisuals`. |
| `make check` | Passed; both committed example outputs regenerated through `render-all`, with byte-identical SVGs and refreshed native fingerprints. |
| `make tests` with both explicit central reference variables | Passed. Final Python 3.10.20 and 3.14.5 runs each report 144 tests, with four runtime-smoke tests intentionally gated out of the host suite and exercised separately. |
| `make mcp-build` / `make mcp-check` | Passed with the pinned private MCP environment. |
| `make mcp-smoke REQUIRE_DOCKER=1` | Passed for CLI and factory-Make transports, file/inline bundles with both engines, plus all three Docker smoke cases. |
| `make docker-smoke REQUIRE_DOCKER=1` | Passed: both engines/all formats, isolated container cleanup, retained-data relocation and SVG reproduction after producer retirement. |
| `make tests-install` | Passed: Linux wheel/sdist, wheel rebuilt from sdist, non-editable package assets, CLI/native receipt checks and real installed-wheel MCP exports. |
| Independent v1 verifier | Pinned figure/deck fixtures and producer bundles accepted; moved retained bundles remain valid and missing retained data is rejected. |

The observed renderer in these proofs is
`sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e`,
with renderer contract
`28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800`.
These are actual local execution identities, not a claim of registry publication.
Built candidate distributions are `dist/vegavisuals-0.4.0-py3-none-linux_x86_64.whl`
and `dist/vegavisuals-0.4.0.tar.gz`; obtain final published hashes from the release
build rather than promoting uncommitted development artifacts into consumer pins.

Run `make check`, `make tests`, `make mcp-build`, `make mcp-check`,
`make mcp-smoke REQUIRE_DOCKER=1`, `make tests-install` and
`make docker-smoke REQUIRE_DOCKER=1`. Also run the host suite with explicit
`VEGAVISUALS_HANDOFF_REFERENCE` and `VEGAVISUALS_FACTORY_MANAGER` paths to check
the pinned central fixtures and real workspace manager. The regular suite must
remain usable without those optional sibling references. The CI matrix checks
Python 3.10 and 3.14; Docker-unavailable skips do not count as release evidence.

The runtime smoke must prove both engines, file/inline retention, local-data
closure and relocation after producer teardown; installed-wheel MCP smoke must
discover and invoke both new tools. Review modified `.vegavisuals.lock.json`
only as generated native provenance for the committed examples. Generated
bundles, distribution outputs, caches and test virtual environments stay ignored.

On publication, attach the tested full commit, wheel/sdist hashes and the CI
run/PR URL. Only then substitute concrete immutable pins into dependent PRs.
The outstanding coordination gates are published producer artifacts, reviewed
earlier-pilot findings and real importer/runtime adoption proofs.
