"""Searching and reading the web.

The first thing here that leaves the machine, so half of this is about it being
named and switchable, and the other half is about parsing somebody else's page
without pretending that is a stable contract.

Nothing in here goes near the network. The HTML is a trimmed copy of what the
endpoint actually returned, kept so that a change of shape fails a test rather
than a conversation.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from jarvis.config import Config
from jarvis.tools import build_toolbox
from jarvis.web import is_an_advert, read, search, strip_html, unwrap

# One advert and two results, as their HTML endpoint lays them out.
PAGE = """
<div class="result results_links_deep result--ad">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/y.js?ad_domain=booking.com">
    90 Hotels in Canberra</a>
  <a class="result__snippet" href="//duckduckgo.com/y.js">Book your Hotel online.</a>
</div>
<div class="result results_links_deep">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FCanberra&amp;rut=x">
    Canberra - <b>Wikipedia</b></a>
  <a class="result__snippet" href="x">Canberra is the <b>capital</b> city of Australia.</a>
</div>
<div class="result results_links_deep">
  <a rel="nofollow" class="result__a" href="https://www.britannica.com/place/Canberra">
    Canberra | Britannica</a>
  <a class="result__snippet" href="x">Federal capital of the Commonwealth.</a>
</div>
"""


def answer(body, status=200):
    """A response that has been through a real request, so raise_for_status works."""
    return httpx.Response(status, text=body, request=httpx.Request("POST", "https://example.com"))


def refusal(body):
    """What being throttled looks like: a 202 carrying a challenge page."""
    return answer(f"<html>{body}</html>", status=202)


def answering(body, *, status=200, kind="text/html"):
    """An httpx transport that returns one page, whatever is asked of it."""

    def handle(request):
        return httpx.Response(status, text=body, headers={"content-type": kind})

    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """Nothing here goes near the network, so nothing here waits for it.

    Without this every search in this file sits out the one-a-second gap, which
    is a second per test for no reason at all.
    """
    import jarvis.web

    monkeypatch.setattr(jarvis.web.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(jarvis.web, "_last_query", 0.0)
    # Being cut off is remembered at module level, which is right in a running
    # JARVIS and cross-contamination here: one test tripping it would make every
    # search after it return "cut off" without going anywhere near the fake.
    monkeypatch.setattr(jarvis.web, "_blocked_until", 0.0)


@pytest.fixture
def endpoint(monkeypatch):
    """Point httpx's module level get/post at a fixed page."""

    def serve(body, **kwargs):
        transport = answering(body, **kwargs)

        def post(url, **rest):
            with httpx.Client(transport=transport) as client:
                return client.post(
                    url, **{k: v for k, v in rest.items() if k != "follow_redirects"}
                )

        def get(url, **rest):
            with httpx.Client(transport=transport) as client:
                return client.get(url, **{k: v for k, v in rest.items() if k != "follow_redirects"})

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(httpx, "get", get)

    return serve


# ------------------------------------------------------------------ searching


def test_a_search_comes_back_as_a_short_readable_list(endpoint):
    endpoint(PAGE)
    found = search("capital of australia")
    assert "1. Canberra - Wikipedia  [en.wikipedia.org]" in found
    assert "Canberra is the capital city of Australia." in found
    assert "https://en.wikipedia.org/wiki/Canberra" in found


def test_the_redirect_is_unwrapped_to_the_real_address():
    """Their results link through duckduckgo.com, which is no use to anybody
    wanting to read the page or say where an answer came from."""
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FCanberra&rut=x"
    assert unwrap(wrapped) == "https://en.wikipedia.org/wiki/Canberra"
    assert unwrap("https://example.com/plain") == "https://example.com/plain"


def test_adverts_are_left_out(endpoint):
    """The first result for anything commercial is usually paid, and a model
    reads the first result as the best answer and repeats it as fact."""
    endpoint(PAGE)
    found = search("capital of australia")
    assert "90 Hotels" not in found
    assert "booking.com" not in found


@pytest.mark.parametrize(
    "link",
    [
        "//duckduckgo.com/y.js?ad_domain=booking.com",
        "https://duckduckgo.com/y.js",
        "https://www.bing.com/aclick?ad_provider=bingv7aa",
    ],
)
def test_what_counts_as_an_advert(link):
    assert is_an_advert(link) is True


def test_a_real_result_is_not_mistaken_for_one():
    assert is_an_advert("https://en.wikipedia.org/wiki/Canberra") is False


def test_the_number_of_results_is_capped(endpoint):
    endpoint(PAGE)
    assert search("anything", limit=1).count("[") == 1


def test_a_page_that_has_changed_shape_says_so_rather_than_inventing(endpoint):
    """It is somebody's page rather than somebody's contract, so this is a
    question of when rather than if."""
    endpoint("<html><body>nothing we recognise</body></html>")
    found = search("capital of australia")
    assert "No results came back" in found
    assert "rather than inventing an answer" in found


def test_a_search_that_does_not_go_through_is_a_result_not_a_crash(monkeypatch):
    def refuse(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", refuse)
    assert "did not go through" in search("anything")


def test_an_empty_query_is_not_sent_anywhere():
    assert search("   ") == "Nothing to search for."


# -------------------------------------------------------------------- reading


def test_a_page_comes_back_as_its_sentences(endpoint):
    endpoint("<html><head><title>x</title></head><body><h1>Heading</h1><p>Some words.</p></body>")
    text = read("https://example.com")
    assert text == "Heading\nSome words."


def test_script_and_style_never_reach_the_model(endpoint):
    endpoint("<body><script>var x = 1;</script><style>p{color:red}</style><p>Real text.</p></body>")
    assert read("https://example.com") == "Real text."


def test_a_bare_domain_is_given_a_scheme(endpoint):
    endpoint("<p>Fine.</p>")
    assert read("example.com") == "Fine."


def test_a_long_page_is_cut_and_says_so(endpoint):
    endpoint("<p>" + ("word " * 2000) + "</p>")
    text = read("https://example.com", limit=200)
    assert len(text) < 300
    assert "more characters on the page" in text


def test_something_that_is_not_text_is_not_read(endpoint):
    endpoint("binary", kind="application/pdf")
    assert "nothing here to read" in read("https://example.com/a.pdf")


def test_a_page_built_by_script_says_what_happened(endpoint):
    """Not something to retry, something to say."""
    endpoint("<html><body><div id='root'></div></body></html>")
    assert "builds itself with script" in read("https://example.com")


def test_a_page_that_will_not_load_is_a_result_not_a_crash(monkeypatch):
    def refuse(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "get", refuse)
    assert "Could not read" in read("https://example.com")


def test_entities_are_turned_back_into_characters():
    assert strip_html("<p>Fish &amp; chips &mdash; nice</p>") == "Fish & chips — nice"


def test_block_ends_become_line_breaks():
    """Otherwise three headings and a paragraph arrive as one sentence."""
    assert strip_html("<h1>One</h1><p>Two</p><li>Three</li>") == "One\nTwo\nThree"


# ------------------------------------------------------------- switched off


def test_the_web_is_a_switch():
    config = replace(Config(), brain=replace(Config().brain, web=False))
    names = build_toolbox(config).names
    assert "search_web" not in names and "read_page" not in names


def test_it_is_on_by_default_because_there_is_no_local_version():
    assert "search_web" in build_toolbox(Config()).names


def test_the_search_engine_is_configurable(endpoint):
    """DuckDuckGo needs no key, which is why it is the default. Your own SearXNG
    is the version of this that cannot change shape underneath you."""
    endpoint(PAGE)
    assert "Canberra" in search("anything", url="http://127.0.0.1:8888/search")


# ------------------------------------------------------------------ throttled


def test_being_asked_to_slow_down_is_not_the_same_as_no_results(monkeypatch):
    """Four searches in two seconds is enough, and it is refused with a 202 and
    a challenge page - a success code carrying a refusal, which
    raise_for_status is perfectly happy with."""
    import jarvis.web

    monkeypatch.setattr(jarvis.web.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(httpx, "post", lambda url, **rest: refusal("anomaly detected"))
    found = search("anything")
    assert "refusing rather than answering" in found
    assert "one query a second" in found
    assert "open a browser and search there" in found, "the route that did work"


def test_being_cut_off_is_remembered_rather_than_rediscovered(monkeypatch):
    """One live session spent eight attempts and forty seconds finding out the
    same refusal over and over, and every attempt extends the block."""
    import jarvis.web

    tried = []

    def refuse(url, **rest):
        tried.append(url)
        return refusal("anomaly")

    monkeypatch.setattr(jarvis.web.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(httpx, "post", refuse)

    search("anything")
    attempts = len(tried)
    found = search("anything else")
    assert len(tried) == attempts, "the second search never went out"
    assert "cut me off for another" in found
    assert "makes it longer" in found


def test_one_throttled_attempt_is_retried(monkeypatch):
    import jarvis.web

    replies = [refusal("anomaly"), answer(PAGE)]
    waited = []
    monkeypatch.setattr(jarvis.web.time, "sleep", waited.append)
    monkeypatch.setattr(httpx, "post", lambda url, **rest: replies.pop(0))
    assert "Canberra" in search("capital of australia")
    assert waited.count(jarvis.web.PAUSE) == 1, "waited once, not forever"


# --------------------------------------------------------------- one a second


def test_a_second_search_waits_for_its_turn(monkeypatch):
    """Their limit is one query a second, and keeping to it is cheaper than
    being refused - 300ms of waiting is invisible in a conversation, and a
    refusal costs a whole turn."""
    import jarvis.web

    slept = []
    monkeypatch.setattr(jarvis.web.time, "sleep", slept.append)
    monkeypatch.setattr(jarvis.web.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(jarvis.web, "_last_query", 99.7)

    assert jarvis.web.wait_your_turn() == pytest.approx(0.7), "0.3s gone, 0.7s to wait"
    assert slept == [pytest.approx(0.7)]


def test_a_search_after_a_long_gap_goes_straight_out(monkeypatch):
    import jarvis.web

    slept = []
    monkeypatch.setattr(jarvis.web.time, "sleep", slept.append)
    monkeypatch.setattr(jarvis.web.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(jarvis.web, "_last_query", 90.0)

    assert jarvis.web.wait_your_turn() == 0.0
    assert slept == []
