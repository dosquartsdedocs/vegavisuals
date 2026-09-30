# Owner preparation report for the hub — 2026-09-29

## Decision and source state

**Published Vegavisuals v0.4.0 already satisfies the native no-checkout goal on
Linux x86_64. No new code release is needed for this preparation task.** The
published wheel, explicit-interpreter launch, packaged resources, renderer
archive, native receipts and retained bundles passed the acceptance described
below. The documentation gap was the README's checkout-only quick start and
its outdated statement that no prebuilt renderer was published.

- Authoritative checkout: `/home/benizar/git/vegavisuals`, branch `main`.
- Starting HEAD and peeled `v0.4.0`:
  `68c0b231402ae9485cc34ce530dc5239cb0ec194`; initial worktree clean.
- Actual source changes: `README.md` and this report. Changes are uncommitted;
  no preparation issue, PR, tag or publication was created in this session.
- Existing implementation: [PR #9](https://github.com/dosquartsdedocs/vegavisuals/pull/9).
- Existing release: [v0.4.0](https://github.com/dosquartsdedocs/vegavisuals/releases/tag/v0.4.0),
  published 2026-09-26, neither draft nor prerelease.
- Read the owner `AGENTS.md`, the hub's `owner-preparation-prompts.md` and
  `audits/2026-09-28-manual-runtime-baseline.md` under
  `/home/benizar/git/gacontext/src/bash/mcp_factories/`.

This report supersedes the **publication-state** discussion in the historical
[0.4.0 release-candidate handoff](release-handoff-v0.4.0.md). Its integration and
retention obligations still apply. This is the return report for hub collection;
implementation and documentation changes stay in the owner checkout.

## Published identities, downloaded and verified again

All release assets were fetched with `gh release download` and verified against
`SHA256SUMS`; the three distribution hashes also match `release.json`.

| Published asset | SHA-256 |
| --- | --- |
| `vegavisuals-0.4.0-py3-none-linux_x86_64.whl` | `b52ffa743643dd6b5e0320e7a9aa0cd500ea06262b7d5098c0c3f94a379bc0ea` |
| `vegavisuals-0.4.0.tar.gz` | `328d0d275c6d991b821196fd33c5e321a083df178cb56d9a826e351a97497696` |
| `vegavisuals-render-vl-convert-1.9.0-linux-amd64.tar.gz` | `382d2d5dc25d8539787d8089ff7c9fc553d0e8f26d1289f728d87ba4bb090f1f` |
| `release.json` | `51197db1954d345ce029764e939c989c882e9283589b21b129d7510e693789cf` |

The renderer loaded from that archive, with unchanged config/layers, was:

```text
image ID:          sha256:695125943d0fbc3aa7c877babb9a11a6501bbc7ac265b0975bb5657c60439d98
renderer contract: 28bd331ce12ca101b2b1326328b5bd5c8e89a5ecae6f478160ecbbf3c70ed800
platform:          linux/amd64
profile alias:     vegavisuals/render:vl-convert-1.9.0
registry digest:   null (no RepoDigest claim)
base image:        python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1
```

The **archive hash**, **image ID** and **renderer-contract hash** are distinct.
`ensure-renderer` checks the contract label, not `release.json`'s image ID. A
hub selecting the published tuple must compare the exact image ID separately.
Local source builds may satisfy the same contract with a different image ID.

`make tests-install` also produced local, **unpublished** gate artifacts after
the README edit. They are not replacements for the published hashes above:

| Gate artifact in ignored `dist/` | SHA-256 at the gate |
| --- | --- |
| `vegavisuals-0.4.0-py3-none-linux_x86_64.whl` | `4909fe598cae51ada254cf6b4fc6c8600ab2c1ac45fd152fbcc504933d20cb64` |
| `vegavisuals-0.4.0.tar.gz` | `ada2dcbcd8ff56ae46068ce9cc284432733e8a186947560a0e4e6c539cdda3e5` |

The final documentation report is later than that gate build. Do not use these
development artifacts as release pins.

## Current native descriptor and resources

No central H1 fields were added. The existing native descriptor remains schema 1:

| Descriptor | SHA-256 |
| --- | --- |
| Checkout `mcp-factory.yml` | `10ab04d1263dc1ad70c73d2ea7958f9c90e85680de96db18c6244e93d49326fa` |
| Wheel `vegavisuals/factory/mcp-factory.yml`, identical to `src/vegavisuals/factory/mcp-factory.yml` | `2f34535e057197ac27557621e4fc1e713f34ffe857e1522fc4c23fa759166020` |

The package-native descriptor has `install_scope: user`, consumer binding fixed
at startup, package `vegavisuals[mcp]`, `mcp_version: 1.29.0`, profile
`vl-convert-1.9.0`, family `benizar`, and
`checkout_required_for_make_lifecycle: false`. Its static commands start with
`vegavisuals`; query `factory-manifest` through the selected installation to
obtain the **explicit interpreter** commands:

```text
PYTHON=/absolute/path/to/the/installation/bin/python
transport: [PYTHON, -m, vegavisuals.cli, mcp, serve]
environment: MCP_CONSUMER_WORKSPACE=<absolute consumer root>
build: [PYTHON, -m, vegavisuals.cli, ensure-renderer]
check: [PYTHON, -m, vegavisuals.cli, factory-lifecycle-check]
tests: [PYTHON, -m, vegavisuals.cli, self-test]
smoke: [PYTHON, -m, vegavisuals.cli, mcp-smoke]
down: [PYTHON, -m, vegavisuals.cli, --project, <consumer>, down]
```

`factory-manifest` and `mcp client-config` both resolved to the exact external
venv interpreter. Neither needed Make, `${factoryRoot}` or a source checkout.
The package's `runtime.module` metadata is `vegavisuals`; use the actual command
vectors above rather than synthesizing a different module entry point.
Factory-only commands omit the consumer argument; init, serve, render and down
retain explicit consumer binding. `down_all` is absent from discovery commands.

Native contracts remain manifest **1**, lock **2**, inline cache **2**, receipt
**1**, artifact handoff **1**, with typed application errors. Discovery exposed
**15 tools and 8 resources**, matching the native dynamic descriptor's
`mcp.tools`/`mcp.resources` (static descriptor: `mcp.required_tools`).

The wheel's seven resource entries include the agent guide, Dockerfile, worker,
JSON profile, theme, tokens and package descriptor. `factory-check` verified
their presence and static/dynamic parity from `site-packages`. Selected hashes:

| Resource under `vegavisuals/assets/` | SHA-256 |
| --- | --- |
| `Dockerfile` | `e0eaea7c1eaf9033bac95537bd188bd31975deeba7fee21e55730618d6d593ce` |
| `docker/worker.py` | `a82e2fef0ff1bfa12f2e23326c0aa602231ed5f58b0cca8a7b3e1eccfa76786b` |
| `compat/vl-convert-1.9.0.json` | `12c55cc892b3040df7653a1fbc06f5c467e5c72edf67c5b53b1f7f192bc988cf` |
| `themes/benizar.json` | `1d737cf1ab3625c52839e0c2f6c2058f204a80ffd16d5884fdf87066a9283fa0` |
| `tokens/benizar.json` | `5fa4aca3ed70f5f4930a733d6cbe1098821d1f891010aec70d154daf6765e08f` |

The installed `build-renderer --dry-run` resolves its Dockerfile and build
context under the external venv's `site-packages/vegavisuals/assets`, establishing
that source-based preparation is packaged too. This session used the released
archive for real preparation, rather than performing a new image build.

## Tested compatibility, with bounded claims

| Dimension | Actual evidence and limit |
| --- | --- |
| Producer package | Exact **0.4.0**. No broader producer/helper compatibility interval established. |
| Host platform | Linux x86_64, kernel `6.8.0-142-generic`; Docker Engine **27.2.0**, local daemon, amd64. |
| Wheel | `Root-Is-Purelib: false`, `Tag: py3-none-linux_x86_64`. Pip refused this release wheel for `win_amd64`, `macosx_11_0_x86_64` and `linux_aarch64`. |
| Host Python | Fresh published-wheel proof and owner gates: **CPython 3.12.3**. Existing tag CI passed host tests on **3.10** and **3.14**, with installed-package/Docker gates on 3.14. This is sampled coverage of the declared `>=3.10` requirement, not proof of every intermediate/future Python release. |
| MCP / YAML | **mcp 1.29.0**, **PyYAML 6.0.3**. Exact resolved transitive versions retained in `evidence/installed-distributions.json`; their future resolutions were not tested. |
| Renderer runtime | Actual container probe: **Python 3.13.15**, **vl-convert-python 1.9.0.post1**, **Vega 6.2.0**, **qpdf 11.3.0**, Debian `fonts-dejavu-core` **2.37-6**. |
| Vega-Lite | Real local-CSV SVG rendering at **5.8, 5.14, 5.15, 5.16, 5.17, 5.20, 5.21, 6.1, 6.4**; default 6.4. This is a discrete supported set, not the continuous interval 5.8–6.4. Negative probes rejected 5.9, 6.0 and 6.5. |
| Engines / formats | Both Vega-Lite and raw Vega: **SVG, PNG, PDF** from local data through installed-wheel stdio. |
| Local data | **CSV** for Vega-Lite, **JSON** for raw Vega; exact dependency hashes in receipt and retained bundles. TSV is a native documented capability, not an extra real-render claim from this session. |
| Native state | Nonempty receipt v1 with **2 inputs and 6 artifacts**, lock v2, freshness, stale-input receipt invalidation and restoration. |
| Bundles | Six file-output bundles plus two inline bundles; eight verified after relocation, producer removal, and later renderer retirement. |

The current tag CI was inspected via `gh`, not inferred from the old audit:
[run 36238888017](https://github.com/dosquartsdedocs/vegavisuals/actions/runs/36238888017)
is successful at the exact release revision. ARM/aarch64, other architectures,
macOS/Windows host execution, alternative Docker engines and remote daemons were
not validated. Linux publication requires `flock`, descriptor-relative I/O and
fail-closed `renameat2`; a differently tagged source build does not establish a
new supported platform.

## Acceptance procedure and owner gates

Session-only harness, downloaded assets, synthetic consumers and raw evidence:

```text
/tmp/opencode/vegavisuals-owner-20260929/
```

The unmodified published wheel was installed **non-editably**, outside the
checkout. The proof ran there with `PYTHONPATH` removed, checked
`source_checkout() is None`, and resolved all package resources from that venv.
Synthetic charts and local datasets were authored in the harness, independently
of any manual. Renderer runs mounted only isolated staging at `/output`, used
the exact image ID, `--network none` and `--pull never`.

Exact principal commands (working directories stated explicitly):

```bash
# Owner checkout; downloads go to the independent session directory.
gh release download v0.4.0 --repo dosquartsdedocs/vegavisuals \
  --dir /tmp/opencode/vegavisuals-owner-20260929/release

# Working directory: /tmp/opencode/vegavisuals-owner-20260929/release
sha256sum --check SHA256SUMS
python3 -m venv /tmp/opencode/vegavisuals-owner-20260929/venv
/tmp/opencode/vegavisuals-owner-20260929/venv/bin/python -m pip install \
  --disable-pip-version-check \
  '/tmp/opencode/vegavisuals-owner-20260929/release/vegavisuals-0.4.0-py3-none-linux_x86_64.whl[mcp]'

# Working directory: /tmp/opencode/vegavisuals-owner-20260929
python3 prepare.py
env -u PYTHONPATH /tmp/opencode/vegavisuals-owner-20260929/venv/bin/python proof.py

# Working directory: /home/benizar/git/vegavisuals
# Prefix used on EVERY owner gate below; the test guard is described next.
export PATH="/tmp/opencode/vegavisuals-owner-20260929/bin:$PATH"
make check
VEGAVISUALS_HANDOFF_REFERENCE=/home/benizar/git/gacontext \
VEGAVISUALS_FACTORY_MANAGER=/home/benizar/git/gacontext/src/bash/mcp_factories/mcp-factory-manager.py \
  make tests
make tests-install
make mcp-build mcp-check
make mcp-smoke REQUIRE_DOCKER=1
make docker-smoke REQUIRE_DOCKER=1

# Working directory: /tmp/opencode/vegavisuals-owner-20260929; after all gates
python3 finish.py
env -u PYTHONPATH /tmp/opencode/vegavisuals-owner-20260929/venv/bin/python verify-retained.py
```

| Gate | Final result |
| --- | --- |
| Published asset verification | All four `SHA256SUMS` entries passed. |
| Installed-wheel `proof.py` | Passed: lifecycle, explicit launch, resources/platform checks, both engines, six outputs, receipts, eight bundles, relocation and byte-identical SVG re-render from retained data. |
| Prepared startup | Two successful `ensure-renderer` calls returned `built: false`; two complete stdio sessions passed. |
| `make check` | Passed. |
| `make tests` with explicit central references | 150 discovered, 146 passed, 4 runtime-gated cases skipped here and exercised below. |
| `make tests-install` | Passed: wheel/sdist, sdist-built non-editable wheel, resource/manifest/license/platform checks, package-native smoke and installed CLI stdio/bundle test. |
| `make mcp-build mcp-check` | Passed, reusing the prepared image and existing stamped MCP environment. |
| `make mcp-smoke REQUIRE_DOCKER=1` | Passed: one CLI transport case, one Make transport case, three real Docker cases. |
| `make docker-smoke REQUIRE_DOCKER=1` | Three passed: both engines/all formats, delayed PDF repeatability, project-scoped cleanup and retained-data relocation/re-render. |
| `verify-retained.py` after image removal | All eight bundles passed with ordinary Docker PATH, no producer directory and no test renderer remaining. |
| Visual inspection | Both PNGs show the three expected labelled bars (4, 7, 5), with readable axes. |

Initial harness drafts were corrected to match native manifest keys, unique
source entries and the dynamic descriptor's `mcp.tools`; no product fix was
needed. One attempt stopped on host `ENOSPC`. A chained gate invocation later
hit the tool's 120-second wall-clock limit; `mcp-smoke` was rerun separately with
a 300-second limit and passed, then `docker-smoke` passed separately. MCP's
resolved `pydantic-settings` emitted an `IncompleteFieldDefinitionWarning` on
stderr; protocol discovery, calls and results still passed.

## Isolation and disk cleanup

The pre-existing profile alias selected image `46aa…`, different from the
published `6951…` image. Loading the archive unchanged would overwrite that
alias. The test harness therefore changed **only archive tag metadata** to
`vegavisuals-owner-preparation:20260929-v040`, verified unchanged config hash /
image ID and renderer-contract label, and loaded that distinct alias.

A **test-only** Docker guard mapped native inspection of the fixed profile
alias to the test alias. Actual rendering then used the immutable published
image ID. The guard logged and rejected build/pull operations and bounded
cleanup; all permitted Docker operations were real. This is test isolation,
**not a native alternate-image selector**. Package/profile bytes were unmodified.
An isolated daemon remains the native way to test a different image behind the
same fixed alias without that harness.

Across the proof and owner gates, the guard recorded **195 calls, 65 real render
runs, zero builds/pulls and zero denied calls**. A separate read-only container
probe obtained the runtime versions reported above. These protected IDs matched
before testing and after cleanup:

| Protected selection | Preserved image ID |
| --- | --- |
| `vegavisuals/render:vl-convert-1.9.0` | `sha256:46aaea98bb22103cc01d7c85e2230ed04137c2d5c34a5aae6d58cd64dd61e29e` |
| `tigit-compute-python:local` | `sha256:22ed1731046c75bc968890159d944de07d59904efc44a7d3d7b1550944fcbd76` |
| `tigit-compute-r:local` | `sha256:6192f629aff627eb438d02bbe700aa95f2b2a5408f1c15a240ef101af3cf42f2` |

No manual source/configuration, active client registration or consumer helper
installation was targeted. Synthetic cleanup was project-scoped. No global
Docker prune, broad container stop, compute rebuild or consumer upgrade ran.

Following the user's disk-space request, diagnostics found the filesystem at
99% (4.5 GiB available when measured), healthy inode availability, no Vegavisuals
containers and only the protected renderer plus the newly loaded test image.
Docker reported 83.2 GB of shared build cache; this was not owner-specific
disposable evidence. Cleanup removed the already-loaded approximately 220 MB
temporary tar and, after all tests, the exact test alias/image with no force and
after confirming no containers referenced it. Free space measured approximately
11 GiB immediately after cleanup and 6.7 GiB (99% used) at the final review;
the changing overall capacity is not all attributable to this session's cleanup.
The protected renderer and TIGIT images remain intact. Downloaded release assets,
small test bundles and evidence remain available locally.

## Retained evidence and next hub action

Raw evidence is session-local, **not a published artifact**. Key SHA-256 values:

| File relative to the session directory | SHA-256 |
| --- | --- |
| `evidence/summary.json` | `3882b41cda6d7424b8e385648ea4c8328396d60bfb3d188621d50dac3c68240b` |
| `evidence/final-audit.json` | `16960aa7bee4f435223ab247a2938f8e4917ad9c6f497e61f26b090627d3809f` |
| `evidence/factory-manifest.json` (command/result envelope) | `46f17adaf16e66c74c9ffd5ed16d4bd8807b703bccc5802a85cda82ca0f7acff` |
| `proof.py` | `09388cee1b8c46d902cc7b1fa8db0dee988e790f97a39ad9fdeb7d481bffe051` |
| `bin/docker` | `e103dd83d417349cd4142e1817d29810aa212f96e25c68d3972cdb38103c0f27` |

The original native receipt hash was
`ed3ba2c6e7420f9ad4354b15adfa100edeef96f09da2edbfacedc7b582730608`,
with request hash
`131dd513980de78267dc25f0cc736a53ef51c4d31abfe928f82c84fde968f882`.
Export did not change it. Its parsed contents and the native lock are retained
in `evidence/receipt.json` and `evidence/lock.json`.

All bundles are below `relocated/retained/`; each row identifies its `bundle.json`:

| Bundle directory | SHA-256 |
| --- | --- |
| `vega-lite-svg` | `8af0adfd02cedb0b4a7748c3b9a8d7bbdebd98a5f6f126775c0bff10b3137d8a` |
| `vega-lite-png` | `6cc92e5db7fbefa2a5bdeeb05f546243624882595c693446aafc9887b7dfb5d0` |
| `vega-lite-pdf` | `0fa54848f2d22055abcaba0395fe8d22fe4e8b2789fb30d3c9ddf5fabc726d27` |
| `vega-svg` | `b57852f38accc15048fa1be408e1c7170b36c0823db15cd1253c7341dd0afedc` |
| `vega-png` | `a5832e0389b205e360ab138769c91531efcc9ac3f0f76452f46e107129342d1d` |
| `vega-pdf` | `dcaf1e8b7032a12fab36bd476dd786352632aba7eb2927c2b1e3363ae7528375` |
| `inline-vega-lite` | `83a48bf3f66c328c9e443ad9dd8f5ccb9f24c8d81ea2c83665f3fef593dfff30` |
| `inline-vega` | `a17f04bf09a2225bf8347fb10fc24fbcded6ec00e32d201178af7c6be36593ba` |

Next action belongs to the hub's accepted H1 integration:

1. Consume the existing hashed release, package-native descriptor and exact
   image tuple above. No additional Vegavisuals release is a prerequisite.
2. Integrate the accepted catalogue/range model when available, distinguishing
   declared compatibility from exact installed selections and the discrete
   tested sets above. No unaccepted schema fields or invented ranges were added.
3. Use the selected installation's explicit-interpreter commands. Keep image
   preparation separate from stdio startup and compare exact runtime identity
   when selecting the published archive. Plan fixed-alias collisions explicitly.
4. Keep live client switching, real manual/helper upgrades and importer acceptance
   as separate operations. These producer proofs establish native receipt/bundle
   capability, not every downstream web/PDF/slide importer combination.
