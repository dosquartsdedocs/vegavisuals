# vegavisuals agent guide

Use the startup consumer root for every file operation. Render `.vl.json` and
`.vg.json` sources through the registry; do not invoke `vl-convert` on the host.
Remote data is forbidden. Local data must be relative to the project root and remain
inside the consumer root. Do not replace unmanaged or modified outputs without
the caller's explicit `confirm_replace` instruction.

Image and hyperlink URL channels are dependencies and are forbidden. Expected
tool failures are returned as typed dictionaries with `ok: false`.

Inline tools accept JSON with inline values only. Request `include_data` only
when inline SVG or base64 output is actually needed.

Opt in to `export_visualization_bundle` after a fresh managed render. Supply a
new durable bundle directory and the exact original text for inline output.
Retain the whole returned bundle tree and its manifest SHA-256. Verify with
`check_visualization_bundle` after transport or relocation. The first export
ensures ignored private staging under `.cache/vegavisuals/handoff`; failed
publication preserves recovery trees. Bundle export does not rewrite native
locks/receipts into integration records. Final mappings and content references
belong to the importing factory. Cleanup is explicit, after verified integration
and runtime teardown; preserve originals and selected author-owned variants.
