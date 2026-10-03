"""Pure parsers from page HTML to records.

Verified against live pages on 2026-10-03: class names are obfuscated hashes, so parsers rely
on `<main>`, headings, `aria-label`s, `role`s and link shapes. `data-view-name` markers appear
on some loads only, so they are never relied on. Every
such assumption lives in ANCHORS; a missing one raises `layout_changed` instead of guessing.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from linkedin_bot.adapters.linkedin.models import (
    Company,
    Conversation,
    Job,
    LinkedInError,
    Person,
    Profile,
    SessionState,
)

ANCHORS = {
    "conversation_list": "Conversation List",
    "about": "About",
    "experience": "Experience",
    "no_results": "no results found",
    "no_jobs": "no matching jobs",
}
PRONOUNS = re.compile(r"^[A-Za-z]+/[A-Za-z]+$")
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


def fragments(node: Node) -> list[str]:
    """Visible text pieces in document order, with immediate repeats collapsed.

    Result cards repeat the name once for screen readers, so ["Jane", "Jane", ...] becomes
    ["Jane", ...].
    """
    pieces: list[str] = []

    def walk(current: Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                text = re.sub(r"\s+", " ", child).strip()
                if text and (not pieces or pieces[-1] != text):
                    pieces.append(text)
            elif child.tag not in ("script", "style"):
                walk(child)

    walk(node)
    return pieces


def main_region(html: str) -> Node:
    return need(parse_html(html).find(lambda n: n.tag == "main"))


def parse_messages(html: str) -> Parsed[Conversation]:
    main = main_region(html)
    container = need(labelled(main, ANCHORS["conversation_list"]))
    items: list[Conversation] = []
    skipped = 0
    for row in rows(container):
        if not row.text():
            continue  # the list pads itself with empty items
        title = row.find(lambda n: n.tag == "h3")
        snippet = row.find(lambda n: n.tag == "p")
        when = row.find(lambda n: n.tag == "time")
        if title is None or not title.text():
            skipped += 1
            continue
        items.append(
            Conversation(
                id=str(len(items)),
                title=title.text(),
                snippet=snippet.text() if snippet else "",
                unread="unread" in row.text().lower(),
                when=when.text() if when else "",
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


def result_cards(html: str, kind: str) -> tuple[list[tuple[str, Node, Node]], int]:
    """Cards are `role=listitem` regions holding a /<kind>/ link. Returns (url, link, card)s and
    the number of listitems with text but no usable link."""
    main = main_region(html)
    found: list[tuple[str, Node, Node]] = []
    skipped = 0
    for card in (n for n in main.descendants() if n.attrs.get("role") == "listitem"):
        link = card_link(card, kind)
        if link is not None:
            found.append((link[0], link[1], card))
        elif card.text():
            skipped += 1
    if not found and not skipped and ANCHORS["no_results"] not in main.text().lower():
        raise LinkedInError("layout_changed")
    return found, skipped


def card_link(card: Node, kind: str) -> tuple[str, Node] | None:
    """The richest matching link: an avatar link and a text link usually share a card."""
    options = []
    for link in card.descendants():
        url = canonical(link.attrs.get("href", ""), kind) if link.tag == "a" else None
        if url is not None:
            options.append((len(link.text()), url, link))
    if not options:
        return None
    _, url, link = max(options, key=lambda option: option[0])
    return url, link


def parse_people(html: str) -> Parsed[Person]:
    cards, skipped = result_cards(html, "in")
    items: list[Person] = []
    for url, link, _card in cards:
        pieces = [p for p in fragments(link) if not p.startswith("•")]
        if not pieces:
            skipped += 1
            continue
        items.append(
            Person(
                url=url,
                name=pieces[0],
                headline=pieces[1] if len(pieces) > 1 else "",
                location=pieces[2] if len(pieces) > 2 else None,
            )
        )
    return Parsed(tuple(items), skipped)


def parse_companies(html: str) -> Parsed[Company]:
    cards, skipped = result_cards(html, "company")
    items: list[Company] = []
    for url, _link, card in cards:
        pieces = fragments(card)
        if not pieces:
            skipped += 1
            continue
        items.append(Company(url=url, name=pieces[0], tagline=", ".join(pieces[1:3])))
    return Parsed(tuple(items), skipped)


def canonical_job(href: str) -> tuple[str, str] | None:
    """(id, canonical URL) for a LinkedIn job link; relative links are what the site emits."""
    parts = urlsplit(href)
    relative = parts.scheme == "" and parts.netloc == ""
    absolute = parts.scheme == "https" and parts.hostname == "www.linkedin.com"
    match = re.fullmatch(r"/jobs/view/(\d+)/?", parts.path)
    if not (relative or absolute) or not match:
        return None
    return match.group(1), f"https://www.linkedin.com/jobs/view/{match.group(1)}"


def parse_jobs(html: str) -> Parsed[Job]:
    main = main_region(html)
    items: list[Job] = []
    seen: set[str] = set()
    skipped = 0
    cards = 0
    for row in (n for n in main.descendants() if n.tag == "li"):
        found = next(
            (
                (link, job)
                for link in row.descendants()
                if link.tag == "a" and (job := canonical_job(link.attrs.get("href", "")))
            ),
            None,
        )
        if found is None:
            continue
        cards += 1
        (identifier, url) = found[1]
        if identifier in seen:
            continue
        seen.add(identifier)
        pieces = fragments(row)
        if len(pieces) < 2:
            skipped += 1
            continue
        items.append(
            Job(
                id=identifier,
                url=url,
                title=pieces[0],
                company=pieces[1],
                location=pieces[2] if len(pieces) > 2 else None,
            )
        )
    if cards == 0 and ANCHORS["no_jobs"] not in main.text().lower():
        raise LinkedInError("layout_changed")
    return Parsed(tuple(items), skipped)


def heading_block(main: Node, title: str) -> Node | None:
    """The smallest ancestor of an h2 titled `title` that holds more than the heading."""
    heading = main.find(lambda n: n.tag == "h2" and n.text() == title)
    if heading is None:
        return None
    parents = {
        id(c): n for n in [main, *main.descendants()] for c in n.children if isinstance(c, Node)
    }
    block: Node = heading
    while block.text() == title and id(block) in parents:
        block = parents[id(block)]
    return block


def parse_profile(html: str, url: str) -> Profile:
    main = main_region(html)
    name_node = need(main.find(lambda n: n.tag == "h2" and bool(n.text())))
    pieces = fragments(main)
    after = pieces[pieces.index(name_node.text()) + 1 :] if name_node.text() in pieces else []
    plain = [p for p in after if not PRONOUNS.match(p) and not p.startswith("Verify")]
    headline = plain[0] if plain else ""
    location = None
    if "·" in plain:
        before = plain[: plain.index("·")]
        location = before[-1] if len(before) > 1 else None
    about_block = heading_block(main, ANCHORS["about"])
    experience_block = heading_block(main, ANCHORS["experience"])
    return Profile(
        url=url,
        name=name_node.text(),
        headline=headline,
        location=location,
        about=about_block.text().removeprefix(ANCHORS["about"]).strip() if about_block else "",
        experience=tuple(r.text() for r in rows(experience_block))[:20] if experience_block else (),
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
