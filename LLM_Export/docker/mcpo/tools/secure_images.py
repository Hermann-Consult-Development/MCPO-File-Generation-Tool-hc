"""Images for authenticated exports, from four kinds of source only.

1. The caller's own Open WebUI files (uploaded or generated in a chat), read
   through Open WebUI with the caller's forwarded token. Files of other users
   are refused even when the caller could read them.
2. The configured image search (Unsplash or Pexels). Its results are fetched
   only from that provider's image hosts.
3. Public https URLs on the standard port. Every address the host resolves to
   must be public, on every redirect, and the connection goes to the address
   that was checked, so a second DNS answer cannot move it elsewhere.
4. Inline data: images.

Local paths and every other scheme are refused before anything is read. Each
image is decoded with a fixed set of Pillow decoders under size and pixel
limits and re-encoded before it is placed in a document. An image that cannot
be used fails the whole file with a message that names it; nothing is
silently left out.
"""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import http.client
from io import BytesIO
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
import warnings

import certifi
from PIL import Image, ImageOps
import urllib3
from urllib3.connection import HTTPSConnection

from .file_security import FilePolicyError, file_id as checked_file_id, http_get

log = logging.getLogger("file_export_mcp.images")

MAX_BYTES = int(os.getenv("IMAGE_MAX_BYTES", 10 * 1024 * 1024))
MAX_TOTAL_BYTES = int(os.getenv("IMAGE_MAX_TOTAL_BYTES", 50 * 1024 * 1024))
MAX_PIXELS = int(os.getenv("IMAGE_MAX_PIXELS", 40_000_000))
MAX_COUNT = int(os.getenv("IMAGE_MAX_COUNT", 20))
TIMEOUT = float(os.getenv("IMAGE_TIMEOUT", 20))
MAX_SIDE = 2400          # longest side after re-encoding; enough for A4 at 300 dpi
MAX_REDIRECTS = 3
MAX_URL_LENGTH = 2048
FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")
FORMAT_NAMES = "PNG, JPEG, GIF, WebP or BMP"
PROVIDER_HOSTS = {"unsplash": {"images.unsplash.com"}, "pexels": {"images.pexels.com"}}
USER_AGENT = "HC-File-Export (+https://github.com/Hermann-Consult-Development/MCPO-File-Generation-Tool-hc)"
SOURCE_HINT = ("use the user's own Open WebUI image (file_id), the image search (query), "
               "a public https URL or a data: image")

# Tests point these at a local TLS server; production always uses 443 and certifi.
_HTTPS_PORT = 443
_CA_BUNDLE = certifi.where()

_OWUI_FILE = re.compile(r"/api/v1/files/([A-Za-z0-9_-]{1,128})(?:/content)?/?(?:\?[^#]*)?")
_DATA_URL = re.compile(r"data:image/(png|jpeg|jpg|gif|webp|bmp);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_IMAGE_LINE = re.compile(r"\s*!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)\s*")
_INLINE_IMAGE = re.compile(r"!\[[^\]]*\]\(|<\s*img\b", re.IGNORECASE)
_REDIRECTS = {301, 302, 303, 307, 308}
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")
_SAFE_PATH = "/%:@!$&'()*+,;=-._~"

_scope = ContextVar("image_scope", default=None)


class _Unavailable(Exception):
    """The image could not be fetched; reported as one generic message."""


class _Refused(Exception):
    """A URL (first or redirected) breaks a rule; the reason is safe to show."""


@dataclass
class Picture:
    data: bytes
    ext: str
    width: int
    height: int

    def stream(self):
        return BytesIO(self.data)

    def fit(self, max_width, max_height, unit=72, dpi=150):
        """Natural size at `dpi` in `unit`s per inch, scaled down to fit the box."""
        width, height = self.width * unit / dpi, self.height * unit / dpi
        scale = min(1.0, max_width / width, max_height / height)
        return width * scale, height * scale

    def fill(self, box_width, box_height):
        """Largest size with this aspect ratio inside the box."""
        scale = min(box_width / self.width, box_height / self.height)
        return self.width * scale, self.height * scale


class _Scope:
    def __init__(self, token):
        self.token = token
        self.count = 0
        self.total = 0
        self.user_id = None


@contextmanager
def request_scope(token):
    """One tool call: the forwarded token and the image budget it shares."""
    marker = _scope.set(_Scope(token))
    try:
        yield
    finally:
        _scope.reset(marker)


# --- sources (no I/O) ----------------------------------------------------------

def classify(src):
    """Return (kind, value) for an image link, or refuse it."""
    if not isinstance(src, str) or not src.strip():
        raise FilePolicyError("An image needs a source; " + SOURCE_HINT)
    value = src.strip()
    lower = value.lower()
    if lower.startswith("image_query:"):
        query = unquote(value[len("image_query:"):]).strip()
        if not query:
            raise FilePolicyError("An image search needs a query")
        return "query", query
    if lower.startswith("data:"):
        return "data", value
    if len(value) > MAX_URL_LENGTH:
        raise FilePolicyError("Image links may be at most 2048 characters long")
    if any(ord(c) < 33 for c in value):
        raise FilePolicyError("Image links must not contain spaces or control characters")
    match = _OWUI_FILE.fullmatch(value)
    if match:
        return "file", match.group(1)
    if lower.startswith("https://"):
        return "url", value
    if value.startswith("//"):
        raise FilePolicyError("Image URLs must start with https://")
    if re.match(r"[A-Za-z]:[\\/]", value) or not _SCHEME.match(value) or lower.startswith("file:"):
        raise FilePolicyError("Local file paths cannot be used as images; " + SOURCE_HINT)
    raise FilePolicyError("This kind of image link is not supported; " + SOURCE_HINT)


def source_of(item):
    """Source of an image block: exactly one of file_id, url or query."""
    given = [(k, item.get(k)) for k in ("file_id", "url", "query") if item.get(k) is not None]
    if len(given) != 1 or not isinstance(given[0][1], str) or not given[0][1].strip():
        raise FilePolicyError("An image needs exactly one of file_id, url or query")
    key, value = given[0][0], given[0][1].strip()
    if key == "file_id":
        return "file", checked_file_id(value)
    if key == "query":
        return "query", value
    kind, value = classify(value)
    if kind == "query":
        raise FilePolicyError("Put an image search in query, not in url")
    return kind, value


def slide_source(slide):
    """Source of a PPTX slide image, or None: one of image_file_id, image_url, image_query."""
    keys = {"image_file_id": "file_id", "image_url": "url", "image_query": "query"}
    given = {name: slide[key] for key, name in keys.items() if slide.get(key) is not None}
    if not given:
        return None
    if len(given) > 1:
        raise FilePolicyError("A slide takes one of image_file_id, image_url or image_query")
    return source_of(given)


def link(kind, value):
    """The link form of a source, as PDF text and DOCX lines write it."""
    if kind == "file":
        return f"/api/v1/files/{value}/content"
    if kind == "query":
        return "image_query:" + quote(value, safe="")
    return value


def describe(kind, value):
    if kind == "file":
        return f"image file {value}"
    if kind == "query":
        return f"image search '{value[:80]}'"
    if kind == "data":
        return "inline image"
    return f"image from {urlsplit(value).hostname or '?'}"


def image_line(text):
    """(alt, link) when a text line is one markdown image and nothing else."""
    match = _IMAGE_LINE.fullmatch(text) if isinstance(text, str) else None
    return (match.group(1), match.group(2)) if match else None


def has_inline_image(text):
    return isinstance(text, str) and bool(_INLINE_IMAGE.search(text))


def validate_request(item):
    """Refuse unusable image sources of one file before anything is generated.

    PDF text is not inspected here: markdown decides there what is an image
    (an example inside a code block is not), and every image goes through
    load() anyway, which applies the same rules.
    """
    fmt = (item.get("format") or "").lower()
    found = []
    if fmt in ("pdf", "docx"):
        content = item.get("content")
        for block in content if isinstance(content, list) else [content]:
            if isinstance(block, dict) and block.get("type") in ("image", "image_query"):
                found.append(source_of(block if block.get("type") == "image" else {"query": block.get("query")}))
            elif fmt == "docx":
                # A markdown text is read line by line; a text item is one paragraph.
                texts = block.split("\n") if isinstance(content, str) else (
                    [block] if isinstance(block, str) else _texts(block))
                for text in texts:
                    image = image_line(text) if isinstance(block, str) else None
                    if image:
                        found.append(classify(image[1]))
                    elif has_inline_image(text):
                        raise FilePolicyError("In DOCX an image must be its own image block or line, not part of a text")
    elif fmt == "pptx":
        for slide in item.get("slides_data") or []:
            source = slide_source(slide)
            if source:
                found.append(source)
            if any(has_inline_image(t) for t in _texts(slide.get("title")) + _texts(slide.get("content"))):
                raise FilePolicyError("Slide images go in image_file_id, image_url or image_query, not in the slide text")
    if len(found) > MAX_COUNT:
        raise FilePolicyError(f"At most {MAX_COUNT} images per request are supported")
    return found


def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


# --- loading -------------------------------------------------------------------

def load(kind, value):
    """Fetch, check and re-encode one image; FilePolicyError when it cannot be used."""
    scope = _scope.get()
    if scope is None:
        raise RuntimeError("image loaded outside a tool call")
    label = describe(kind, value)
    scope.count += 1
    if scope.count > MAX_COUNT:
        raise FilePolicyError(f"At most {MAX_COUNT} images per request are supported")
    if kind == "file":
        raw = _owui_file(value, scope, label)
    elif kind == "query":
        raw = _search(value, label)
    elif kind == "url":
        raw = _fetch(value, None, label)
    elif kind == "data":
        raw = _data_url(value, label)
    else:
        raise FilePolicyError("This kind of image link is not supported; " + SOURCE_HINT)
    scope.total += len(raw)
    if scope.total > MAX_TOTAL_BYTES:
        raise FilePolicyError(f"The images of one request may total at most {MAX_TOTAL_BYTES // 2**20} MB")
    return _normalize(raw, label)


def _too_large(label):
    return FilePolicyError(f"The {label} is larger than {MAX_BYTES // 2**20} MB")


def _data_url(value, label):
    match = _DATA_URL.fullmatch(value.strip())
    if not match:
        raise FilePolicyError(f"The {label} must be a base64 data: URL of a {FORMAT_NAMES} image")
    payload = re.sub(r"\s+", "", match.group(2))
    if len(payload) * 3 // 4 > MAX_BYTES:
        raise _too_large(label)
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError:
        raise FilePolicyError(f"The {label} is not valid base64") from None


def _read_capped(chunks, label, deadline=None):
    body = bytearray()
    for chunk in chunks:
        body += chunk
        if len(body) > MAX_BYTES:
            raise _too_large(label)
        if deadline is not None and time.monotonic() > deadline:
            raise _Unavailable("deadline")
    return bytes(body)


def _owui_file(fid, scope, label):
    base = (os.getenv("OWUI_URL") or "").rstrip("/")
    fid = checked_file_id(fid)
    headers = {"Authorization": scope.token, "Accept": "application/json"}
    missing = FilePolicyError(f"The {label} was not found among your files")
    try:
        if scope.user_id is None:
            me = http_get(f"{base}/api/v1/auths/", headers=headers)
            user_id = me.json().get("id") if me.status_code == 200 else None
            if not isinstance(user_id, str) or not user_id:
                raise FilePolicyError("Your Open WebUI session could not be confirmed")
            scope.user_id = user_id
        info = http_get(f"{base}/api/v1/files/{fid}", headers=headers)
        meta = info.json() if info.status_code == 200 else {}
        if meta.get("user_id") != scope.user_id:
            raise missing
        content_type = str((meta.get("meta") or {}).get("content_type") or "")
        if content_type and not content_type.lower().startswith("image/"):
            raise FilePolicyError(f"The {label} is not an image")
        response = http_get(f"{base}/api/v1/files/{fid}/content",
                            headers={"Authorization": scope.token}, stream=True)
        try:
            if response.status_code != 200:
                raise missing
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise _too_large(label)
            return _read_capped(response.iter_content(65536), label)
        finally:
            response.close()
    except FilePolicyError:
        raise
    except Exception:
        log.warning("Open WebUI image file could not be read", exc_info=True)
        raise FilePolicyError(f"The {label} could not be read from Open WebUI") from None


def _search(query, label):
    source = os.getenv("IMAGE_SOURCE", "unsplash").strip().lower()
    if source == "unsplash":
        key = os.getenv("UNSPLASH_ACCESS_KEY")
        api, auth = "https://api.unsplash.com/search/photos", f"Client-ID {key}"
        pick = lambda data: [r["urls"]["regular"] for r in data.get("results") or []]
    elif source == "pexels":
        key = os.getenv("PEXELS_ACCESS_KEY")
        api, auth = "https://api.pexels.com/v1/search", f"{key}"
        pick = lambda data: [p["src"]["large"] for p in data.get("photos") or []]
    else:
        raise FilePolicyError("This image search is not available in secure exports; "
                              "use the user's own images or public https URLs")
    if not key:
        raise FilePolicyError("Image search is not configured on this server; "
                              "use the user's own images or public https URLs")
    try:
        response = http_get(api, params={"query": query, "per_page": 1, "orientation": "landscape"},
                            headers={"Authorization": auth})
        if response.status_code != 200:
            raise _Unavailable(response.status_code)
        urls = pick(response.json())
    except Exception:
        log.warning("Image search failed (%s)", source, exc_info=True)
        raise FilePolicyError("Image search is unavailable at the moment") from None
    if not urls:
        raise FilePolicyError(f"The {label} found no image")
    if not isinstance(urls[0], str):
        raise FilePolicyError(f"The {label} returned an unusable link")
    return _fetch(urls[0], PROVIDER_HOSTS[source], label)


# --- public https --------------------------------------------------------------

def _is_public_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _is_public_ip(ip.sixtofour)
        if ip.teredo is not None or ip in _SITE_LOCAL:
            return False
        if any(ip in net for net in _NAT64):
            return _is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return ip.is_global and not (ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local
                                 or ip.is_reserved or ip.is_unspecified)


def _resolve(host, port):
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _public_addresses(host):
    try:
        found = _resolve(host, _HTTPS_PORT)
    except OSError:
        raise _Unavailable("name not resolved") from None
    addresses = []
    for value in found:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
        if not _is_public_ip(ip):
            raise _Unavailable("address not public")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise _Unavailable("name not resolved")
    return addresses


def _checked_url(url, allowed_hosts):
    """(host, is_ip_literal, request target) of an allowed https URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _Refused("the link is not a valid URL") from None
    if parts.scheme.lower() != "https":
        raise _Refused("only https links are allowed")
    if parts.username is not None or parts.password is not None:
        raise _Refused("links must not contain user names or passwords")
    if port not in (None, 443, _HTTPS_PORT):
        raise _Refused("links must use the standard https port")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise _Refused("the link has no host")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _Refused("the link has an invalid host name") from None
    if allowed_hosts is not None and host not in allowed_hosts:
        raise _Unavailable("host outside the provider")
    target = quote(parts.path or "/", safe=_SAFE_PATH)
    if parts.query:
        target += "?" + quote(parts.query, safe=_SAFE_PATH + "?")
    return host, literal, target


def _fetch(url, allowed_hosts, label):
    """GET a public https image, following at most three redirects, each checked again."""
    deadline = time.monotonic() + TIMEOUT
    try:
        for _ in range(MAX_REDIRECTS + 1):
            host, literal, target = _checked_url(url, allowed_hosts)
            status, location, body = _get_pinned(_public_addresses(host), host, literal, target, deadline, label)
            if status in _REDIRECTS:
                if not location:
                    raise _Unavailable("redirect without location")
                url = urljoin(url, location)
                continue
            if status != 200:
                raise _Unavailable(f"HTTP {status}")
            return body
        raise _Unavailable("too many redirects")
    except _Refused as exc:
        raise FilePolicyError(f"The {label} could not be loaded: {exc}") from None
    except _Unavailable as exc:
        log.info("Image not loaded from %s: %s", urlsplit(url).hostname, exc)
        raise FilePolicyError(f"The {label} could not be loaded; use another image") from None


class _PinnedConnection(HTTPSConnection):
    """Keeps the TCP socket's descriptor so the watchdog can cut even a TLS handshake."""
    fd = None

    def _new_conn(self):
        sock = super()._new_conn()
        self.fd = sock.fileno()
        return sock


class _Watchdog:
    """Shuts the connection down at the deadline: per-read timeouts alone would let a
    server that sends one byte at a time hold a call open for as long as it likes."""

    def __init__(self, conn, seconds):
        self.conn, self.lock, self.stopped, self.fired = conn, threading.Lock(), False, False
        self.timer = threading.Timer(seconds, self.cut)
        self.timer.daemon = True
        self.timer.start()

    def cut(self):
        with self.lock:
            if self.stopped or self.conn.fd is None:
                return
            self.fired = True
            sock = socket.socket(fileno=self.conn.fd)
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                sock.detach()

    def stop(self):
        # Before the connection is closed, so the descriptor cannot have been reused.
        with self.lock:
            self.stopped = True
        self.timer.cancel()


def _get_pinned(addresses, host, literal, target, deadline, label):
    """One GET to the checked addresses only; TLS is verified for the host name."""
    host_header = (f"[{host}]" if ":" in host else host) + ("" if _HTTPS_PORT == 443 else f":{_HTTPS_PORT}")
    headers = {"Host": host_header, "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
               "Accept": "image/png,image/jpeg,image/gif,image/webp,image/bmp;q=0.9,image/*;q=0.5"}
    failure = "no address"
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("deadline")
        conn = _PinnedConnection(address, port=_HTTPS_PORT, timeout=min(10.0, remaining),
                                 cert_reqs="CERT_REQUIRED", ca_certs=_CA_BUNDLE,
                                 assert_hostname=host, server_hostname=None if literal else host)
        watchdog = _Watchdog(conn, remaining)
        try:
            try:
                conn.request("GET", target, headers=headers, preload_content=False, decode_content=False)
                response = conn.getresponse()
            except (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError, ValueError) as exc:
                # ValueError: urllib3 reports a certificate for another name as CertificateError.
                failure = type(exc).__name__
                continue
            if watchdog.fired:
                raise _Unavailable("deadline")
            if response.status != 200:
                return response.status, response.headers.get("Location"), b""
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise _Unavailable("compressed body")
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith(("text/", "application/json", "application/xml", "image/svg")):
                raise FilePolicyError(f"The {label} is not a supported image ({FORMAT_NAMES})")
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise _too_large(label)
            try:
                body = _read_capped(iter(lambda: response.read1(65536), b""), label, deadline)
            except (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError, ValueError):
                raise _Unavailable("read failed") from None
            if watchdog.fired:
                # A cut connection can look like a short, complete body.
                raise _Unavailable("deadline")
            return 200, None, body
        finally:
            watchdog.stop()
            conn.close()
    raise _Unavailable(failure)


# --- decoding ------------------------------------------------------------------

def _normalize(raw, label):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=FORMATS) as probe:
                width, height = probe.size
                source_format = probe.format
                if width < 1 or height < 1 or width * height > MAX_PIXELS:
                    raise FilePolicyError(f"The {label} has too many pixels")
                probe.verify()
            with Image.open(BytesIO(raw), formats=FORMATS) as opened:
                opened.seek(0)
                opened.load()
                image = ImageOps.exif_transpose(opened)
                alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
                photo = source_format in ("JPEG", "WEBP") and not alpha
                image = image.convert("RGBA" if alpha else "RGB")
                if max(image.size) > MAX_SIDE:
                    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                out = BytesIO()
                if photo:
                    image.save(out, "JPEG", quality=90, optimize=True)
                else:
                    image.save(out, "PNG", optimize=True)
                return Picture(out.getvalue(), "jpeg" if photo else "png", *image.size)
    except FilePolicyError:
        raise
    except Exception:
        raise FilePolicyError(f"The {label} is not a supported image ({FORMAT_NAMES})") from None
