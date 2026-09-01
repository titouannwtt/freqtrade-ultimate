"""Static-file serving for the built FreqUI bundle (fork-specific).

The plain ``FileResponse`` catch-all in :mod:`web_ui` sent every asset
uncompressed and uncacheable, so a 3.6 MB JS bundle was re-downloaded in full on
every visit. On a multi-bot dashboard the browser's 6-connections-per-host queue
is the real bottleneck, so weight and cache hits matter far more than server CPU.

Three additions, all confined to static files (the API and the WebSocket route
are deliberately untouched — no global compression middleware):

* **gzip**, served from a precompressed ``.gz`` / ``.br`` sidecar when Vite
  produced one, otherwise compressed once and memoised in a bounded LRU cache
  (compressing a 7 MB worker on every request would be worse than the download).
* **Cache-Control**: hashed files under ``assets/`` are immutable for a year,
  ``index.html`` is ``no-cache`` so a deployment is seen immediately.
* **ETag / If-None-Match**, so an unchanged file costs an empty 304.

Everything degrades to the previous behaviour when a file is small,
incompressible, or the cache budget is exhausted.
"""

import gzip
import logging
import mimetypes
import threading
from collections import OrderedDict
from pathlib import Path

from starlette.responses import FileResponse, Response


logger = logging.getLogger(__name__)

# Only text-ish payloads benefit; png/jpg/woff2 are already compressed.
COMPRESSIBLE_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".css",
        ".html",
        ".htm",
        ".json",
        ".map",
        ".svg",
        ".txt",
        ".xml",
        ".wasm",
        ".webmanifest",
        ".ico",
    }
)

# Below this, the gzip header costs more than it saves.
MIN_COMPRESS_SIZE = 1024
# Above this we stream the raw file: holding it twice in RAM is not worth it.
MAX_COMPRESS_SIZE = 16 * 1024 * 1024
# Total compressed bytes kept in memory across all files (LRU eviction).
CACHE_BUDGET_BYTES = 64 * 1024 * 1024

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_NO_CACHE = "no-cache"
CACHE_SHORT = "public, max-age=3600"

_LOCK = threading.Lock()
_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_CACHE_BYTES = 0


def cache_stats() -> dict:
    with _LOCK:
        return {"entries": len(_CACHE), "bytes": _CACHE_BYTES}


def clear_cache() -> None:
    global _CACHE_BYTES
    with _LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0


def _cache_get(key: tuple) -> bytes | None:
    with _LOCK:
        blob = _CACHE.get(key)
        if blob is not None:
            _CACHE.move_to_end(key)
        return blob


def _cache_put(key: tuple, blob: bytes) -> None:
    global _CACHE_BYTES
    if len(blob) > CACHE_BUDGET_BYTES:
        return
    with _LOCK:
        if key in _CACHE:
            return
        _CACHE[key] = blob
        _CACHE_BYTES += len(blob)
        while _CACHE_BYTES > CACHE_BUDGET_BYTES and len(_CACHE) > 1:
            _, evicted = _CACHE.popitem(last=False)
            _CACHE_BYTES -= len(evicted)


def accepted_encodings(accept_encoding: str | None) -> set[str]:
    """Encodings the client accepts, ``q=0`` honoured."""
    out: set[str] = set()
    for part in (accept_encoding or "").split(","):
        token = part.strip()
        if not token:
            continue
        name, _, params = token.partition(";")
        name = name.strip().lower()
        if not name:
            continue
        qvalue = 1.0
        for param in params.split(";"):
            k, _, v = param.partition("=")
            if k.strip().lower() == "q":
                try:
                    qvalue = float(v.strip())
                except ValueError:
                    qvalue = 1.0
        if qvalue > 0:
            out.add(name)
    return out


def cache_control_for(rel_path: str) -> str:
    """Cache policy from the path alone.

    ``assets/`` names carry a content hash from Vite, so they can never change
    meaning: immutable. ``index.html`` is the entry point and MUST be revalidated,
    otherwise a deployment would never be picked up.
    """
    normalised = rel_path.replace("\\", "/").lstrip("/")
    if normalised.endswith(".html") or normalised in ("", "index.html"):
        return CACHE_NO_CACHE
    if normalised.startswith("assets/"):
        return CACHE_IMMUTABLE
    return CACHE_SHORT


def _etag(stat_result, encoding: str) -> str:
    suffix = f"-{encoding}" if encoding else ""
    return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}{suffix}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        tag = candidate.strip()
        if tag == "*":
            return True
        if tag.startswith(("W/", "w/")):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def _media_type(filename: Path) -> str | None:
    if filename.suffix in (".js", ".mjs", ".cjs"):
        # Force text/javascript - circumvent faulty system configuration.
        return "application/javascript"
    guessed, _ = mimetypes.guess_type(filename.name)
    return guessed


def _sidecar(filename: Path, accepted: set[str]) -> tuple[Path, str] | None:
    """A precompressed sibling the client can take, if Vite emitted one."""
    for encoding, ext in (("br", ".br"), ("gzip", ".gz")):
        if encoding not in accepted:
            continue
        candidate = filename.with_name(filename.name + ext)
        try:
            if candidate.is_file():
                return candidate, encoding
        except OSError:
            continue
    return None


def _gzip_blob(filename: Path, stat_result) -> bytes | None:
    key = (str(filename), stat_result.st_mtime_ns, stat_result.st_size)
    blob = _cache_get(key)
    if blob is not None:
        return blob
    try:
        raw = filename.read_bytes()
    except OSError as exc:
        logger.warning("ui_static: cannot read %s: %s", filename.name, exc)
        return None
    blob = gzip.compress(raw, 6)
    if len(blob) >= len(raw):
        # Incompressible payload: better to serve it raw.
        return None
    _cache_put(key, blob)
    return blob


def static_response(
    filename: Path,
    rel_path: str,
    *,
    accept_encoding: str | None = None,
    if_none_match: str | None = None,
) -> Response:
    """Serve one existing static file, compressed and cacheable.

    ``filename`` must already have been validated (existing file, inside the UI
    root). ``rel_path`` only drives the cache policy.
    """
    cache_control = cache_control_for(rel_path)
    media_type = _media_type(filename)
    accepted = accepted_encodings(accept_encoding)

    sidecar = _sidecar(filename, accepted)
    if sidecar is not None:
        path, encoding = sidecar
        try:
            stat_result = path.stat()
        except OSError:
            sidecar = None
        else:
            etag = _etag(stat_result, encoding)
            headers = {
                "Cache-Control": cache_control,
                "Content-Encoding": encoding,
                "Vary": "Accept-Encoding",
                "ETag": etag,
            }
            if etag_matches(if_none_match, etag):
                return Response(status_code=304, headers=headers)
            return FileResponse(
                str(path), media_type=media_type, headers=headers, stat_result=stat_result
            )

    try:
        stat_result = filename.stat()
    except OSError:
        raise FileNotFoundError(filename) from None

    compressible = (
        "gzip" in accepted
        and filename.suffix in COMPRESSIBLE_SUFFIXES
        and MIN_COMPRESS_SIZE <= stat_result.st_size <= MAX_COMPRESS_SIZE
    )
    if compressible:
        blob = _gzip_blob(filename, stat_result)
        if blob is not None:
            etag = _etag(stat_result, "gzip")
            headers = {
                "Cache-Control": cache_control,
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
                "ETag": etag,
            }
            if etag_matches(if_none_match, etag):
                return Response(status_code=304, headers=headers)
            return Response(
                content=blob,
                media_type=media_type,
                headers=headers,
            )

    etag = _etag(stat_result, "")
    headers = {"Cache-Control": cache_control, "Vary": "Accept-Encoding", "ETag": etag}
    if etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return FileResponse(
        str(filename), media_type=media_type, headers=headers, stat_result=stat_result
    )
