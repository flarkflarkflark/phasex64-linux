#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Offline source guards and a finite model of the watchdog/stop lock cycle.

Never opens a device. The model is not a kernel or USB fault-injection test:
it assumes bounded recovery operations and the workqueue cancellation contract.
"""

from dataclasses import dataclass, replace
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "linux/snd-usb-phasex64/phasex64.c").read_text()
CODE = re.sub(r"/\*.*?\*/|//[^\n]*", "", SOURCE, flags=re.S)


def body(name):
    match = re.search(r"^static [^\n]*\b" + name + r"\(", CODE, re.M)
    assert match, name
    return CODE[match.start():CODE.index("\n}", match.start()) + 2]


@dataclass(frozen=True)
class State:
    owner: str = ""
    stop: int = 0
    watchdog: int = 0
    running: bool = True
    pending: bool = False


def successors(s, trylock):
    # One already executing watchdog invocation and one process-context stop.
    # cancel_sync drains both that invocation and its self-requeued tick.
    if s.stop == 0 and not s.owner:
        yield replace(s, owner="stop", stop=1)
    elif s.stop == 1:
        yield replace(s, running=False, stop=2)
    elif s.stop == 2 and s.watchdog == 4:
        yield replace(s, pending=False, stop=3)
    elif s.stop == 3:
        yield replace(s, owner="", stop=4)

    if s.watchdog == 0:
        yield replace(s, pending=True, watchdog=1)
    elif s.watchdog == 1:
        if not s.owner:
            yield replace(s, owner="watchdog", watchdog=2)
        elif trylock:
            yield replace(s, watchdog=4)
    elif s.watchdog == 2:
        if s.running:
            yield replace(s, watchdog=3)
        else:
            yield replace(s, owner="", watchdog=4)
    elif s.watchdog == 3:
        # Abstract the finite USB recovery section; it never calls stop/cancel.
        yield replace(s, owner="", watchdog=4)


def explore(trylock):
    todo, seen, blocked = [State()], set(), []
    while todo:
        state = todo.pop()
        if state in seen:
            continue
        seen.add(state)
        following = list(successors(state, trylock))
        if not following and (state.stop != 4 or state.watchdog != 4):
            blocked.append(state)
        todo.extend(following)
    return seen, blocked


class WatchdogDeadlockTest(unittest.TestCase):
    def test_watchdog_never_waits_for_device_mutex(self):
        watchdog = body("px_cap_watchdog_fn")
        self.assertRegex(watchdog, r"if \(!mutex_trylock\(&px->mutex\)\)\s+return;")
        self.assertNotRegex(watchdog, r"\bmutex_lock(?:_interruptible|_killable)?\(")
        self.assertLess(watchdog.index("schedule_delayed_work("),
                        watchdog.index("mutex_trylock("))
        self.assertIn("!active || !px->running || px->disconnected", watchdog)
        self.assertNotIn("px_stop_streaming(", watchdog)
        self.assertNotIn("cancel_delayed_work_sync(", watchdog)

    def test_stop_keeps_synchronous_drain_without_spinlock(self):
        stop = body("px_stop_streaming")
        self.assertLess(stop.index("px->running = false;"),
                        stop.index("cancel_delayed_work_sync("))
        self.assertIn("px_kill_audio_urbs(px);", stop)
        # A spinlock may be taken and released earlier in the function,
        # but none may still be held where a synchronous cancel runs.
        cancels = [m.start() for m in re.finditer(r"cancel_delayed_work_sync\(", stop)]
        self.assertTrue(cancels)
        for at in cancels:
            before = stop[:at]
            self.assertEqual(len(re.findall(r"\bspin_lock\w*\(", before)),
                             len(re.findall(r"\bspin_unlock\w*\(", before)),
                             "spinlock held across cancel_delayed_work_sync()")

    def test_teardown_callers_remain_mutex_serialized(self):
        for name in ("px_pcm_close", "px_pcm_hw_free", "px_pcm_hw_params",
                     "px_suspend", "px_disconnect"):
            with self.subTest(name=name):
                caller = body(name)
                self.assertLess(caller.index("mutex_lock(&px->mutex)"),
                                caller.index("px_stop_streaming(px)"))
                self.assertLess(caller.index("px_stop_streaming(px)"),
                                caller.index("mutex_unlock(&px->mutex)"))
        prepare = body("px_pcm_prepare")
        self.assertLess(prepare.index("mutex_lock(&px->mutex)"),
                        prepare.index("px_start_streaming("))
        self.assertIn("px_stop_streaming(px);", body("px_start_streaming"))
        self.assertNotIn("cancel_delayed_work_sync(", body("px_pcm_trigger"))

    def test_model_reproduces_old_deadlock(self):
        _, blocked = explore(trylock=False)
        self.assertTrue(blocked)
        self.assertTrue(all(s.owner == "stop" and s.stop == 2 and
                            s.watchdog == 1 for s in blocked))

    def test_model_all_fixed_interleavings_drain(self):
        states, blocked = explore(trylock=True)
        self.assertFalse(blocked)
        final = [s for s in states if s.stop == s.watchdog == 4]
        self.assertTrue(final)
        self.assertTrue(all(not s.owner and not s.running and not s.pending
                            for s in final))

    def test_release_versions_agree(self):
        # The fix shipped in 0.1.11.1; every later release must keep the
        # three version sources in step and must not predate the fix.
        def version(pattern, text):
            match = re.search(pattern, text, re.M)
            self.assertIsNotNone(match, pattern)
            return tuple(int(x) for x in match.group(1).split("."))

        versions = {
            version(r'MODULE_VERSION\("([\d.]+)"\)', SOURCE),
            version(r'^PACKAGE_VERSION="([\d.]+)"',
                    (ROOT / "linux/snd-usb-phasex64/dkms.conf").read_text()),
            version(r"^pkgver=([\d.]+)$", (ROOT / "linux/PKGBUILD").read_text()),
        }
        self.assertEqual(len(versions), 1, versions)
        self.assertGreaterEqual(versions.pop(), (0, 1, 11, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
