"""Cutting the accessibility tree down to something a model can act on.

None of this needs a desktop. The COM half lives in uia.py and hands back plain
`Element`s, which is the whole reason the judgement can be tested at all.
"""

from __future__ import annotations

import json
import time

import pytest

from conftest import FakeDesktop, button
from jarvis.config import ScreenConfig
from jarvis.screen import (
    Element,
    Scan,
    Screen,
    ScreenUnavailable,
    Target,
    confirms,
    means_the_same,
    select,
)


def make_screen(elements=(), **kwargs) -> tuple[Screen, FakeDesktop]:
    backend = FakeDesktop(elements, **kwargs)
    return Screen(ScreenConfig(focus_settle_seconds=0.0), backend=backend), backend


# --------------------------------------------------------------------- filtering


def test_only_what_can_be_acted_on_survives():
    """The measured case: one Teams window is 810 nodes and 54 of them matter."""
    targets, _ = select(
        [
            button("Send"),
            Element("a whole paragraph of text", "Text", 0, 40, 400, 20),
            Element("", "Pane", 0, 0, 800, 600),
            Element("", "Image", 0, 70, 32, 32),
            Element("", "ScrollBar", 780, 0, 16, 600),
        ]
    )
    assert [target.element.label for target in targets] == ["Send"]


def test_an_unnamed_text_box_is_still_somewhere_to_type():
    """An unnamed button is noise. An unnamed edit is the message field."""
    targets, _ = select([Element("", "Edit", 0, 0, 200, 24), button("")])
    assert [t.element.role for t in targets] == ["Edit"]
    assert targets[0].as_dict()["accepts_text"] is True


def test_offscreen_and_disabled_are_left_out():
    targets, _ = select(
        [
            button("Visible"),
            button("Scrolled away", top=40, offscreen=True),
            button("Greyed out", top=80, enabled=False),
        ]
    )
    assert [t.element.name for t in targets] == ["Visible"]


def test_dividers_are_not_targets():
    """A one pixel separator has a name and a control type and is not clickable."""
    targets, _ = select([button("Resize", width=4, height=900), button("Real")])
    assert [t.element.name for t in targets] == ["Real"]


def test_a_container_holding_its_children_is_dropped():
    """A tree item wrapping nine chats has the same rectangle as all nine, and
    clicking it does nothing anyone wanted."""
    targets, _ = select(
        [
            Element("Chats", "TreeItem", 0, 0, 400, 300),
            Element("Alpha team", "TreeItem", 0, 0, 400, 70),
            Element("Standup", "TreeItem", 0, 70, 400, 70),
            Element("Nathan", "TreeItem", 0, 140, 400, 70),
        ]
    )
    assert "Chats" not in [t.element.name for t in targets]
    assert len(targets) == 3


def test_a_button_enclosing_its_own_label_survives():
    """One enclosed child is a label, two is a container. The line is at two."""
    targets, _ = select([button("Save"), Element("Save", "Text", 4, 4, 40, 16)])
    assert [t.element.name for t in targets] == ["Save"]


def test_a_wrapper_mirroring_one_control_is_deduplicated():
    targets, _ = select([button("Send", width=100, height=40), button("Send", width=80, height=24)])
    assert len(targets) == 1
    assert targets[0].element.width == 80, "the smaller of the two is the real one"


def test_two_controls_with_the_same_name_elsewhere_both_survive():
    targets, _ = select([button("Delete", top=0), button("Delete", top=100)])
    assert len(targets) == 2


# ---------------------------------------------------------------------- ordering


def test_numbered_in_reading_order():
    targets, _ = select(
        [button("third", left=0, top=100), button("second", left=200), button("first", left=0)]
    )
    assert [t.element.name for t in targets] == ["first", "second", "third"]


def test_a_row_is_not_reordered_by_two_pixels():
    """Controls on one toolbar do not sit at identical tops, and ordering them
    by that difference puts the rightmost button first."""
    targets, _ = select([button("right", left=200, top=12), button("left", left=0, top=10)])
    assert [t.element.name for t in targets] == ["left", "right"]


def test_the_limit_is_reported_rather_than_hidden():
    targets, truncated = select([button(f"b{n}", top=n * 30) for n in range(20)], limit=5)
    assert len(targets) == 5
    assert truncated == 15
    assert Scan(1, "w", 1, targets, 20, truncated, 0.0).as_dict()["not_shown"] == 15


def test_matching_narrows_a_crowded_window():
    targets, _ = select(
        [button("Reply", top=0), button("Reply all", top=30), button("Delete", top=60)],
        matching="reply",
    )
    assert [t.element.name for t in targets] == ["Reply", "Reply all"]
    assert [t.number for t in targets] == [1, 2], "numbered from one within the filter"


# ------------------------------------------------------------------- ambiguity


def test_a_repeated_label_is_placed_by_what_it_follows():
    """Four buttons called Close and nothing to choose between them is how a
    model picks the wrong one three times in four."""
    targets, _ = select(
        [
            Element("Inbox tab", "TabItem", 0, 0, 200, 30),
            button("Close", left=200, width=24, height=24),
            Element("Sent tab", "TabItem", 230, 0, 200, 30),
            button("Close", left=430, width=24, height=24),
        ]
    )
    hints = {t.element.name: t.where for t in targets}
    assert hints["Inbox tab"] == "", "unique labels are left alone"
    closes = [t.where for t in targets if t.element.name == "Close"]
    assert closes == ["after Inbox tab", "after Sent tab"]


def test_a_repeated_label_with_no_neighbour_falls_back_to_a_ninth():
    targets, _ = select(
        [button("Close", left=10, top=10), button("Close", left=700, top=500)],
        bounds=(0, 0, 900, 600),
    )
    assert [t.where for t in targets] == ["top left", "bottom right"]


def test_the_hint_survives_the_filter_that_removes_its_neighbour():
    """Searching for "close" leaves four buttons and none of their tabs, so the
    hint has to be worked out before the filter runs, not after."""
    targets, _ = select(
        [
            Element("Inbox tab", "TabItem", 0, 0, 200, 30),
            button("Close", left=200, width=24, height=24),
            Element("Sent tab", "TabItem", 230, 0, 200, 30),
            button("Close", left=430, width=24, height=24),
        ],
        matching="close",
    )
    assert [t.where for t in targets] == ["after Inbox tab", "after Sent tab"]


# ------------------------------------------------------------ still there?


def test_a_runtime_id_settles_it():
    target = Target(1, button("Send", runtime_id=(42, 7)))
    assert confirms(target, [button("", runtime_id=(42, 7))]) is True


def test_the_match_is_looked_for_up_the_chain():
    """The centre of a button lands on the label inside it as often as not."""
    target = Target(1, button("Send", runtime_id=(42, 7)))
    chain = [Element("Send", "Text", 4, 4, 40, 16), button("Send", runtime_id=(42, 7))]
    assert confirms(target, chain) is True


def test_a_rebuilt_control_still_counts_as_the_same_thing():
    """Virtualised lists rebuild rows constantly and the runtime id changes with
    them. Refusing on that alone would refuse most correct clicks."""
    target = Target(1, button("Nathan Murfey", runtime_id=(42, 7)))
    assert confirms(target, [button("Nathan Murfey", runtime_id=(42, 99))]) is True


def test_something_else_there_now_is_not_confirmed():
    target = Target(1, button("Reply", runtime_id=(42, 7)))
    assert confirms(target, [button("Delete", runtime_id=(42, 8))]) is False


def test_an_unnamed_element_cannot_confirm_by_name():
    """Two blank panes are not evidence that the right one is under the pointer."""
    target = Target(1, Element("", "Pane", 0, 0, 10, 10))
    assert confirms(target, [Element("", "Pane", 0, 0, 10, 10)]) is False


# ------------------------------------------------------- saying what you mean


@pytest.mark.parametrize(
    ("claimed", "actual", "accepted"),
    [
        ("Reply", "Reply", True),
        ("reply  all", "Reply all", True),
        ("Reply", "Reply to Nathan Murfey about the valve", True),
        ("Gutenberg standup, 9:30 to 10:00, Fri", "Gutenberg standup, 9:30", True),
        ("Reply", "Delete", False),
        ("OK", "OK", True),
        ("a", "Archive", False),
        ("", "Reply", False),
    ],
)
def test_what_the_agent_claims_has_to_be_what_is_there(claimed, actual, accepted):
    assert means_the_same(claimed, actual) is accepted


# ------------------------------------------------------------------ the scan


def test_a_scan_survives_being_written_out_and_read_back():
    targets, _ = select([button("Send", runtime_id=(42, 7)), Element("", "Edit", 0, 40, 200, 24)])
    scan = Scan(3, "Notepad", 99, targets, 120, 4, time.monotonic(), matching="e")
    again = Scan.from_json(json.loads(json.dumps(scan.as_json())))

    assert (again.id, again.window, again.hwnd) == (3, "Notepad", 99)
    assert (again.considered, again.truncated, again.matching) == (120, 4, "e")
    assert [t.element for t in again.targets] == [t.element for t in scan.targets]
    assert again.targets[0].element.runtime_id == (42, 7), "a tuple, not the JSON list"


def test_a_reloaded_scan_keeps_its_real_age():
    """taken_at is monotonic and means nothing in another process, so the age is
    carried across on the wall clock instead."""
    scan = Scan(1, "w", 1, (), 0, 0, time.monotonic())
    written = scan.as_json()
    written["saved_at"] -= 30
    assert 29 < Scan.from_json(written).age() < 32


def test_a_target_is_described_without_coordinates():
    described = Target(4, button("Send", left=1200, top=900)).as_dict()
    assert described == {"id": 4, "label": "Send", "role": "Button"}
    assert "1200" not in json.dumps(described)


def test_a_long_label_is_cut_to_size():
    """A chat row carries its whole last message as its name, and sixty of those
    is the prompt this was meant to shrink."""
    described = Target(1, button("x" * 300)).as_dict(label_chars=40)
    assert len(described["label"]) == 40


# ----------------------------------------------------------------- looking


def test_looking_scans_the_window_in_front_by_default():
    screen, _backend = make_screen([button("Send")], title="Mail")
    scan = screen.look()
    assert (scan.window, scan.considered, len(scan.targets)) == ("Mail", 1, 1)
    assert screen.latest is scan


def test_a_window_is_found_by_any_part_of_its_title():
    screen, _ = make_screen([button("Send")], title="Mail - Outlook - Google Chrome")
    assert screen.look("outlook").window == "Mail - Outlook - Google Chrome"


def test_an_unmatched_window_name_says_what_is_open():
    screen, _ = make_screen(title="Mail", others=[(2, "Notepad")])
    with pytest.raises(ScreenUnavailable, match="Notepad"):
        screen.look("Excel")


def test_a_minimised_window_is_refused_rather_than_scanned():
    """It still reports a full tree with coordinates from wherever it was last
    drawn, so scanning it hands back numbers that point at other applications."""
    screen, _ = make_screen([button("Send")], title="Mail", minimised=True)
    with pytest.raises(ScreenUnavailable, match="minimised"):
        screen.look()


def test_focusing_raises_the_window_then_scans_it():
    screen, backend = make_screen([button("Send")], title="Mail", hwnd=7, front=2)
    scan = screen.focus("Mail")
    assert backend.activations == [7]
    assert scan.window == "Mail"


def test_scan_ids_go_up_so_two_looks_can_be_told_apart():
    screen, _ = make_screen([button("Send")])
    assert [screen.look().id, screen.look().id] == [1, 2]


# -------------------------------------------------------------------- aiming


def test_aiming_before_looking_says_so():
    screen, _ = make_screen()
    with pytest.raises(ScreenUnavailable, match="Nothing has been scanned"):
        screen.aim(1)


def test_a_number_outside_the_scan_says_what_the_range_was():
    screen, _ = make_screen([button("Send")])
    screen.look()
    with pytest.raises(ScreenUnavailable, match="1 to 1"):
        screen.aim(4)


def test_an_expired_scan_is_refused():
    screen, _ = make_screen([button("Send")])
    screen.config = ScreenConfig(max_scan_age_seconds=10.0, focus_settle_seconds=0.0)
    scan = screen.look()
    object.__setattr__(scan, "taken_at", scan.taken_at - 60)
    with pytest.raises(ScreenUnavailable, match="expired"):
        screen.aim(1)


def test_the_window_is_raised_before_the_check_not_after():
    """Whatever is under a point in a covered window is the window covering it,
    so checking first would refuse every background target on those grounds."""
    screen, backend = make_screen([button("Send")], title="Mail", hwnd=7, front=99)
    screen.look("Mail")
    target, _ = screen.aim(1)
    assert backend.activations == [7]
    assert target.element.name == "Send"


def test_an_already_raised_window_is_not_raised_again():
    screen, backend = make_screen([button("Send")], hwnd=7, front=7)
    screen.look()
    screen.aim(1)
    assert backend.activations == []


def test_a_target_that_has_moved_is_refused():
    screen, backend = make_screen([button("Reply")])
    screen.look()
    backend._elements = [button("Delete")]
    with pytest.raises(ScreenUnavailable, match="something else is there now"):
        screen.aim(1)


def test_a_saved_scan_can_be_acted_on_by_another_process(tmp_path):
    """`jarvis look` prints numbers in one process and `jarvis click` uses them
    in the next, so the map has to outlive the process that made it."""
    screen, backend = make_screen([button("Send", runtime_id=(1, 2))])
    screen.look()
    screen.remember(tmp_path / "scan.json")

    second = Screen(ScreenConfig(focus_settle_seconds=0.0), backend=backend)
    reloaded = second.recall(tmp_path / "scan.json")
    assert [t.element.name for t in reloaded.targets] == ["Send"]
    assert second.aim(1)[0].element.name == "Send"


def test_nothing_to_save_says_so(tmp_path):
    screen, _ = make_screen()
    with pytest.raises(ScreenUnavailable, match="nothing to save"):
        screen.remember(tmp_path / "scan.json")


def test_a_missing_saved_scan_points_at_the_command_that_writes_it(tmp_path):
    screen, _ = make_screen()
    with pytest.raises(ScreenUnavailable, match="jarvis look"):
        screen.recall(tmp_path / "never-written.json")


def test_an_exact_title_wins_over_one_that_merely_contains_it():
    """There are two taskbars on a two monitor desk and only one is called
    Taskbar. Substring alone lands on whichever came back first."""
    screen, _ = make_screen(
        [button("Send")], title="Taskbar (second screen)", others=[(2, "Taskbar")]
    )
    assert screen.look("Taskbar").window == "Taskbar"


# ------------------------------------------------- a tree that never populated


def test_a_single_full_window_target_is_not_clickable():
    """The Start menu reports one element: itself. It arrives as a target whose
    rectangle is the whole window, so its centre is the middle of the panel
    rather than a control, and the point check refuses it forever."""
    from jarvis.screen import offers_nothing_clickable

    whole = [Target(1, Element("Search box", "Edit", 0, 0, 800, 600))]
    assert offers_nothing_clickable(whole, (0, 0, 800, 600)) is True


def test_a_real_control_that_happens_to_be_alone_is_still_clickable():
    from jarvis.screen import offers_nothing_clickable

    one_button = [Target(1, button("Send", left=300, top=500))]
    assert offers_nothing_clickable(one_button, (0, 0, 800, 600)) is False


def test_two_targets_are_never_a_dead_tree():
    """The rule is about a window standing in for its own contents. Two of
    anything means the tree populated."""
    from jarvis.screen import offers_nothing_clickable

    pair = [
        Target(1, Element("a", "Edit", 0, 0, 800, 600)),
        Target(2, button("Send", top=500)),
    ]
    assert offers_nothing_clickable(pair, (0, 0, 800, 600)) is False
