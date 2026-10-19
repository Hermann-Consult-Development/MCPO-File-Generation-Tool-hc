# HC secure chat exports

The opt-in `REQUIRE_USER_AUTH=true` path uses the Docker MCPO implementation.
Its five tools require the forwarded user's bearer token; the legacy admin
token fallback is unavailable. Generated files and archives are uploaded to
that user's Open WebUI file store with `process=false`, and tools return native
authenticated `/api/v1/files/<id>/content` links. A failed upload returns an
error, never a public download URL or server path. Temporary generated files
are removed after secure operations, including errors.

The file server disables its `/files` route and static mount in secure mode.
Configure both services with the flag and keep the file server's Traefik route
disabled. MCPO header-debug logging is disabled. All tool-entry output names
must be plain filenames, including archives, edits and reviews. Outbound HTTP
requests have bounded timeouts and never follow redirects with credentials.

Secure generation supports DOCX, PDF, XLSX, CSV and PPTX with text, tables
and images. Legacy SSE/Python entrypoints are unchanged and are not the secure
chat deployment path.

### Images (hc.4)

Up to hc.3 this mode refused every image, because the legacy renderers read
local image paths and arbitrary network URLs. Since hc.4 images come from four
kinds of source only (`tools/secure_images.py`):

1. The caller's own Open WebUI files: an image block with `file_id`, or an
   `/api/v1/files/<id>/content` link. They are read through Open WebUI with the
   forwarded token, and the file must belong to the caller; a file the caller
   could only read through sharing or an admin role is refused.
2. The configured image search (`IMAGE_SOURCE` `unsplash` or `pexels` with its
   key). Results are fetched only from `images.unsplash.com` or
   `images.pexels.com`. `local_sd` is not available in this mode. Unsplash's
   attribution and download-tracking rules are not implemented.
3. Public `https` URLs on port 443. Every address of the host must be public
   (no private, loopback, link-local, shared, multicast or reserved ranges,
   including IPv4 embedded in IPv6), on every redirect (at most three); the
   connection goes to the checked address and the certificate is verified for
   the host name. No credentials in URLs, no compressed bodies, no proxy.
4. Inline `data:image/...;base64` images.

Local paths and every other scheme are refused before anything is read. Each
image is limited in size (`IMAGE_MAX_BYTES`, default 10 MB, and
`IMAGE_MAX_TOTAL_BYTES`, 50 MB per tool call), number (`IMAGE_MAX_COUNT`, 20 per
tool call) and time (`IMAGE_TIMEOUT`, 20 s in total; the connection is closed at
the deadline). It is decoded only as PNG, JPEG, GIF, WebP or BMP within
`IMAGE_MAX_PIXELS` (40 million) and re-encoded as PNG or JPEG (longest side at
most 2400 px, metadata dropped) before it is placed. An image that cannot be
used fails the whole file with a message that names it; images are never left
out silently. For the same reason PDF images inside lists, tables, quotes or
headings, and DOCX or PPTX images inside text, are refused with a hint where
to put them.

## Qualification and release

`dockerfile.chat` preserves the immutable previously deployed HC MCPO runtime
and applies separately pinned parser/runtime security corrections. The normal
legacy Dockerfiles remain separate. The tag-only HC workflow builds
`owui-mcpo` and `owui-file-export-server` with the pushed tag (for example
`v0.8.1-hc.4`); it does not publish `latest` or a legacy SSE image. PR builds do not publish. Record the
qualified image digests in the central infra release catalog before release.

Run `tests/test_secure_exports.py` only in an isolated test environment with
the real file-generation dependencies. The explicit `tests/qualify_owui.py`
probe requires a private synthetic credential fixture and disposable Open
WebUI. It creates/reuses an HC security-test identity, reuses the existing
ordinary test user, performs no inference and never prints credentials.

Initial remote qualification on 2026-10-07 produced real files in all five
formats, including German filenames: owner download 200, other HC test user
404, anonymous 401 for every artifact; unauthenticated generation denied.
This is file/API qualification, not a claim of full customer workflow approval.
All nine focused tests also pass with the image's default packaged templates,
in addition to the blank-template fixtures. The XLSX template places its table
below branded headings; validation checks preserved content rather than
assuming the first data row is always row 2.

### Artifact fidelity correction in hc.2

Deeper artifact acceptance found that the packaged XLSX template inserts the
input grid at B5 but hc.1 left formulas pointing at their old A1-based positions.
A real spreadsheet recalculation produced zero and `#VALUE!` instead of the
requested totals. Formulas now move with the supplied grid: local A1 references,
including `$` anchors and explicitly qualified references to the output sheet,
are relocated. References to existing other sheets remain unchanged because
this tool does not move those sheets. Quoted text remains text. Missing sheets,
named/structured ranges, `INDIRECT`, external workbooks and multi-sheet ranges
return an explicit error. Only one supplied grid is supported. Cached formula
values are not invented; a spreadsheet application calculates them when opened.

The PDF renderer now splits tall table rows across pages, repeats the header,
and fits tables within the A4 text frame. A remaining layout error returns a
failed operation without uploading a replacement document. Previously, an
oversized cell could produce a successful download containing only `Error in
PDF generation`. DOCX table borders are inserted in valid OOXML element order;
the previous placement failed schema validation even though LibreOffice opened it.

`tests/test_artifact_fidelity.py` adds actual workbook/PDF/DOCX regressions.
The test-only `pypdf` dependency is downloaded on the CI runner, then installed
into a temporary test directory inside the network-disabled container; it is
not added to the product image. Re-run the existing authorization tests as well
as these artifact checks with blank and packaged templates. Opening, rendering
and recalculating in a compatible Office viewer remain separate acceptance
checks; a correct MIME type or successful upload does not prove file fidelity.

The first dependency scan of the fixed predecessor found 2 critical and 37
high Python findings. The targeted corrections remove the directly used
`lxml`/Pillow parser findings and update PyJWT, AnyIO, urllib3, multipart,
py7zr and soupsieve. `pip check` and real five-format qualification are required
after these corrections. PyJWT is pinned to 2.15.1, including follow-up fixes
beyond the scanner's original 2.14.0 floor.

The scan is **not clean**. A follow-up correction also pins cryptography50.0.2
and MCP1.28.1; dependency checks, nine tests and actual five-format user-owned
downloads pass with the default packaged templates after that change.
The final scan retains Starlette findings plus inherited OS and Node/npm
findings. Starlette's flagged Windows StaticFiles and form parsing paths are
not used by these Linux JSON tool routes. A major Starlette/FastAPI transition
is outside this bounded change. Inherited OS libraries still need release-owner
review; absence of a demonstrated call path is not proof of non-exploitability.
Node/npm archive tools are not invoked by the fixed Python
tool configuration. Archive generation/editing uses Python's zipfile/tarfile,
not Node tar. The OS scanner also reports SQLite and minizip-related zlib
findings without a Debian fixed version. These are recorded rather than hidden
with an ignore file. The separate alpha/runtime refresh remains open.

## Relationship to infra issue 87

The existing `alpha-with-pdf-tables` branch at `79a7c54` has the modular alpha
layout and unicode fixes, but still returns public generated-file URLs and
serves anonymous downloads. This bounded master-based security change retains
the HC PDF/DOCX table patches and does not perform the unrelated alpha
migration or Blankstahl/Uppenbrink rollout. Issue 87 remains open. When that
migration proceeds, port this authenticated ownership boundary and its tests
into the modular implementation; do not restore public generation URLs.
