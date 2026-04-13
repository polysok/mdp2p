import asyncio
import time

import pytest

from protocol import send_msg, recv_msg, RateLimiter, validate_uri


class TestValidateUri:
    def test_accepts_simple(self):
        assert validate_uri("demo") == "demo"

    def test_accepts_dotted(self):
        assert validate_uri("blog.alice") == "blog.alice"

    def test_accepts_hyphens_underscores(self):
        assert validate_uri("my-site_v2") == "my-site_v2"

    def test_accepts_max_length(self):
        uri = "a" * 255
        assert validate_uri(uri) == uri

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_uri("")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_uri(None)

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("../../etc")

    def test_rejects_dot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_uri(".")

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_uri("..")

    def test_rejects_embedded_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_uri("foo..bar")

    def test_rejects_slash(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("foo/bar")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("foo\\bar")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("foo\x00bar")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            validate_uri("a" * 256)

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_uri("my site")

    def test_rejects_starting_with_dot(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_uri(".hidden")

    def test_rejects_starting_with_hyphen(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_uri("-flag")


class TestRateLimiter:
    def test_allows_under_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60.0)
        for _ in range(5):
            assert limiter.is_allowed("client1")

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60.0)
        for _ in range(3):
            assert limiter.is_allowed("client1")
        assert not limiter.is_allowed("client1")

    def test_separate_clients(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        assert limiter.is_allowed("a")
        assert limiter.is_allowed("a")
        assert not limiter.is_allowed("a")
        assert limiter.is_allowed("b")

    def test_window_expiry(self):
        limiter = RateLimiter(max_requests=2, window_seconds=0.01)
        assert limiter.is_allowed("c")
        assert limiter.is_allowed("c")
        assert not limiter.is_allowed("c")
        time.sleep(0.02)
        assert limiter.is_allowed("c")


@pytest.mark.asyncio
class TestProtocolSendRecv:
    async def test_roundtrip(self):
        server_data = []

        async def handler(reader, writer):
            msg = await recv_msg(reader)
            server_data.append(msg)
            await send_msg(writer, {"echo": msg})

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_msg(writer, {"hello": "world"})
        response = await recv_msg(reader)
        assert response == {"echo": {"hello": "world"}}

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    async def test_recv_timeout(self):
        async def slow_handler(reader, writer):
            await asyncio.sleep(10)
            await send_msg(writer, {"late": True})

        server = await asyncio.start_server(slow_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            result = await recv_msg(reader, timeout=0.2)
            assert result is None
        finally:
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()

    async def test_connection_reset(self):
        async def close_immediately(reader, writer):
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(close_immediately, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        result = await recv_msg(reader)
        assert result is None

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
