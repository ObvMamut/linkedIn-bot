"""Identify exact board links in an explicitly chosen official careers page."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from linkedin_bot.adapters.ats import Board


class CareerLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("a", "iframe"):
            for name, value in attrs:
                if name in ("href", "src") and value:
                    self.links.append(value)


def board_links(html: str, official_page: str, board: Board) -> tuple[str, ...]:
    parser = CareerLinks()
    parser.feed(html)
    hosts = {
        "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
        "lever": {"jobs.lever.co"},
        "ashby": {"jobs.ashbyhq.com"},
    }
    verified: set[str] = set()
    for link in parser.links:
        url = urljoin(official_page, link)
        parts = urlsplit(url)
        path = parts.path.strip("/").split("/")
        if (
            parts.scheme == "https"
            and parts.hostname in hosts[board.provider]
            and path[0].casefold() == board.slug.casefold()
            and not parts.username
            and not parts.password
        ):
            verified.add(url)
    return tuple(sorted(verified))
