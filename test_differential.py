#!/usr/bin/env python3
"""Offline checks for the differential key refresh — no Stream Deck needed.

    .venv/bin/python test_differential.py

A fake deck stands in for the hardware: same image format as a Stream Deck XL,
records every write and can fail on demand, which is how the HID-error and the
reconnect paths are exercised without a device (and without taking the real one
away from the running service).
"""

import sys

from StreamDeck.ImageHelpers import PILHelper

import deck_stats as ds


class FakeDeck:
    """Stand-in for a Stream Deck XL: same native format, records the writes."""

    KEY_PIXEL_WIDTH = 96
    KEY_PIXEL_HEIGHT = 96
    KEY_IMAGE_FORMAT = "JPEG"
    KEY_FLIP = (True, True)
    KEY_ROTATION = 0

    def __init__(self, fail_keys=()):
        self.fail_keys = set(fail_keys)
        self.sends = 0
        self.resets = 0
        self.brightness = None
        self.last = {}

    # --- the slice of the deck API that run()/push_key() touch ---
    def deck_type(self):
        return "fake deck"

    def get_serial_number(self):
        return "FAKE"

    def is_open(self):
        return True

    def open(self):
        pass

    def close(self):
        pass

    def reset(self):
        self.resets += 1

    def set_brightness(self, value):
        self.brightness = value

    def key_layout(self):
        return (4, ds.COLS)

    def key_image_format(self):
        return {"size": (self.KEY_PIXEL_WIDTH, self.KEY_PIXEL_HEIGHT),
                "format": self.KEY_IMAGE_FORMAT,
                "flip": self.KEY_FLIP, "rotation": self.KEY_ROTATION}

    def set_key_image(self, key, data):
        if key in self.fail_keys:
            raise IOError("simulated HID failure")
        self.sends += 1
        self.last[key] = data


def native(deck, **render_kwargs):
    """The exact bytes run() would push for one key."""
    img = ds.render_key(**render_kwargs)
    data = PILHelper.to_native_key_format(deck, img)
    img.close()
    return data


# ---------------- push_key, in isolation ----------------

def test_identical_frame_is_skipped():
    deck = FakeDeck()
    pushed = {}
    frame = native(deck, label="TEMP", kind="temp", value=61.2, warn=75, crit=90,
                   cell=(0, 1))
    assert ds.push_key(deck, pushed, 1, frame) is True
    assert ds.push_key(deck, pushed, 1, frame) is False
    assert deck.sends == 1, deck.sends
    assert pushed[1] == frame


def test_failed_send_leaves_the_frame_owed():
    deck = FakeDeck(fail_keys={7})
    pushed = {}
    frame = native(deck, label="TEMP", kind="temp", value=61.2, warn=75, crit=90,
                   cell=(0, 1))
    try:
        ds.push_key(deck, pushed, 7, frame)
    except IOError:
        pass
    else:
        raise AssertionError("the HID failure should have propagated")
    assert 7 not in pushed, "cache must not record a frame that never arrived"
    deck.fail_keys.clear()
    assert ds.push_key(deck, pushed, 7, frame) is True, "the frame is still owed"


def test_colour_thresholds_change_the_frame():
    deck = FakeDeck()
    for kind, value_a, value_b in (("temp", 60.0, 95.0),      # warn/crossed
                                   ("peak", 40.0, 88.0),      # the MAX keys
                                   ("pct", 10.0, 86.0)):      # bar colour
        a = native(deck, label="X", kind=kind, value=value_a, warn=75, crit=90,
                   cell=(0, 1))
        b = native(deck, label="X", kind=kind, value=value_b, warn=75, crit=90,
                   cell=(0, 1))
        assert a != b, f"{kind}: crossing the threshold must change the frame"
        pushed = {}
        assert ds.push_key(deck, pushed, 0, a)
        assert not ds.push_key(deck, pushed, 0, a)
        assert ds.push_key(deck, pushed, 0, b), f"{kind}: not resent"


def test_sparkline_progress_changes_the_frame():
    deck = FakeDeck()
    kwargs = dict(label="TEMP", kind="temp", value=61.0, warn=75, crit=90,
                  cell=(0, 1))
    a = native(deck, history=[59.0, 60.0, 61.0], **kwargs)
    b = native(deck, history=[60.0, 61.0, 61.0], **kwargs)
    assert a != b, "an advancing sparkline is a new picture even if the " \
                   "number on it is unchanged"
    deck = FakeDeck()
    pushed = {}
    ds.push_key(deck, pushed, 1, a)
    assert ds.push_key(deck, pushed, 1, b) is True


# ---------------- the loop: first tick, later ticks, reconnect ----------------

def synthetic_widgets(value):
    """32 constant keys: no sparkline, no unit, no sub-line."""
    return [(row, col, f"K{row}{col}", "temp", lambda a, s: value[0], 75, 90)
            for row in range(4) for col in range(ds.COLS)]


def run_cycle(deck, ticks, value, mutate=None):
    """Call ds.run() with synthetic widgets; return frames sent per tick.

    mutate(tick_index) runs just before the tick renders, so a change made for
    tick n shows up in the count of tick n.
    """
    saved = (ds.WIDGETS, ds.UPDATE_SECONDS, ds.HISTORY_KEYS, ds.UNITS, ds.SUBS,
             ds._STOP, ds.App.sample)
    ds.WIDGETS = synthetic_widgets(value)
    ds.HISTORY_KEYS = set()
    ds.UNITS = {}
    ds.SUBS = {}
    ds.UPDATE_SECONDS = 0.0
    ds._STOP = False
    marks = []
    real_sample = ds.App.sample

    def counting(self):
        marks.append((len(marks), deck.sends))
        if mutate:
            mutate(len(marks) - 1)
        if len(marks) >= ticks:
            ds._STOP = True
        return real_sample(self)

    ds.App.sample = counting
    try:
        ds.run(deck)
    finally:
        (ds.WIDGETS, ds.UPDATE_SECONDS, ds.HISTORY_KEYS, ds.UNITS, ds.SUBS,
         ds._STOP, ds.App.sample) = saved
    totals = [s for _, s in marks] + [deck.sends]
    return [totals[i + 1] - totals[i] for i in range(len(totals) - 1)]


def test_first_tick_sends_all_later_only_changes():
    value = [42.0]
    deck = FakeDeck()

    def mutate(tick):                       # 0-based: shows up in this very tick
        if tick == 1:                       # cross the warn threshold mid-run
            value[0] = 95.0

    per_tick = run_cycle(deck, 6, value, mutate=mutate)
    assert per_tick[0] == 32, f"first tick must paint all keys: {per_tick}"
    assert per_tick[1] == 32, f"crossing the warn threshold repaints all: {per_tick}"
    assert per_tick[2:] == [0, 0, 0, 0], f"nothing changed, nothing to send: {per_tick}"
    return per_tick


def test_reconnect_repaints_everything():
    """Two cycles against the same deck: the cache never crosses the boundary."""
    value = [42.0]
    deck = FakeDeck()
    first = run_cycle(deck, 4, value)
    assert first == [32, 0, 0, 0], first
    deck2_sends_before = deck.sends
    second = run_cycle(deck, 3, value)          # same deck, new connection cycle
    assert second[0] == 32, f"after a reconnect every key is blank: {second}"
    assert second[1:] == [0, 0], second
    assert deck.resets == 4, deck.resets       # entry reset + exit blanking, x2 cycles
    assert deck.sends - deck2_sends_before == 32


def test_hid_failure_then_reconnect():
    """A failing write ends the cycle; the next one still owes that frame."""
    value = [42.0]
    deck = FakeDeck(fail_keys={3})
    try:
        run_cycle(deck, 4, value)
    except IOError:
        pass
    else:
        raise AssertionError("a HID failure must propagate out of run()")
    deck.fail_keys.clear()
    per_tick = run_cycle(deck, 3, value)
    assert per_tick[0] == 32, f"the reconnect must resend everything: {per_tick}"
    assert per_tick[1:] == [0, 0]


def main():
    checks = [test_identical_frame_is_skipped,
              test_failed_send_leaves_the_frame_owed,
              test_colour_thresholds_change_the_frame,
              test_sparkline_progress_changes_the_frame,
              test_first_tick_sends_all_later_only_changes,
              test_reconnect_repaints_everything,
              test_hid_failure_then_reconnect]
    for check in checks:
        check()
        print(f"ok  {check.__name__}")
    print(f"\n{len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
