"""Pure parsers from page HTML to records.

Every layout assumption lives in the ANCHORS block below. It is UNVERIFIED against real
LinkedIn pages until the supervised live step; a missing anchor raises `layout_changed`
instead of guessing.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from linkedin_bot.adapters.linkedin.models import (
    Company,
    Conversation,
    LinkedInError,
    Person,
    Profile,
    SessionState,
)

ANCHORS = {
    "conversations": "Conversations",
    "search_results": "Search results",
    "profile": "Profile",
    "about": "About",
    "experience": "Experience",
}
VOID = {"br", "img", "input", "meta", "link", "hr"}
CHALLENGE_PATHS = ("/checkpoint", "/challenge")
LOGIN_PATHS = ("/login", "/uas/login", "/authwall")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        parts = [c if isinstance(c, str) else c.text() for c in self.children]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def descendants(self) -> list[Node]:
        found: list[Node] = []
        for child in self.children:
            if isinstance(child, Node):
                found.append(child)
                found.extend(child.descendants())
        return found

    def find(self, match: Callable[[Node], bool]) -> Node | None:
        return next((n for n in self.descendants() if match(n)), None)


class Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    builder = Builder()
    builder.feed(html)
    return builder.root


def labelled(root: Node, label: str) -> Node | None:
    return root.find(lambda n: n.attrs.get("aria-label") == label)


def need(node: Node | None) -> Node:
    if node is None:
        raise LinkedInError("layout_changed")
    return node


def has_class(node: Node, name: str) -> bool:
    return name in node.attrs.get("class", "").split()


@dataclass(frozen=True)
class Parsed[T]:
    items: tuple[T, ...]
    skipped: int


def rows(container: Node) -> list[Node]:
    return [n for n in container.descendants() if n.tag == "li"]


def parse_messages(html: str) -> Parsed[Conversation]:
    container = need(labelled(parse_html(html), ANCHORS["conversations"]))
    items, skipped = [], 0
    for row in rows(container):
        title = row.find(lambda n: n.tag == "h3")
        snippet = row.find(lambda n: n.tag == "p")
        identifier = row.attrs.get("data-conversation-id")
        if not identifier or title is None or not title.text():
            skipped += 1
            continue
        items.append(
            Conversation(
                id=identifier,
                title=title.text(),
                snippet=snippet.text() if snippet else "",
                unread=row.attrs.get("data-unread") == "true",
            )
        )
    return Parsed(tuple(items), skipped)


def canonical(href: str, kind: str) -> str | None:
    """Normalise to https://www.linkedin.com/<kind>/<slug>, or None for anything else."""
    parts = urlsplit(href)
    match = re.fullmatch(rf"/{kind}/([A-Za-z0-9_%-]+)/?", parts.path)
    if parts.scheme != "https" or parts.hostname != "www.linkedin.com" or not match:
        return None
    return f"https://www.linkedin.com/{kind}/{match.group(1)}"


def search_rows(html: str, kind: str) -> tuple[list[tuple[str, Node]], int]:
    container = need(labelled(parse_html(html), ANCHORS["search_results"]))
    found, skipped = [], 0
    for row in rows(container):
        link = row.find(lambda n: n.tag == "a" and "href" in n.attrs)
        url = canonical(link.attrs["href"], kind) if link else None
        if url is None:
            skipped += 1
        else:
            found.append((url, row))
    return found, skipped


def field_text(row: Node, name: str) -> str:
    node = row.find(lambda n: has_class(n, name))
    return node.text() if node else ""


def parse_people(html: str) -> Parsed[Person]:
    found, skipped = search_rows(html, "in")
    items = []
    for url, row in found:
        name = field_text(row, "name")
        if not name:
            skipped += 1
            continue
        items.append(
            Person(
                url=url,
                name=name,
                headline=field_text(row, "headline"),
                location=field_text(row, "location") or None,
            )
        )
    return Parsed(tuple(items), skipped)


def parse_companies(html: str) -> Parsed[Company]:
    found, skipped = search_rows(html, "company")
    items = []
    for url, row in found:
        name = field_text(row, "name")
        if not name:
            skipped += 1
            continue
        items.append(Company(url=url, name=name, tagline=field_text(row, "headline")))
    return Parsed(tuple(items), skipped)


def parse_profile(html: str, url: str) -> Profile:
    main = need(labelled(parse_html(html), ANCHORS["profile"]))
    heading = need(main.find(lambda n: n.tag == "h1"))

    def data(name: str) -> str:
        node = main.find(lambda n: n.attrs.get("data-field") == name)
        return node.text() if node else ""

    about = labelled(main, ANCHORS["about"])
    experience = labelled(main, ANCHORS["experience"])
    return Profile(
        url=url,
        name=heading.text(),
        headline=data("headline"),
        location=data("location") or None,
        about=about.text() if about else "",
        experience=tuple(r.text() for r in rows(experience))[:20] if experience else (),
    )


def detect_state(url: str, html: str) -> SessionState:
    path = urlsplit(url).path
    if path.startswith(CHALLENGE_PATHS):
        return "challenge_required"
    if path.startswith(LOGIN_PATHS):
        return "auth_required"
    if "unusual activity" in html.lower():
        return "challenge_required"
    root = parse_html(html)
    if root.find(lambda n: n.tag == "input" and n.attrs.get("type") == "password"):
        return "auth_required"
    # Class names are obfuscated hashes; a signed-in page is recognised by its <main> region
    # on a URL that did not bounce to login (logged-out visits redirect there).
    return "ready" if root.find(lambda n: n.tag == "main") is not None else "unavailable"
