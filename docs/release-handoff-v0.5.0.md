# Release handoff: Vegavisuals 0.5.0 control plane

## Scope and native interface

0.5.0 releases the startup-fixed immutable renderer selector introduced by the
[owner follow-up](owner-followup-2026-09-29.md). The Python/MCP control plane,
wheel, sdist and native discovery manifests advance together. Published renderer
profile, worker, Dockerfile, theme, tokens and font requirements remain unchanged.

Select a prepared **full lowercase local Docker image ID** using one of:

- Global CLI option `--renderer-image-id sha256:<64 hex digits>`.
- Startup environment `VEGAVISUALS_RENDERER_IMAGE_ID`.
- Python `Registry(root, renderer_image_id=...)`.

Explicit API/CLI values override the environment; the registry captures selection
once. Tags, shortened IDs and `repository@sha256:…` references are rejected.
Resolution of tags/RepoDigests and verified acquisition belong to the caller.
H2 must pass the resulting full local image ID and preserve existing aliases.

Explicit ensure inspects only, requiring the expected image ID **and** the
unchanged renderer-contract label. Explicit build is rejected. Inspection errors,
missing/incompatible images and partial failed-inspection output cannot become
cache hits or build/pull fallbacks. Runtime selection reaches fingerprints,
freshness, cache, native provenance, bundles and generated launch configuration.
Receipt v1 retains its source/artifact hash contract and requires selected-runtime
freshness before issuance. Retained bundle verification stays offline.

Normal profile-based behavior remains available with the selector unset. The
existing registry-code fingerprint makes prior managed outputs stale when the
control-plane code changes; regenerate them through the provider after an
intentional consumer upgrade. No real consumer upgrade is part of this release.

## Unchanged renderer delivery

Reuse this exact archive, originally published with v0.4.0:

```text
archive: vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz
archive SHA-256: 382d2d5dc25d8539787d8089ff7c9fc553d0e8f26d1289f728d87ba4bb090f1f
profile: vl-convert-1.9.0
profile SHA-256: 12c55cc892b3040df7653a1fbc06f5c467e5c72edf67c5b53b1f7f192bc988cf
renderer contract: 28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800
image ID: sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98
platform: linux/amd64
registry RepoDigest: none claimed
```

The archive carries the legacy `vegavisuals/render:vl-convert-1.9.0` alias. Load it
only in a fresh/approved environment or transport its verified config/layers
without tag metadata when preserving a differing shared alias. Never edit the
profile's `image` field to implement selection: its bytes are part of the
published renderer contract. CI reuses the verified archive in its fresh runner.

The compatible local image ID
`sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e`
is a second acceptance point with the same contract, not a registry-distributed
artifact or a certification of other arbitrary images carrying a label.

## Final release gates

Use the exact final-version tree. Preserve the historical 0.4.0 and `.dev0`
reports as historical evidence, not replacements for these gates:

```bash
make check
VEGAVISUALS_HANDOFF_REFERENCE=/absolute/gacontext \
VEGAVISUALS_FACTORY_MANAGER=/absolute/gacontext/src/bash/mcp_factories/mcp-factory-manager.py \
  make tests PYTHON=/absolute/python
make tests-install PYTHON=/absolute/python
make mcp-build mcp-check
make mcp-smoke REQUIRE_DOCKER=1
make docker-smoke REQUIRE_DOCKER=1
```

Run the host matrix on Python 3.10 and 3.14; record any additional sampled host
versions separately. Run installed acceptance and stdio on the final wheel and
sdist-built installation. Prove native A/B/A freshness/cache transitions with
Vega-Lite and Vega/local data, explicit missing/incompatible failures, receipts,
bundles, unchanged aliases and bundle verification after retiring only owned
test images. Render containers must use the actual immutable ID, no runtime
network, `--pull never`, and only isolated staging mounts.

## Receipt and publication

After source review, PR checks and protected-branch integration, bind delivery to
the approved full Git revision. The release's `release.json` and `SHA256SUMS`
must identify:

- Version/tag `0.5.0` / `v0.5.0`, source revision, implementation PR and CI URLs.
- Final wheel/sdist filenames, SHA-256 and byte sizes.
- The unchanged renderer archive hash, profile bytes/contract, exact image ID,
  platform and lack of a claimed registry RepoDigest.
- The matrix actually executed, installed acceptance, native selector semantics,
  the two exact compatible image IDs, and retained evidence identities.

Publish new assets under v0.5.0; never replace the older release's assets. Download
the delivered assets through GitHub, verify every checksum and reinstall the
downloaded wheel before announcing availability. Record the tag CI and delivery
verification in the final owner closeout report/release notes. Artifact hashes
belong to the final release receipt rather than a self-referential source file.

The wheel/renderer distribution platform is Linux x86_64/amd64. No other host
architecture, continuous helper-version interval or full Python-version interval
is inferred from sampled tests. H1 metadata adoption, H2 activation and real
manual/helper migrations remain separate reviewed operations.
