# vegavisuals agent guide

Use the startup consumer root for every file operation. Render `.vl.json` and
`.vg.json` sources through the registry; do not invoke `vl-convert` on the host.
Remote data is forbidden. Local data must be relative to the source and remain
inside the consumer root. Do not replace unmanaged or modified outputs without
the caller's explicit `confirm_replace` instruction.

Image and hyperlink URL channels are dependencies and are forbidden. Expected
tool failures are returned as typed dictionaries with `ok: false`.

Inline tools accept JSON with inline values only. Request `include_data` only
when inline SVG or base64 output is actually needed.
