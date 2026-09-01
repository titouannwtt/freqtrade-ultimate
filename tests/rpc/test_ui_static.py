"""Fork-specific: compression + HTTP caching of the FreqUI static bundle."""

import gzip
from pathlib import Path

import pytest
from starlette.responses import FileResponse

from freqtrade.rpc.api_server import ui_static
from freqtrade.rpc.api_server.ui_static import (
    CACHE_IMMUTABLE,
    CACHE_NO_CACHE,
    CACHE_SHORT,
    accepted_encodings,
    cache_control_for,
    clear_cache,
    etag_matches,
    static_response,
)


JS_BODY = ("const x = 'freqtrade';\n" * 400).encode()  # ~9 kB, very compressible


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def uidir(tmp_path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_bytes(JS_BODY)
    (tmp_path / "index.html").write_text("<html>hi</html>" * 200)
    (tmp_path / "favicon.ico").write_bytes(b"\x00" * 64)
    return tmp_path


def _body(resp) -> bytes:
    return resp.body


class TestAcceptEncoding:
    @pytest.mark.parametrize(
        "header,expected",
        [
            (None, set()),
            ("", set()),
            ("gzip, deflate, br", {"gzip", "deflate", "br"}),
            ("GZIP", {"gzip"}),
            ("gzip;q=0", set()),
            ("gzip;q=0.5, br;q=0", {"gzip"}),
            ("gzip;q=bogus", {"gzip"}),
        ],
    )
    def test_parsing(self, header, expected):
        assert accepted_encodings(header) == expected


class TestCachePolicy:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("assets/index-abc123.js", CACHE_IMMUTABLE),
            ("/assets/style-x.css", CACHE_IMMUTABLE),
            ("index.html", CACHE_NO_CACHE),
            ("", CACHE_NO_CACHE),
            ("sub/page.html", CACHE_NO_CACHE),
            ("favicon.ico", CACHE_SHORT),
        ],
    )
    def test_policy(self, path, expected):
        assert cache_control_for(path) == expected

    def test_html_never_immutable(self):
        # A hashed-looking html under assets/ must STILL revalidate, otherwise a
        # deployment would never be picked up.
        assert cache_control_for("assets/index.html") == CACHE_NO_CACHE


class TestEtagMatching:
    @pytest.mark.parametrize(
        "header,tag,expected",
        [
            (None, '"a"', False),
            ("", '"a"', False),
            ('"a"', '"a"', True),
            ('W/"a"', '"a"', True),
            ('"b", "a"', '"a"', True),
            ('"b"', '"a"', False),
            ("*", '"a"', True),
        ],
    )
    def test_matching(self, header, tag, expected):
        assert etag_matches(header, tag) is expected


class TestStaticResponse:
    def test_gzip_applied_and_smaller(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        resp = static_response(f, "assets/index-abc123.js", accept_encoding="gzip, br")
        assert resp.headers["content-encoding"] == "gzip"
        assert resp.headers["cache-control"] == CACHE_IMMUTABLE
        assert resp.headers["vary"] == "Accept-Encoding"
        assert resp.media_type == "application/javascript"
        assert len(_body(resp)) < len(JS_BODY) / 3
        assert gzip.decompress(_body(resp)) == JS_BODY

    def test_no_gzip_when_not_accepted(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        resp = static_response(f, "assets/index-abc123.js", accept_encoding="identity")
        assert "content-encoding" not in resp.headers
        assert isinstance(resp, FileResponse)
        assert "etag" in resp.headers

    def test_small_file_not_compressed(self, uidir):
        resp = static_response(uidir / "favicon.ico", "favicon.ico", accept_encoding="gzip")
        assert "content-encoding" not in resp.headers
        assert resp.headers["cache-control"] == CACHE_SHORT

    def test_incompressible_suffix_not_compressed(self, uidir):
        big = uidir / "assets" / "pic-abc.png"
        big.write_bytes(b"\x00" * 4096)
        resp = static_response(big, "assets/pic-abc.png", accept_encoding="gzip")
        assert "content-encoding" not in resp.headers

    def test_etag_stable_then_304(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        first = static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        etag = first.headers["etag"]
        second = static_response(
            f, "assets/index-abc123.js", accept_encoding="gzip", if_none_match=etag
        )
        assert second.status_code == 304
        assert _body(second) == b""
        assert second.headers["etag"] == etag
        assert second.headers["cache-control"] == CACHE_IMMUTABLE

    def test_etag_differs_between_encodings(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        gz = static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        raw = static_response(f, "assets/index-abc123.js", accept_encoding="identity")
        assert gz.headers["etag"] != raw.headers["etag"]
        # A gzip ETag must NOT satisfy an identity request (different payload).
        again = static_response(
            f,
            "assets/index-abc123.js",
            accept_encoding="identity",
            if_none_match=gz.headers["etag"],
        )
        assert again.status_code == 200

    def test_etag_changes_when_file_changes(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        first = static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        f.write_bytes(JS_BODY + b"// more\n" * 50)
        second = static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        assert first.headers["etag"] != second.headers["etag"]
        assert gzip.decompress(_body(second)) == f.read_bytes()

    def test_compression_is_memoised(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        assert ui_static.cache_stats()["entries"] == 0
        static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        stats = ui_static.cache_stats()
        assert stats["entries"] == 1
        assert stats["bytes"] > 0
        calls = []
        real = gzip.compress

        def spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        ui_static.gzip.compress = spy
        try:
            static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        finally:
            ui_static.gzip.compress = real
        assert calls == []

    def test_cache_budget_evicts(self, uidir, monkeypatch):
        first = uidir / "assets" / "chunk-0.js"
        first.write_bytes(JS_BODY)
        static_response(first, "assets/chunk-0.js", accept_encoding="gzip")
        one_blob = ui_static.cache_stats()["bytes"]
        # Budget for ~2 blobs: the 6th insertion must have evicted the oldest.
        monkeypatch.setattr(ui_static, "CACHE_BUDGET_BYTES", one_blob * 2 + 1)
        for i in range(1, 6):
            f = uidir / "assets" / f"chunk-{i}.js"
            f.write_bytes(JS_BODY + str(i).encode())
            static_response(f, f"assets/chunk-{i}.js", accept_encoding="gzip")
        assert ui_static.cache_stats()["bytes"] <= one_blob * 2 + 1
        assert ui_static.cache_stats()["entries"] < 6

    def test_precompressed_gz_sidecar_served(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        sidecar = uidir / "assets" / "index-abc123.js.gz"
        sidecar.write_bytes(gzip.compress(JS_BODY, 9))
        resp = static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        assert resp.headers["content-encoding"] == "gzip"
        assert isinstance(resp, FileResponse)  # streamed, not compressed in-process
        assert resp.path == str(sidecar)
        # Sidecar path must not have populated the in-memory compression cache.
        assert ui_static.cache_stats()["entries"] == 0

    def test_br_sidecar_preferred_when_accepted(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        (uidir / "assets" / "index-abc123.js.gz").write_bytes(gzip.compress(JS_BODY))
        br = uidir / "assets" / "index-abc123.js.br"
        br.write_bytes(b"fake-brotli-payload")
        resp = static_response(f, "assets/index-abc123.js", accept_encoding="gzip, br")
        assert resp.headers["content-encoding"] == "br"
        assert resp.path == str(br)

    def test_br_sidecar_ignored_when_not_accepted(self, uidir):
        f = uidir / "assets" / "index-abc123.js"
        (uidir / "assets" / "index-abc123.js.br").write_bytes(b"fake")
        resp = static_response(f, "assets/index-abc123.js", accept_encoding="gzip")
        assert resp.headers["content-encoding"] == "gzip"
        assert gzip.decompress(_body(resp)) == JS_BODY

    def test_index_html_no_cache(self, uidir):
        resp = static_response(uidir / "index.html", "index.html", accept_encoding="gzip")
        assert resp.headers["cache-control"] == CACHE_NO_CACHE
        assert resp.headers["content-encoding"] == "gzip"

    def test_missing_file_raises(self, uidir):
        with pytest.raises(FileNotFoundError):
            static_response(uidir / "nope.js", "nope.js")
