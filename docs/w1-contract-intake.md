# W1 intake and marker interoperability question

Status: **contract intake/reservation only; implementation and release pending**.
This document does not declare Vegavisuals W1 conformance or opt the current
published profile into job-volume storage.

## Verified starting point

- Authoritative checkout: `/home/benizar/git/vegavisuals`.
- Base: `9788de7129e704fc9028d536008a28d629cdd506`; clean and synchronized `main`
  before creating `feat/w1-job-storage` in the same checkout.
- Preflight after branch creation: eligible, no active competing session/lease,
  one primary checkout. The diagnostic does not retain an exclusion lease.
- Owner reservation: [Vegavisuals #14](https://github.com/dosquartsdedocs/vegavisuals/issues/14).
- Hub coordination: [gaContExt #22](https://github.com/dosquartsdedocs/gacontext/issues/22).
- Current published provider: v0.5.1. No prior release, receipt or runtime alias
  was changed during this intake.

The requested immutable reference is
`83cb0d3e2f424759475ae70b423a0f8dca8520b2`. GitHub confirms it is an ancestor of
merged [gaContExt PR #23](https://github.com/dosquartsdedocs/gacontext/pull/23),
merge `f847fb118c6f5cd84767c49c87f8b9c65bd790ae`, two commits ahead. Read and
compared to the pin:

- `JOB_STORAGE_CONTRACT.md` and `job-storage-v1.schema.json`.
- `mcp_job_storage/contract.py`, `planner.py`, `cli.py` and package exports.
- `examples/job-storage/` provider/state/retention examples and README.
- `tests/test_mcp_job_storage.py`, including its 19 positive/negative methods.
- `RUNTIME_CONTRACT.md` for the existing D0 boundary.

The compared local files had no diff from the pinned reference. The schema's
exact SHA-256 is
`25e2bf047715e170ad8975ca3fd81d0597bdb0fead9ac6c52e8a777b3fb6434c`.

## Clarification returned before fixing a native convention

See the [hub question](https://github.com/dosquartsdedocs/gacontext/issues/22#issuecomment-6048930280).

The common binding mount exposes exactly `id`, `name`, `role` and
`marker_sha256` (`job-storage-v1.schema.json`, lines 139–143), with additional
properties rejected. The contract requires the worker to check the mounted
markers before I/O (169–177) and treats manager markers specially when accounting
for otherwise undeclared protected content (71–75, 120–122). It does not name a
marker locator/encoding or a generic discovery algorithm.

A provider-owned standalone manager can choose an internal convention, but doing
so alone would not establish the common worker/shared-manager interoperability
requested by W1. The outstanding question is which reviewed convention applies:

1. A reserved marker pathname and encoding.
2. Discovery at the volume root by the exact bound digest, with explicit treatment
   of multiple/unknown candidates.
3. An intentionally manager-native locator supplied through authenticated context
   outside the common binding, with that boundary explicitly agreed.

This is a request to clarify the contract boundary, **not** an assertion that the
reference planner is a Docker executor or that a private implementation choice
would itself be a schema extension. No `marker_path` or other field has been added
to the common schema/bindings, and no incompatible convention has been shipped.

## Read-only reproducible probe

The probe loads schema/examples directly from Git blobs at the pinned revision,
uses an independent Draft 2020-12 validator, constructs a matching synthetic
writer binding and marker digests from the volume-label tuples, and shows:

- Provider, state and binding conform to the pinned JSON Schema.
- The same binding does not distinguish alternative internal marker locations.
- Adding `marker_path` to a binding mount is rejected.

This is **schema expressibility evidence**, not real volume/lifecycle acceptance.
No Docker creation/removal or provider execution is part of the probe.

```bash
.tmp/install-venv/bin/python /tmp/opencode/vegavisuals-w1-intake/marker_probe.py
```

| Local evidence | SHA-256 |
| --- | --- |
| `/tmp/opencode/vegavisuals-w1-intake/marker_probe.py` | `18a8f445de5ca68b770d6b00ccd4c0f30492242c77034f8d53625c69fcea7f71` |
| `/tmp/opencode/vegavisuals-w1-intake/probe.json` | `14f558201cf16ddced224b3d75a1ea50ddf5d251524008490c9aacf847c4ac8d` |

## Remaining owner work

After the common marker boundary is clarified, implement the selected W1 profile
and its authenticated manager context, five-root companion, distinct retained/
scratch volumes, status/quiesce/seal, source/resource closure, leases/CAS/epochs,
receiver-verified retention and D0-scoped lifecycle. Keep monitored budgets honest,
including actual daemon-filesystem capacity, reservations, byte observations and
blocked retention. The proposed changed-profile release is 0.6.0, not yet prepared.

The required real/local-data relocation, two-client/busy/last-user/crash/transfer,
wrong epoch/daemon/volume, pressure/reactivation and installed release gates remain
**pending**. No implementation PR, new release, descriptor hash or W1 acceptance
is claimed by this intake. The companion opt-in file is intentionally not installed
before the corresponding capability exists. No gacontext filesystem, consumer,
registration, shared image or volume was changed.
