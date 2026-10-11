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


class Pages:
    """A PublicHTTP stand-in that answers from a function of the URL."""

    def __init__(self, answer):
        self.answer, self.urls = answer, []

    async def get(self, url, *, max_bytes=None):
        self.urls.append((url, max_bytes))
        return self.answer(url)


def test_greenhouse_scheduled_read_keeps_the_whole_board_with_text_and_dates():
    jobs = [
        {
            "id": index,
            "title": f"Engineer {index}",
            "absolute_url": f"https://example.org/{index}",
            "location": {"name": "Berlin"},
            "content": "&lt;p&gt;We build &amp;amp; ship.&lt;/p&gt;"
            "&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
            "updated_at": "2026-10-01T10:00:00-04:00",
            **({"first_published": "2026-09-01T10:00:00-04:00"} if index == 0 else {}),
        }
        for index in range(60)
    ]
    http = Pages(lambda url: json.dumps({"jobs": jobs}).encode())
    page = asyncio.run(ATSSource(http, Board(provider="greenhouse", slug="acme")).collect_board())
    assert len(page.items) == 60 and page.coverage == "complete"
    assert page.items[0].description == "We build & ship.\nPython"
    assert page.items[0].published_at == "2026-09-01T10:00:00-04:00"
    assert page.items[1].published_at == "2026-10-01T10:00:00-04:00"
    assert http.urls[0][0].endswith("/jobs?content=true") and http.urls[0][1] > 2_000_000


def test_greenhouse_falls_back_to_the_list_when_descriptions_are_too_large():
    def answer(url):
        if "content=true" in url:
            raise AdapterError("response_too_large")
        return json.dumps(
            {"jobs": [{"id": 1, "title": "A", "absolute_url": "https://e.org/1", "location": {}}]}
        ).encode()

    http = Pages(answer)
    page = asyncio.run(ATSSource(http, Board(provider="greenhouse", slug="acme")).collect_board())
    assert [o.title for o in page.items] == ["A"] and page.items[0].description is None
    assert [url.endswith("/jobs") for url, _ in http.urls] == [False, True]


def test_lever_scheduled_read_walks_pages_and_reads_creation_dates():
    def answer(url):
        skip = int(url.rsplit("skip=", 1)[1])
        count = 100 if skip < 200 else 30
        return json.dumps(
            [
                {
                    "id": f"{skip + i}",
                    "text": "Intern",
                    "hostedUrl": f"https://jobs.lever.co/x/{skip + i}",
                    "categories": {"location": "Warsaw"},
                    "createdAt": 1_790_000_000_000,
                }
                for i in range(count)
            ]
        ).encode()

    http = Pages(answer)
    page = asyncio.run(ATSSource(http, Board(provider="lever", slug="x")).collect_board())
    assert len(page.items) == 230 and page.coverage == "complete" and page.next_cursor is None
    assert page.items[0].published_at.startswith("2026-")
    assert len(http.urls) == 3


def test_lever_scheduled_read_stops_after_a_bounded_number_of_pages():
    def answer(url):
        skip = int(url.rsplit("skip=", 1)[1])
        return json.dumps(
            [
                {"id": f"{skip + i}", "text": "T", "hostedUrl": "https://e.org", "categories": {}}
                for i in range(100)
            ]
        ).encode()

    http = Pages(answer)
    page = asyncio.run(ATSSource(http, Board(provider="lever", slug="x")).collect_board())
    assert len(http.urls) == 5 and page.coverage == "partial" and page.next_cursor == "500"


def test_board_limits_follow_the_provider():
    assert Board(provider="greenhouse", slug="a").endpoint(500, None)
    with pytest.raises(AdapterError, match="invalid_limit"):
        Board(provider="lever", slug="a").endpoint(101, None)
