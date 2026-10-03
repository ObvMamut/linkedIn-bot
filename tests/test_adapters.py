import asyncio
import json
import socket

import httpx
import pytest

from linkedin_bot.adapters import public_http
from linkedin_bot.adapters.ats import ATSSource, Board
from linkedin_bot.adapters.ollama import OllamaProbe
from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP, resolve_public
from linkedin_bot.runtime.config import InferenceConfig


@pytest.mark.parametrize(
    "provider,payload",
    [
        (
            "greenhouse",
            {
                "jobs": [
                    {
                        "id": 123,
                        "title": "Intern",
                        "absolute_url": "https://example.org/1",
                        "location": {"name": "Warsaw"},
                        "content": "Python",
                    }
                ]
            },
        ),
        (
            "lever",
            [
                {
                    "id": "uuid-1",
                    "text": "Intern",
                    "hostedUrl": "https://example.org/1",
                    "categories": {"location": "Warsaw"},
                    "descriptionPlain": "Python",
                }
            ],
        ),
        (
            "ashby",
            {
                "jobs": [
                    {
                        "id": "uuid-1",
                        "title": "Intern",
                        "jobUrl": "https://example.org/1",
                        "location": "Warsaw",
                        "descriptionPlain": "Python",
                    }
                ]
            },
        ),
    ],
)
def test_ats_observations(provider, payload):
    http = PublicHTTP()
    try:
        source = ATSSource(http, Board(provider=provider, slug="synthetic"))
        page = source.normalize(json.dumps(payload).encode(), 20)
        assert page.coverage == "complete"
        assert page.items[0].title == "Intern" and page.items[0].location == "Warsaw"
        assert page.items[0].description == "Python"
        assert (
            source.normalize(json.dumps(payload).encode(), 20).items[0].content_hash
            == page.items[0].content_hash
        )
        with pytest.raises(AdapterError, match="invalid_response"):
            source.normalize(b'{"unexpected": "layout change"}', 20)
    finally:
        asyncio.run(http.close())


def test_pagination_and_board_path_injection():
    http = PublicHTTP()
    try:
        source = ATSSource(http, Board(provider="lever", slug="synthetic"))
        body = json.dumps(
            [{"id": "1", "text": "Intern", "hostedUrl": "https://example.org", "categories": {}}]
        ).encode()
        page = source.normalize(body, 1, "10")
        assert page.coverage == "partial" and page.next_cursor == "11"
        for slug in ("../secrets", "x?redirect=http://127.0.0.1", "https://example.org", ""):
            with pytest.raises(AdapterError, match="invalid_board"):
                Board(provider="greenhouse", slug=slug).endpoint(20, None)
        with pytest.raises(AdapterError, match="invalid_cursor"):
            source.normalize(body, 1, "-1")
        with pytest.raises(AdapterError, match="invalid_limit"):
            source.normalize(body, 0)
    finally:
        asyncio.run(http.close())


def test_public_get_pins_dns_preserves_tls_and_excludes_cookies(monkeypatch):
    async def scenario():
        calls = []

        async def resolve(host, port):
            assert host == "example.org" and port == 443
            return "93.184.216.34"

        monkeypatch.setattr(public_http, "resolve_public", resolve)

        def transport(request):
            calls.append(request)
            return httpx.Response(
                200, content=b"public body", headers={"set-cookie": "session=synthetic"}
            )

        http = PublicHTTP()
        await http.client.aclose()
        http.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        try:
            assert await http.get("https://example.org/jobs") == b"public body"
            assert await http.get("https://example.org/jobs") == b"public body"
            assert all(request.url.host == "93.184.216.34" for request in calls)
            assert all(request.headers["host"] == "example.org" for request in calls)
            assert all(request.extensions["sni_hostname"] == "example.org" for request in calls)
            assert all("cookie" not in request.headers for request in calls)
        finally:
            await http.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_private_destinations_rejected(monkeypatch, address):
    async def scenario():
        async def addresses(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
        with pytest.raises(AdapterError, match="private_destination"):
            await resolve_public("synthetic.test", 443)

    asyncio.run(scenario())


def test_redirect_revalidation_size_and_status(monkeypatch):
    async def scenario():
        async def resolve(host, port):
            if host == "private.test":
                raise AdapterError("private_destination")
            return "93.184.216.34"

        monkeypatch.setattr(public_http, "resolve_public", resolve)

        def transport(request):
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"location": "http://private.test/secret"})
            if request.url.path == "/rate":
                return httpx.Response(429)
            if request.url.path == "/missing":
                return httpx.Response(404)
            return httpx.Response(200, content=b"0123456789")

        http = PublicHTTP(max_bytes=5)
        await http.client.aclose()
        http.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        try:
            for path, error in (
                ("redirect", "private_destination"),
                ("rate", "rate_limited"),
                ("missing", "http_status"),
                ("big", "response_too_large"),
            ):
                with pytest.raises(AdapterError, match=error):
                    await http.get(f"https://example.org/{path}")
        finally:
            await http.close()

    asyncio.run(scenario())


def test_ollama_no_download_and_semantic_probe(monkeypatch):
    async def scenario():
        calls = []

        def transport(request):
            calls.append(request)
            if request.url.path == "/api/tags":
                return httpx.Response(
                    200, json={"models": [{"name": "synthetic:1", "digest": "abc", "size": 100}]}
                )
            body = json.loads(request.content)
            assert body["keep_alive"] == 0 and body["options"]["num_ctx"] == 4096
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "response": json.dumps(
                        {
                            "skills": ["Python"],
                            "fact_ids": ["f1"],
                            "work_authorization": None,
                        }
                    ),
                },
            )

        probe = OllamaProbe(InferenceConfig())
        await probe.client.aclose()
        probe.client = httpx.AsyncClient(
            base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(transport)
        )
        try:
            with pytest.raises(AdapterError, match="model_not_installed"):
                await probe.synthetic("missing")
            assert (await probe.synthetic("synthetic:1"))["synthetic_grounding_passed"]
            assert {request.url.path for request in calls} == {"/api/tags", "/api/generate"}
        finally:
            await probe.close()

    asyncio.run(scenario())


def test_official_page_requires_exact_board_link():
    from linkedin_bot.adapters.company import board_links

    board = Board(provider="ashby", slug="Synthetic")
    html = (
        '<a href="https://jobs.ashbyhq.com/Synthetic/role">role</a>'
        '<a href="https://jobs.ashbyhq.com/Other">other</a>'
        '<a href="https://jobs.ashbyhq.com.evil.test/Synthetic">fake</a>'
    )
    assert board_links(html, "https://example.org/careers", board) == (
        "https://jobs.ashbyhq.com/Synthetic/role",
    )
    assert board_links("jobs.ashbyhq.com/Synthetic", "https://example.org/careers", board) == ()
