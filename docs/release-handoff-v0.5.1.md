# Release handoff: Vegavisuals 0.5.1 runtime identity

0.5.1 adds [runtime identity schema 1](runtime-identity-v1.md) through CLI
`runtime-identity`, MCP tool `runtime_identity` and resource
`vegavisuals://runtime/identity`. The startup-bound immutable image selector and
all 0.5.0 rendering, freshness, cache, receipt and bundle contracts are preserved.
Inventories grow to 16 tools and 9 resources; prior names/arguments remain valid.

The query reports imported package version separately from startup/current disk
observations, interpreter, bound workspace, PID/instance start, selected/observed
image ID, recorded RepoDigests, contract and explicit discrepancies. It performs
no preparation/build/pull/retagging, writes no consumer state, and returns neither
raw origin URLs/environment nor consumer contents. Full local image IDs remain
the only explicit selector; reference resolution/acquisition is caller work.

The patch version is an additive diagnostic release. It is not a replacement of
any 0.5.0 tag, source, wheel, sdist, receipt or image archive. H2 can use both
published releases for coexistence/reconnection/rollback, but 0.5.0 lacks the new
identity query: capability absence must not be treated as proof of a new server.
Use the existing MCP connection to identify an active server; CLI identifies a
new invocation. Disk snapshots are observations, not memory/peer attestation.

## Reused renderer

```text
profile: vl-convert-1.9.0
profile SHA-256: 12c55cc892b3040df7653a1fbc06f5c467e5c72edf67c5b53b1f7f192bc988cf
renderer contract: 28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800
image ID: sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98
archive: vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz
archive SHA-256: 382d2d5dc25d8539787d8089ff7c9fc553d0e8f26d1289f728d87ba4bb090f1f
registry RepoDigest: none recorded/claimed for the archive-loaded image
```

Reuse the verified archive without changing shared aliases. Its original tag
metadata may be omitted during caller-controlled import of unchanged config and
layers. The provider query itself never loads an archive. Renderer resources and
profile bytes stay unchanged; no new renderer build is needed.

## Gates and publication

Run the owner host matrix including Python 3.10 and 3.14, `make check`,
`make tests-install`, `make mcp-build mcp-check`,
`make mcp-smoke REQUIRE_DOCKER=1` and `make docker-smoke REQUIRE_DOCKER=1`.
Include the explicit central reference/workspace checks locally. Regenerate the
example lock through the provider when the existing registry-code fingerprint
changes; preserve byte-identical example SVGs and renderer assets.

Installed acceptance must prove:

- CLI and live MCP tool/resource identity with stable per-instance values and
  independent consumers, before init/render side effects.
- Loaded version remaining stable while isolated on-disk version/installer
  metadata changes; explicit drift with no reload or reflected secrets.
- Missing/incompatible images and offline local inspection, with no preparation
  or hidden fallback and unchanged shared aliases.
- Coexistence with the separately installed published 0.5.0, reconnection/new
  instance IDs, rollback to 0.5.0 with capability absence recorded accurately,
  and retained bundle verification after retiring only owned test images.

After review, protected PR/CI integration and tag CI, build final wheel/sdist from
the approved source. Bind their hashes, source/PR/CI, the versioned native API and
unchanged renderer/profile/image identity in a new `release.json` and checksum
file. Publish only new 0.5.1 assets, download them again, verify all hashes, then
exercise the downloaded installation before reporting availability.

This delivers a native capability for H2B/H2C. It does not edit gacontext (including
the referenced H2A revisions), activate global clients, change manual/helper pins
or certify untested architecture/helper/Python intervals. The final owner report
must distinguish actual installed points, image IDs and RepoDigests.
