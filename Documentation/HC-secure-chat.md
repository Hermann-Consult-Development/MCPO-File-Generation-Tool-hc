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

Secure generation supports DOCX, PDF, XLSX, CSV and PPTX with text and tables.
Embedded images and image search are explicitly rejected in this mode: the
legacy PDF renderer reads local image paths and arbitrary network image URLs.
A safe image-input policy is future work; omitted images are not reported as a
complete successful export. Legacy SSE/Python entrypoints are unchanged and
are not the secure chat deployment path.

## Qualification and release

`dockerfile.chat` preserves the immutable previously deployed HC MCPO runtime
and applies separately pinned parser/runtime security corrections. The normal
legacy Dockerfiles remain separate. The tag-only HC workflow builds
`owui-mcpo:v0.8.1-hc.1` and `owui-file-export-server:v0.8.1-hc.1`; it does not
publish `latest` or a legacy SSE image. PR builds do not publish. Record the
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
