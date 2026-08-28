"""Searching and reading the web, in as little code as it can be done in.

The first thing in this repository that leaves the machine, which is why it is a
switch and why `privacy_report` names it at startup. Nothing here is sent
anywhere except the query you asked for and the page you asked to read.

DuckDuckGo's HTML endpoint rather than an API, because every search API worth
using wants a key and the point of this project is that it works out of the box.
That is a real trade: it is somebody's page rather than somebody's contract, so
it can change shape without warning, and when it does this returns "no results"
rather than nonsense. Their limit is one query a second, which this keeps to
rather than discovers. `brain.search_url` points it at your own SearXNG instead,
which is the version of this with neither constraint.

Everything comes back as plain text, hard capped. A model reading a web page
wants the sentences; the markup is a thousand tokens of nothing.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger("jarvis.web")

DUCKDUCKGO = "https://html.duckduckgo.com/html/"

# One query a second is the published limit on their HTML endpoint, so the gap is
# kept rather than discovered: a search that waits 300ms is invisible in a
# conversation, and a search that comes back refused costs a whole turn. The
# retry is what covers being throttled anyway - one retry, because two turn a
# slow answer into an absent one and somebody is waiting to hear this.
MIN_GAP = 1.0
TRIES = 2
PAUSE = 1.5

# Once they have cut you off it lasts minutes, not seconds, and every further
# request extends it. So being refused is remembered and the next search does
# not go out at all - one live session spent eight attempts and forty seconds
# discovering the same refusal over and over, which is worse than useless.
BACKOFF = 90.0
_blocked_until = 0.0

# When the last query went out. Module level because the limit is per client
# rather than per caller, and there is one of us.
_last_query = 0.0
_turn = threading.Lock()

# Their endpoint returns a near empty page to anything that looks automated.
BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# One result: the link, then the snippet under it. Deliberately loose about
# attribute order and whitespace, since that is what changes between their
# deployments; the class names are the part that has been stable.
RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<text>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

DROPPED = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
BREAKS = re.compile(r"</(p|div|li|tr|h[1-6]|br)\s*>|<br\s*/?>", re.IGNORECASE)
TAGS = re.compile(r"<[^>]+>")


def strip_html(body: str) -> str:
    """A page as the sentences in it.

    Block ends become newlines first, so the text does not run three headings
    and a paragraph into one line - which is what a model then reads as one
    sentence.
    """
    body = DROPPED.sub(" ", body)
    body = BREAKS.sub("\n", body)
    body = TAGS.sub(" ", body)
    body = html.unescape(body)
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def unwrap(href: str) -> str:
    """The real address behind DuckDuckGo's redirect.

    Their results link to `//duckduckgo.com/l/?uddg=<encoded>`, which is no use
    to anybody wanting to read the page or say where something came from.
    """
    if "uddg=" not in href:
        return href
    query = parse_qs(urlparse(href if "//" not in href[:2] else f"https:{href}").query)
    found = (query.get("uddg") or [""])[0]
    return unquote(found) or href


def wait_your_turn(gap: float = MIN_GAP) -> float:
    """Hold off until a second has passed since the last query. Returns the wait."""
    global _last_query
    with _turn:
        waited = max(0.0, gap - (time.monotonic() - _last_query))
        if waited:
            time.sleep(waited)
        _last_query = time.monotonic()
    return waited


def is_an_advert(link: str) -> bool:
    """Whether a result is a paid placement rather than an answer.

    They come back through `y.js` and never leave duckduckgo.com, and the first
    result for anything commercial is usually one of them - which a model reads
    as the best answer and repeats out loud as fact.
    """
    return (
        "/y.js" in link
        or "ad_provider=" in link
        or urlparse(link).netloc.endswith("duckduckgo.com")
    )


def search(query: str, limit: int = 5, url: str = DUCKDUCKGO, timeout: float = 15.0) -> str:
    """Search, as a short numbered list of titles, sites and snippets."""
    global _blocked_until
    query = query.strip()
    if not query:
        return "Nothing to search for."
    logger.info("search_web: %s", query)

    if (left := _blocked_until - time.monotonic()) > 0:
        return (
            f"The search engine has cut me off for another {round(left)} seconds - its limit "
            "is one query a second and it has been passed. Trying again before then makes it "
            "longer. Tell them, and either wait or open a browser and search there instead."
        )

    body = ""
    for _attempt in range(TRIES):
        if waited := wait_your_turn():
            logger.debug("Held the search back %.2fs to stay inside the rate limit.", waited)
        try:
            response = httpx.post(
                url,
                data={"q": query},
                headers={"User-Agent": BROWSER},
                timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Search failed - %s", exc)
            return f"The search did not go through - {exc}. Say so rather than guessing an answer."

        # Past one a second they stop answering and start asking you to slow
        # down, with a 202 and a challenge page - a success code carrying a
        # refusal, which raise_for_status is perfectly happy with.
        if response.status_code != 202 and "anomaly" not in response.text[:2000]:
            body = response.text
            break
        logger.info("Throttled by the search engine, waiting %.1fs.", PAUSE)
        time.sleep(PAUSE)
    else:
        _blocked_until = time.monotonic() + BACKOFF
        logger.warning("Cut off by the search engine; not trying again for %.0fs.", BACKOFF)
        return (
            "The search engine is refusing rather than answering - its limit is one query a "
            f"second and it has been passed. It will not answer for about {round(BACKOFF)} "
            "seconds and trying again before then makes it longer. Tell them that, and offer "
            "to open a browser and search there instead. Do not call this again now."
        )

    titles = RESULT.finditer(body)
    snippets = [strip_html(match.group("text")) for match in SNIPPET.finditer(body)]

    found: list[str] = []
    for index, match in enumerate(titles):
        if len(found) >= max(1, limit):
            break
        link = unwrap(html.unescape(match.group("href")))
        if is_an_advert(link):
            continue
        title = strip_html(match.group("title"))
        snippet = snippets[index] if index < len(snippets) else ""
        where = urlparse(link).netloc or link
        found.append(f"{len(found) + 1}. {title}  [{where}]\n   {snippet}\n   {link}")

    if not found:
        return (
            f"No results came back for {query!r}. Either there are none or the search page "
            "has changed shape - say you could not find it rather than inventing an answer."
        )
    return "\n".join(found)


def read(url: str, limit: int = 3000, timeout: float = 20.0) -> str:
    """One page, as text.

    Capped hard: an article is a few thousand characters of sentences and forty
    thousand of navigation, and the cap is what stops one page filling the whole
    conversation.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    logger.info("read_page: %s", url)
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": BROWSER},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Fetch failed - %s", exc)
        return f"Could not read {url} - {exc}."

    kind = response.headers.get("content-type", "")
    if "html" not in kind and "text" not in kind:
        return f"{url} is {kind or 'not text'}, so there is nothing here to read."

    text = strip_html(response.text)
    if not text:
        return f"{url} came back empty, which usually means the page builds itself with script."
    if len(text) > limit:
        text = text[:limit] + f"\n... [cut here, {len(text) - limit} more characters on the page]"
    return text
