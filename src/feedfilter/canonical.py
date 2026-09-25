"""Reducing a URL to the article it points at.

The same story reaches us through several feeds, each adding its own tracking
parameters, and the raw links never match. Stripping the noise gives one key both copies
agree on, which is what makes exact deduplication possible at all.

The rule throughout is to remove only what is provably not part of the address. A
parameter that might select content stays, because a wrong merge loses an article
silently while a missed merge only shows a duplicate.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Parameters that identify the campaign that sent you, never the content.
TRACKING_PREFIXES = ("utm_",)

TRACKING_PARAMS = frozenset(
    {
        "fbclid",  # Facebook
        "gclid",  # Google Ads
        "dclid",  # DoubleClick
        "msclkid",  # Microsoft Ads
        "igshid",  # Instagram
        "mc_cid",  # Mailchimp campaign
        "mc_eid",  # Mailchimp recipient
        "ref",
        "referer",
        "referrer",
        "source",
        "src",
        "cmpid",
        "campaign_id",
        "sponsored",
        "at_medium",  # BBC
        "at_campaign",
        "ns_campaign",
        "ns_source",
        "ns_mchannel",
        "smid",  # New York Times
        "partner",
        "s_kwcid",
        "yclid",  # Yandex
        "_ga",
        "spm",
    }
)

#: Ports that say nothing, because the scheme already said it.
DEFAULT_PORTS = {"http": "80", "https": "443"}


def is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """The same article through two links should come out of here identical.

    Scheme and host are lowercased, since both are case-insensitive by definition, and a
    default port is dropped for the same reason. ``www.`` is *not* dropped: plenty of
    sites serve different things with and without it, and guessing wrong merges two
    articles into one.

    The fragment goes. It is never sent to the server, so it cannot be part of which
    document you get -- only of where you land inside it.

    Remaining query parameters are sorted, so two feeds that agree on the parameters but
    not their order agree here too.
    """
    if not url or not url.strip():
        return ""

    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        # Not an absolute URL. Nothing here can be normalised safely, and inventing a
        # scheme would be a guess, so it is handed back untouched.
        return url.strip()

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and str(parts.port) != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"

    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking(name)
    ]
    query = urlencode(sorted(kept))

    path = parts.path
    # A trailing slash on a path that is not the site root is almost always the same
    # document, and feeds disagree about it constantly. The root itself keeps its slash,
    # since "https://example.com" and "https://example.com/" want one spelling.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    return urlunsplit((scheme, host, path, query, ""))


def url_hash(url: str) -> str:
    """The dedup key: the canonical URL, hashed.

    A hash because SQLite indexes 64 fixed characters far more happily than a
    2000-character column, and because the value is only ever compared for equality.
    """
    return hashlib.sha256(canonicalize(url).encode("utf-8")).hexdigest()
