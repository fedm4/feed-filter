"""Reading an OPML export from another feed reader.

OPML is what every reader exports, and what nobody agrees on the details of: feeds appear
as `xmlUrl` on an `outline` element, nested to whatever depth the exporting app felt like,
sometimes with a `title` and sometimes only a `text`. So this walks the whole tree rather
than assuming a shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


class OpmlError(ValueError):
    """The upload is not OPML we can read."""


@dataclass(frozen=True, slots=True)
class OpmlFeed:
    name: str
    url: str


def parse_opml(data: bytes | str) -> list[OpmlFeed]:
    """Every feed in the document, in document order, deduplicated by URL."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise OpmlError(f"not valid XML: {exc}") from exc

    feeds: dict[str, OpmlFeed] = {}
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            # Outlines are also used as folders, which carry no xmlUrl at all.
            continue
        if url in feeds:
            continue
        name = (outline.get("title") or outline.get("text") or url).strip()
        feeds[url] = OpmlFeed(name=name, url=url)

    if not feeds:
        raise OpmlError("no feeds found; is this an OPML export?")
    return list(feeds.values())
