#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Non-hardware tests for the PHASE X64 panel process lifecycle."""

from __future__ import annotations

import gc
import importlib.machinery
import importlib.util
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


PANEL_PATH = Path(__file__).with_name("phasex64-panel")
loader = importlib.machinery.SourceFileLoader("phasex64_panel", str(PANEL_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
panel = importlib.util.module_from_spec(spec)
loader.exec_module(panel)


class MonitorLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.real_popen = subprocess.Popen
        self.children = []

    def tearDown(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()

    def launch_child(self, _command, **kwargs):
        # Signal readiness, then behave like a long-running line-oriented
        # monitor. SIGTERM has its normal action, exercising real reap logic.
        command = [sys.executable, "-u", "-c",
                   "import time; print('event', flush=True); time.sleep(60)"]
        child = self.real_popen(command, **kwargs)
        self.children.append(child)
        return child

    def test_start_stop_reaps_owned_process(self) -> None:
        changed = threading.Event()
        mixer = panel.Mixer("test")
        with mock.patch.object(panel.subprocess, "Popen", self.launch_child):
            thread = mixer.start_change_monitor(changed.set)
            self.assertTrue(changed.wait(2.0))
            child = self.children[-1]
            mixer.stop_change_monitor()

        self.assertIsNotNone(child.returncode)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(mixer._monitor_proc)

    def test_restart_reaps_previous_process_only(self) -> None:
        mixer = panel.Mixer("test")
        with mock.patch.object(panel.subprocess, "Popen", self.launch_child):
            mixer.start_change_monitor(lambda: None)
            first = self.children[-1]
            mixer.start_change_monitor(lambda: None)
            second = self.children[-1]
            self.assertIsNotNone(first.returncode)
            self.assertIsNone(second.poll())
            mixer.stop_change_monitor()

        self.assertIsNotNone(second.returncode)
        self.assertEqual(len(self.children), 2)

    def test_card_reload_restarts_monitor_for_new_card(self) -> None:
        mixer = panel.Mixer("3")
        with mock.patch.object(panel.subprocess, "Popen", self.launch_child), \
                mock.patch.object(panel, "find_card", return_value="4"):
            mixer.start_change_monitor(lambda: None)
            first = self.children[-1]
            mixer.ensure_change_monitor(lambda: None)
            second = self.children[-1]
            self.assertIsNotNone(first.returncode)
            self.assertIsNone(second.poll())
            self.assertEqual(mixer.card, "4")
            mixer.stop_change_monitor()

        self.assertIsNotNone(second.returncode)

    def test_panel_close_stops_monitor(self) -> None:
        mixer = mock.Mock()
        root = mock.Mock()
        instance = panel.Panel.__new__(panel.Panel)
        instance.root = root
        instance.mixer = mixer
        instance.closed = False
        instance.refresh_job = 123

        instance.close()
        instance.close()

        root.after_cancel.assert_called_once_with(123)
        mixer.stop_change_monitor.assert_called_once_with()
        root.destroy.assert_called_once_with()

    def test_device_disappears_clears_card_and_monitor(self):
        mixer = panel.Mixer("3")
        mixer.available = set(panel.PEAK_NAMES)
        with mock.patch.object(panel.subprocess, "Popen", self.launch_child), \
                mock.patch.object(panel, "find_card", return_value=None):
            mixer.start_change_monitor(lambda: None)
            child = self.children[-1]
            mixer.ensure_change_monitor(lambda: None)
        self.assertIsNotNone(child.returncode)
        self.assertIsNone(mixer.card)
        self.assertFalse(mixer.available)

    def test_midi_monitor_uses_dynamic_port_and_sigint_shutdown(self):
        launched = []

        def launch(_command, **kwargs):
            launched.append(_command)
            child = self.real_popen(
                [sys.executable, "-u", "-c",
                 "import time; print(' 16:0   Note on 0, note 60, velocity 100', flush=True); "
                 "time.sleep(60)"], **kwargs)
            self.children.append(child)
            return child

        monitor = panel.MidiMonitor()
        with mock.patch.object(monitor, "_discover", return_value=("16:0", "connected")), \
                mock.patch.object(panel.subprocess, "Popen", launch):
            self.assertEqual(monitor.ensure(), "connected")
            self.assertIn("Note on", monitor.events.get(timeout=2))
            child = self.children[-1]
            monitor.close()

        self.assertEqual(launched,
                         [panel._with_pdeathsig(["stdbuf", "-oL", "aseqdump", "-p", "16:0"])])
        self.assertIsNotNone(child.returncode)
        self.assertFalse(monitor._thread)

    def test_midi_monitor_flushes_single_line_sysex_as_one_event(self):
        # The common case per hardware testing: aseqdump puts the whole
        # SysEx dump on one line (a wrapped terminal just made it look
        # multi-line) - must appear as exactly one event, flushed once
        # the next real event line (or EOF) confirms nothing more is
        # coming.
        def launch(_command, **kwargs):
            child = self.real_popen(
                [sys.executable, "-u", "-c",
                 "import time; "
                 "print(' 16:0   System exclusive          F0 7D 01 02 F7', flush=True); "
                 "print(' 16:0   Note on                 0, note 1, velocity 2', flush=True); "
                 "time.sleep(60)"], **kwargs)
            self.children.append(child)
            return child

        monitor = panel.MidiMonitor()
        with mock.patch.object(monitor, "_discover", return_value=("16:0", "connected")), \
                mock.patch.object(panel.subprocess, "Popen", launch):
            monitor.ensure()
            first = monitor.events.get(timeout=2)
            second = monitor.events.get(timeout=2)
            monitor.close()
        self.assertIn("System exclusive", first)
        self.assertIn("F0 7D 01 02 F7", first)
        self.assertIn("Note on", second)

    def test_midi_monitor_merges_multi_line_sysex_continuation(self):
        # Handles genuine multi-line output too, not just the single-line
        # case above - continuation lines are bare hex byte pairs with no
        # source-address prefix. The buffered SysEx only flushes on the
        # *next* real event line (or EOF) - a trailing Note on provides
        # that here, rather than relying on EOF/close() timing.
        def launch(_command, **kwargs):
            child = self.real_popen(
                [sys.executable, "-u", "-c",
                 "import time; "
                 "print(' 16:0   System exclusive', flush=True); "
                 "print('  F0 7D 01', flush=True); "
                 "print('  02 03 F7', flush=True); "
                 "print(' 16:0   Note on                 0, note 1, velocity 2', flush=True); "
                 "time.sleep(60)"], **kwargs)
            self.children.append(child)
            return child

        monitor = panel.MidiMonitor()
        with mock.patch.object(monitor, "_discover", return_value=("16:0", "connected")), \
                mock.patch.object(panel.subprocess, "Popen", launch):
            monitor.ensure()
            event = monitor.events.get(timeout=2)
            trailing = monitor.events.get(timeout=2)
            monitor.close()
        self.assertIn("System exclusive", event)
        self.assertIn("F0 7D 01", event)
        self.assertIn("02 03 F7", event)
        self.assertIn("Note on", trailing)

    def test_midi_monitor_reuses_one_child_then_reopens_fresh(self):
        launched = []

        def launch(_command, **kwargs):
            child = self.real_popen(
                [sys.executable, "-u", "-c", "import time; time.sleep(60)"], **kwargs)
            launched.append(child)
            self.children.append(child)
            return child

        monitor = panel.MidiMonitor()
        with mock.patch.object(monitor, "_discover", return_value=("16:0", "connected")), \
                mock.patch.object(panel.subprocess, "Popen", launch):
            monitor.ensure()
            monitor.ensure()
            self.assertEqual(len(launched), 1)
            monitor.stop()
            self.assertIsNotNone(launched[0].returncode)
            self.assertEqual(monitor.status(), "closed")
            monitor.ensure()
            self.assertEqual(len(launched), 2)
            self.assertIsNone(launched[1].poll())
            monitor.close()
        self.assertIsNotNone(launched[1].returncode)

    def test_midi_monitor_reports_missing_port_and_tool(self):
        monitor = panel.MidiMonitor()
        with mock.patch.object(panel.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(monitor.ensure(), "unavailable")
        listing = " Port    Client name                      Port name\n 14:0    Midi Through  Midi Through Port-0\n"
        completed = subprocess.CompletedProcess(["aseqdump", "-l"], 0, listing, "")
        with mock.patch.object(panel.subprocess, "run", return_value=completed):
            self.assertEqual(monitor.ensure(), "disconnected")
        monitor.close()

    def test_midi_monitor_interrupts_with_sigint(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        panel.MidiMonitor._interrupt(proc)
        proc.send_signal.assert_called_once_with(panel.signal.SIGINT)
        proc.wait.assert_called_once_with(timeout=1.0)

    def test_midi_subscription_failure_becomes_unavailable(self):
        def launch(_command, **kwargs):
            child = self.real_popen(
                [sys.executable, "-u", "-c",
                 "print('Cannot connect from port'); raise SystemExit(1)"], **kwargs)
            self.children.append(child)
            return child

        monitor = panel.MidiMonitor()
        with mock.patch.object(monitor, "_discover", return_value=("16:0", "connected")), \
                mock.patch.object(panel.subprocess, "Popen", launch):
            monitor.ensure()
            deadline = time.monotonic() + 2
            while monitor.status() == "connected" and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(monitor.status(), "unavailable")
        self.assertTrue(monitor.events.empty())
        monitor.close()


class PdeathsigWrappingTest(unittest.TestCase):
    """_with_pdeathsig()'s own command-wrapping logic, isolated from any
    real setpriv/subprocess - the end-to-end mechanism (does PDEATHSIG
    actually fire on this kernel) is PdeathsigIntegrationTest's job.
    """

    def setUp(self) -> None:
        self.setpriv_guard = mock.patch.object(panel, "_SETPRIV", "/usr/bin/setpriv")
        self.setpriv_guard.start()
        self.addCleanup(self.setpriv_guard.stop)
        self.addCleanup(setattr, panel, "_pdeathsig_warned", False)

    def test_wraps_with_setpriv_when_available(self) -> None:
        self.assertEqual(
            panel._with_pdeathsig(["alsactl", "monitor", "hw:0"]),
            ["/usr/bin/setpriv", "--pdeathsig", "TERM", "--", "alsactl", "monitor", "hw:0"])

    def test_falls_back_and_warns_once_when_unavailable(self) -> None:
        panel._pdeathsig_warned = False
        with mock.patch.object(panel, "_SETPRIV", None):
            with self.assertLogs(panel._LOG, level="DEBUG"):
                self.assertEqual(panel._with_pdeathsig(["aseqdump", "-p", "128:0"]),
                                 ["aseqdump", "-p", "128:0"])
            # Second call with the same unavailable setpriv must not log again.
            with self.assertRaises(AssertionError):
                with self.assertLogs(panel._LOG, level="DEBUG"):
                    panel._with_pdeathsig(["aseqdump", "-p", "128:0"])


@unittest.skipUnless(shutil.which("setpriv"), "setpriv not installed")
class PdeathsigIntegrationTest(unittest.TestCase):
    """Real subprocess-of-subprocess test: does setpriv --pdeathsig TERM
    actually make the kernel kill a grandchild when the middle process
    dies, including via SIGKILL (which no Python-level handler could ever
    catch)? This is the one thing code-reading can't answer - it depends
    on the actual kernel PDEATHSIG behaviour and setpriv's own exec
    semantics, not just on this file's own logic.
    """

    def setUp(self) -> None:
        self.middle = None

    def tearDown(self) -> None:
        if self.middle is not None and self.middle.poll() is None:
            self.middle.kill()
            self.middle.wait()

    def test_grandchild_dies_when_middle_process_is_sigkilled(self) -> None:
        setpriv = shutil.which("setpriv")
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen(['%s', '--pdeathsig', 'TERM', '--', "
            "%r, '-c', \"import time; print('grandchild_ready', flush=True); "
            "time.sleep(60)\"])\n"
            "print('grandchild_pid', p.pid, flush=True)\n"
            "time.sleep(60)\n" % (setpriv, sys.executable))
        self.middle = subprocess.Popen([sys.executable, "-u", "-c", script],
                                       stdout=subprocess.PIPE, text=True)
        # Popen returns once setpriv has been exec'd, which can be before
        # setpriv's own prctl(PR_SET_PDEATHSIG) - killing the middle in that
        # window leaves the signal unarmed. The grandchild's own "ready"
        # line (shared stdout) proves setpriv armed it and exec'd python.
        lines = [self.middle.stdout.readline(), self.middle.stdout.readline()]
        pid_line = next((l for l in lines if l.startswith("grandchild_pid ")), "")
        self.assertTrue(pid_line, lines)
        self.assertIn("grandchild_ready\n", lines)
        grandchild_pid = int(pid_line.split()[1])

        self.middle.kill()  # SIGKILL - the one signal nothing can trap
        self.middle.wait(timeout=2.0)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            self.fail("grandchild %d outlived the SIGKILLed middle process" % grandchild_pid)


class SpawnThreadInvariantTest(unittest.TestCase):
    """_with_pdeathsig()'s own comment (and the one next to each call
    site) says PDEATHSIG only protects a child if the thread that spawned
    it lives as long as the panel does - PanelIO's worker thread, never a
    short-lived one. This proves that invariant dynamically, from the
    real request-dispatch path, rather than only by reading the code.
    """

    def test_change_monitor_spawn_runs_on_the_worker_thread(self) -> None:
        mixer = panel.FakeMixer()
        seen = []
        mixer.ensure_change_monitor = lambda on_change: seen.append(threading.current_thread())
        io = panel.PanelIO(mixer)
        self.addCleanup(io.close)
        io.submit("state")
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(seen, [io.thread])

    def test_midi_monitor_ensure_runs_on_the_worker_thread(self) -> None:
        mixer = panel.FakeMixer()
        seen = []
        mixer.midi_monitor.ensure = lambda: seen.append(threading.current_thread())
        io = panel.PanelIO(mixer)
        self.addCleanup(io.close)
        io.submit("midi_start")
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(seen, [io.thread])


class SignalHandlingTest(unittest.TestCase):
    """End-to-end: a real `phasex64-panel --demo --fake-mixer` subprocess,
    sent a real SIGTERM/SIGHUP, must exit within a bound AND take its
    PDEATHSIG-wrapped stand-in child (spawned the same way the real
    alsactl/aseqdump monitors are) down with it - the exact regression
    this fix round is for.
    """

    def _spawn_panel(self):
        proc = subprocess.Popen(
            [sys.executable, str(PANEL_PATH), "--demo", "--fake-mixer",
             "--test-spawn-stand-in-child"],
            stdout=subprocess.PIPE, text=True)
        line = proc.stdout.readline()
        self.assertTrue(line.startswith("stand_in_pid "), line)
        stand_in_pid = int(line.split()[1])
        return proc, stand_in_pid

    def _assert_gone(self, pid, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.05)
        self.fail("pid %d is still alive after %.1fs" % (pid, timeout))

    def _run(self, sig) -> None:
        proc, stand_in_pid = self._spawn_panel()
        try:
            proc.send_signal(sig)
            proc.wait(timeout=3.0)
            self._assert_gone(stand_in_pid)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            try:
                os.kill(stand_in_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @unittest.skipUnless(os.environ.get("PX64_GUI_TESTS") == "1", "spawns a real Tk root under Xvfb")
    def test_sigterm_exits_and_reaps_stand_in_child(self) -> None:
        self._run(signal.SIGTERM)

    @unittest.skipUnless(os.environ.get("PX64_GUI_TESTS") == "1", "spawns a real Tk root under Xvfb")
    def test_sighup_exits_and_reaps_stand_in_child(self) -> None:
        self._run(signal.SIGHUP)


class SingleInstanceLockTest(unittest.TestCase):
    def test_bypass_always_acquires(self) -> None:
        self.assertEqual(panel.acquire_single_instance_lock(bypass=True), (True, None))

    def test_second_acquire_on_same_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(panel, "_lock_path", return_value=Path(tmp) / "panel.lock"):
                acquired1, file1 = panel.acquire_single_instance_lock(bypass=False)
                self.addCleanup(lambda: file1 and file1.close())
                self.assertTrue(acquired1)
                self.assertIsNotNone(file1)

                acquired2, file2 = panel.acquire_single_instance_lock(bypass=False)
                self.assertFalse(acquired2)
                self.assertIsNone(file2)

    def test_lock_released_when_holder_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(panel, "_lock_path", return_value=Path(tmp) / "panel.lock"):
                acquired1, file1 = panel.acquire_single_instance_lock(bypass=False)
                self.assertTrue(acquired1)
                file1.close()  # releases the flock, same as process exit would

                acquired2, file2 = panel.acquire_single_instance_lock(bypass=False)
                self.addCleanup(lambda: file2 and file2.close())
                self.assertTrue(acquired2)


@unittest.skipUnless(os.environ.get("PX64_GUI_TESTS") == "1", "run explicitly under Xvfb")
class AlreadyRunningDialogTest(unittest.TestCase):
    """show_already_running_dialog() blocks on wait_window() by design
    (it's a modal "OK" dialog) - schedule the click before calling it, the
    same way StudioGUITest.pump() interleaves after()-scheduled work with
    Tk's own event loop, so the test doesn't hang waiting for a real click.
    """

    def test_shows_dialog_and_ok_closes_it(self) -> None:
        root = panel.tk.Tk()
        self.addCleanup(root.destroy)

        def click_ok():
            for widget in root.winfo_children():
                if isinstance(widget, panel.tk.Toplevel):
                    widget.destroy()
                    return
            root.after(20, click_ok)

        root.after(20, click_ok)
        panel.show_already_running_dialog(root)  # returns once the Toplevel above is destroyed
        self.assertEqual(root.winfo_children(), [])


class BackendTest(unittest.TestCase):
    def test_source_mapping_and_display_only_rename(self):
        self.assertEqual(panel.ADC_SOURCE_ITEMS["In 1/2 Capture Source"].index("Sound Module"), 4)
        self.assertEqual(panel.ADC_SOURCE_ITEMS["In 3/4 Capture Source"].index("Sound Module"), 3)
        self.assertEqual(panel.source_label("Sound Module"), "DSP")
        self.assertIn("Sound Module 3/4 Playback Switch", panel.PRESET_NAMES)

    def test_peak_parsing_and_dbfs(self):
        self.assertEqual(panel.parse_peak("0,8388607"), (0, 8388607))
        for value in (None, "", "1", "1,2,3", "-1,0", "8388608,0", "a,b"):
            self.assertIsNone(panel.parse_peak(value), value)
        self.assertEqual(panel.peak_db(0), -60)
        self.assertEqual(panel.peak_db(panel.PEAK_MAX), 0)
        self.assertAlmostEqual(panel.peak_db(panel.PEAK_MAX // 2), -6.0206, places=4)

    def test_midi_counter_and_dynamic_seq_port_parsing(self):
        proc = """PHASE X64 USB MIDI
Output 0
  Tx bytes     : 10538
Input 0
  Rx bytes     : 9912
"""
        self.assertEqual(panel.parse_midi_counters(proc), (9912, 10538))
        self.assertEqual(panel.parse_midi_counters("Output 0\n Tx bytes: 7\n"), (None, 7))
        listing = """ Port    Client name                      Port name
 14:0    Midi Through                     Midi Through Port-0
 16:0    PHASE X64 USB                    PHASE X64 USB MIDI
"""
        self.assertEqual(panel.parse_phase_seq_port(listing), "16:0")
        self.assertIsNone(panel.parse_phase_seq_port(listing.splitlines()[1]))

    def test_midi_event_lines_show_channel_1_to_16(self):
        # aseqdump's own -R/--raw-view default (unchanged by this panel)
        # prints channels 0-15; displayed channel numbers should read
        # 1-16, matching ordinary MIDI convention.
        cases = {
            " 16:0   Note on                 0, note 60, velocity 100":
                " 16:0   Note on                 1, note 60, velocity 100",
            " 16:0   Note off                0, note 60, velocity 0":
                " 16:0   Note off                1, note 60, velocity 0",
            " 16:0   Control change          2, controller 7, value 100":
                " 16:0   Control change          3, controller 7, value 100",
            " 16:0   Pitch bend              15, value 0":
                " 16:0   Pitch bend              16, value 0",
        }
        for raw, expected in cases.items():
            self.assertEqual(panel.format_midi_event_line(raw), expected)

    def test_midi_event_lines_without_a_channel_pass_through_unchanged(self):
        # Clock/Start/Stop/Song position pointer/System exclusive have no
        # channel field at all - must never be misinterpreted as one.
        for line in (
            " 16:0   Clock",
            " 16:0   Start",
            " 16:0   Stop",
            " 16:0   Song position pointer      value 0",
            " 16:0   System exclusive          F0 7D 01 02 03 F7",
        ):
            self.assertEqual(panel.format_midi_event_line(line), line)

    def test_midi_counters_are_read_from_proc_without_pcm_state(self):
        mixer = panel.Mixer("3")
        with tempfile.TemporaryDirectory() as directory:
            card = Path(directory) / "card3"
            card.mkdir()
            (card / "midi0").write_text("Tx bytes: 12\nRx bytes: 34\n")
            (card / "midi1").write_text("Tx bytes: 5\nRx bytes: 6\n")
            with mock.patch.object(panel, "PROC_ASOUND", directory), \
                    mock.patch.object(mixer, "stream_active",
                                      side_effect=AssertionError("PCM state consulted")):
                self.assertEqual(mixer.midi_counters(), (40, 17))
        mixer.midi_monitor.close()

    def test_midi_available_reflects_the_rawmidi_device_not_the_monitor(self):
        # Addendum (2026-10-19, hardware test): the status bar's MIDI
        # field used to read the monitor window's own connection state
        # (misleadingly "closed" whenever that window was closed, even
        # though MIDI itself worked fine). midi_available() checks the
        # rawmidi proc entry's presence instead - true the moment the
        # card is loaded, regardless of whether the monitor has ever
        # been opened or PCM is running.
        mixer = panel.Mixer("3")
        with tempfile.TemporaryDirectory() as directory:
            card = Path(directory) / "card3"
            card.mkdir()
            (card / "midi0").write_text("Tx bytes: 0\nRx bytes: 0\n")
            with mock.patch.object(panel, "PROC_ASOUND", directory), \
                    mock.patch.object(mixer, "stream_active",
                                      side_effect=AssertionError("PCM state consulted")):
                self.assertTrue(mixer.midi_available())
        with tempfile.TemporaryDirectory() as empty_directory:
            with mock.patch.object(panel, "PROC_ASOUND", empty_directory):
                self.assertFalse(mixer.midi_available())
        self.assertIsNone(panel.Mixer(None).midi_available())
        mixer.midi_monitor.close()

    def test_version_at_least(self):
        self.assertTrue(panel.version_at_least((0, 1, 12), (0, 1, 12)))
        self.assertTrue(panel.version_at_least((0, 1, 12, 0), (0, 1, 12)))
        self.assertTrue(panel.version_at_least((0, 1, 13), (0, 1, 12)))
        self.assertTrue(panel.version_at_least((1, 0), (0, 1, 12)))
        self.assertFalse(panel.version_at_least((0, 1, 11, 5), (0, 1, 12)))
        self.assertFalse(panel.version_at_least(None, (0, 1, 12)))

    def test_read_driver_version_parses_sysfs_and_handles_missing_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".version") as f:
            f.write("0.1.12-pre1\n")
            f.flush()
            with mock.patch.object(panel, "DRIVER_VERSION_SYSFS", f.name):
                self.assertEqual(panel.read_driver_version(), (0, 1, 12))
        with tempfile.NamedTemporaryFile("w", suffix=".version") as f:
            f.write("0.1.11.5\n")
            f.flush()
            with mock.patch.object(panel, "DRIVER_VERSION_SYSFS", f.name):
                self.assertEqual(panel.read_driver_version(), (0, 1, 11, 5))
        with mock.patch.object(panel, "DRIVER_VERSION_SYSFS", "/nonexistent/path/for/this/test"):
            self.assertIsNone(panel.read_driver_version())

    def test_status_does_not_consume_peaks(self):
        mixer = panel.Mixer("test")
        names = panel.STATE_NAMES + panel.PEAK_NAMES
        commands = []

        def run(args, input_text=None):
            commands.append(args)
            if args == ["controls"]:
                return "\n".join(f"numid={i},iface=MIXER,name='{name}'" for i, name in enumerate(names))
            name = args[1].removeprefix("name=")
            return f"numid=1,iface=MIXER,name='{name}'\n  : values=0\n"

        with mock.patch.object(mixer, "_run", side_effect=run):
            result = mixer.contents()
        self.assertEqual(set(result), set(panel.STATE_NAMES))
        self.assertFalse(any("Peak" in " ".join(command) for command in commands))
        self.assertFalse(any("contents" in command or "--stdin" in command for command in commands))

    def test_missing_peak_controls_do_no_reads(self):
        mixer = panel.Mixer("test")
        with mock.patch.object(mixer, "_run") as run:
            self.assertEqual(mixer.peaks(), {})
        run.assert_not_called()

    def test_reads_only_existing_peak_controls(self):
        mixer = panel.Mixer("test")
        mixer.available = {panel.PEAK_NAMES[0]}
        output = "numid=20,iface=MIXER,name='In 1/2 Peak'\n  : values=123,456\n"
        with mock.patch.object(mixer, "_run", return_value=output) as run:
            self.assertEqual(mixer.peaks(), {"In 1/2 Peak": (123, 456)})
        run.assert_called_once_with(["cget", "name=In 1/2 Peak"])

    def test_hidden_meter_request_does_not_read(self):
        mixer = panel.FakeMixer(demo=True)
        io = panel.PanelIO(mixer)
        try:
            with mock.patch.object(mixer, "peaks") as peaks:
                io.submit("peaks")
                # ("peaks" dict, midi_counters) - see PanelIO.run()'s own
                # comment for why midi_counters rides this same poll.
                self.assertEqual(io.results.get(timeout=2), ("peaks", ({}, (0, 0)), None))
                peaks.assert_not_called()
        finally:
            io.close()
        self.assertFalse(io.thread.is_alive())

    def test_meter_worker_limits_catch_up_rate(self):
        mixer = panel.FakeMixer(demo=True)
        io = panel.PanelIO(mixer)
        times = []
        try:
            io.visible.set()
            with mock.patch.object(mixer, "peaks", side_effect=lambda *_: times.append(time.monotonic()) or {}):
                for _ in range(5):
                    io.submit("peaks")
                for _ in range(5):
                    io.results.get(timeout=2)
        finally:
            io.close()
        self.assertTrue(all(b - a >= .049 for a, b in zip(times, times[1:])))

    def test_io_reports_write_failure(self):
        mixer = panel.FakeMixer()
        io = panel.PanelIO(mixer)
        try:
            with mock.patch.object(mixer, "set", side_effect=RuntimeError("USB gone")):
                io.submit("write", [(panel.RIAA_NAMES[0], "on")])
                kind, _, error = io.results.get(timeout=2)
            self.assertEqual((kind, error), ("write", "USB gone"))
        finally:
            io.close()

    def test_normal_state_poll_never_starts_midi_monitor(self):
        mixer = panel.FakeMixer()
        io = panel.PanelIO(mixer)
        try:
            with mock.patch.object(mixer.midi_monitor, "ensure",
                                   side_effect=AssertionError("monitor auto-started")) as ensure, \
                    mock.patch.object(mixer, "contents",
                                      side_effect=RuntimeError("controls unavailable")):
                io.submit("state")
                kind, _, error = io.results.get(timeout=2)
            self.assertEqual((kind, error), ("state", "controls unavailable"))
            ensure.assert_not_called()
        finally:
            io.close()

    def test_stream_activity_uses_status_not_prepared_hw_params(self):
        mixer = panel.Mixer("3")
        with mock.patch.object(panel.Path, "read_text", side_effect=["state: PREPARED", "closed"]):
            self.assertFalse(mixer.stream_active())
        with mock.patch.object(panel.Path, "read_text", side_effect=["state: RUNNING", "closed"]):
            self.assertTrue(mixer.stream_active())
        with mock.patch.object(panel.Path, "read_text", side_effect=OSError):
            self.assertIsNone(mixer.stream_active())

    def test_demo_uses_fake_backend_and_does_not_auto_load_skin(self):
        with mock.patch.object(sys, "argv", ["panel", "--demo"]), \
                mock.patch.object(panel, "find_card", side_effect=AssertionError("hardware access")), \
                mock.patch.object(panel, "find_skin") as skin, \
                mock.patch.object(panel.tk, "Tk"), \
                mock.patch.object(panel, "Panel") as ui:
            panel.main()
        self.assertIsInstance(ui.call_args.args[1], panel.FakeMixer)
        self.assertTrue(ui.call_args.args[1].meters_present)
        self.assertIsNone(ui.call_args.args[2])
        skin.assert_not_called()


class PresetTest(unittest.TestCase):
    """Built-in presets are keyed by stable id ("vinyl"/"monitor" -
    panel.BUILTIN_PRESET_IDS), not by their (translatable) displayed
    label - see panel.builtin_preset_label() and Panel.preset_entries()."""

    def test_builtins_complete_and_valid(self):
        for controls in panel.builtin_presets().values():
            panel.validate_preset(controls)
        self.assertEqual(set(panel.builtin_presets()), set(panel.BUILTIN_PRESET_IDS))
        vinyl = panel.builtin_presets()["vinyl"]
        self.assertEqual(vinyl["In 1/2 Capture Source"], 1)
        self.assertTrue(vinyl[panel.RIAA_NAMES[0]])
        self.assertFalse(vinyl["Direct 1/2 Playback Switch"])
        self.assertTrue(vinyl["PCM 1/2 Playback Switch"])

    def test_builtin_labels_are_translatable_and_distinct(self):
        labels = [panel.builtin_preset_label(pid) for pid in panel.BUILTIN_PRESET_IDS]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(all(labels))

    def test_json_round_trip_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = panel.Presets(directory)
            controls = panel.builtin_presets()["vinyl"]
            store.save("Mijn plaat", controls)
            self.assertEqual(store.load("Mijn plaat"), controls)
            controls[panel.RIAA_NAMES[1]] = False
            store.save("Mijn plaat", controls)
            self.assertEqual(store.load("Mijn plaat"), controls)
            self.assertEqual(set(store.builtin_ids()), set(panel.BUILTIN_PRESET_IDS))
            self.assertEqual(set(store.custom_names()), {"Mijn plaat"})
            self.assertFalse(list(Path(directory).glob(".preset-*")))

    def test_invalid_names_and_builtin_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = panel.Presets(directory)
            for name in ("../escape", "", "a/b", "a\\b", "vinyl"):
                with self.assertRaises(ValueError):
                    store.save(name, panel.builtin_presets()["monitor"])

    def test_bad_values_unknown_controls_and_interlock(self):
        baseline = panel.builtin_presets()["vinyl"]
        for change in ({"Clock Source": 1}, {panel.RIAA_NAMES[0]: "on"},
                       {"In 1/2 Capture Source": -1}, {"In 3/4 Capture Source": True},
                       {"Direct 1/2 Mono Playback Switch": True}):
            with self.assertRaises(ValueError):
                panel.preset_writes(dict(baseline, **change))
        with self.assertRaises(ValueError):
            panel.validate_preset({})

    def test_schema_rejected_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            store = panel.Presets(directory)
            # Test fixture generation, never a user preset.
            store.path("bad").write_text(json.dumps({"schema": 99, "controls": {}}))
            with self.assertRaises(ValueError):
                store.load("bad")

    def test_prerequisites_before_direct_mono(self):
        controls = panel.builtin_presets()["monitor"]
        controls["In 1/2 Capture Source"] = 0
        controls["Direct 1/2 Mono Playback Switch"] = True
        mixer = panel.FakeMixer()
        for name, value in panel.preset_writes(controls):
            mixer.set(name, value)
        self.assertEqual(panel.preset_from_values(mixer.contents()), controls)


@unittest.skipUnless(os.environ.get("PX64_GUI_TESTS") == "1", "run explicitly under Xvfb")
class StudioGUITest(unittest.TestCase):
    def setUp(self):
        # Any accidental real ALSA subprocess fails immediately, even on this
        # machine with a real card loaded. Demo must remain entirely offline.
        self.guard = mock.patch.object(panel.subprocess, "run", side_effect=AssertionError("hardware access"))
        self.guard.start()
        # self.ui.live_apply_capable must not depend on whatever driver
        # happens to be loaded on the machine running this suite - pin it
        # to "no live apply" (None, e.g. module not loaded) by default so
        # existing pre-v0.1.12 wording assertions stay deterministic;
        # version-gating itself is tested separately against its own
        # Panel instances with an explicit mock (see
        # test_hint_and_notice_text_follow_driver_version).
        self.version_guard = mock.patch.object(panel, "read_driver_version", return_value=None)
        self.version_guard.start()
        self.root = panel.tk.Tk()
        self.mixer = panel.FakeMixer(demo=True)
        self.ui = panel.Panel(self.root, self.mixer, None)
        self.pump(.2)

    def tearDown(self):
        self.ui.close()
        self.assertFalse(self.ui.io.thread.is_alive())
        self.version_guard.stop()
        self.guard.stop()
        # Addendum: root.destroy() doesn't immediately collect this test's
        # now-unreachable tk.Variable objects (self.enum_vars/switch_vars) -
        # CPython's generational GC can sweep them up much later, on
        # *whatever thread* happens to be allocating at the time. If
        # that's a later test's background PanelIO thread, tk.Variable.
        # __del__() calls into Tcl from the wrong thread
        # ("RuntimeError: main thread is not in main loop") and that
        # thread never finishes - thread.join() times out in a *later,
        # unrelated* test, intermittently, and the exact test it hits
        # depends on GC timing, not on that test's own logic (confirmed:
        # reproducible only at full-suite scale - more Tk roots created/
        # destroyed means more accumulated garbage to eventually trigger
        # this - never in isolation or small repeats of one test).
        # Collecting here, on the main thread, right after this test's
        # own root is destroyed, keeps that garbage from ever accumulating
        # into a later test's background thread's path.
        gc.collect()

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)

    def test_riaa_enable_and_source_mapping(self):
        self.assertEqual(self.ui.control_widgets[panel.RIAA_NAMES[0]][0]["state"], "normal")
        self.assertEqual(self.ui.control_widgets[panel.RIAA_NAMES[1]][0]["state"], "disabled")
        self.ui.control_widgets["In 3/4 Capture Source"][0].invoke()
        self.pump(.3)
        self.assertEqual(self.mixer.values["In 3/4 Capture Source"], "0")
        self.assertEqual(self.ui.control_widgets[panel.RIAA_NAMES[1]][0]["state"], "normal")

    def test_load_monitor_preset(self):
        # Selection is by index (self.ui.preset_ids), not by the displayed
        # (and translatable) label - see Panel.preset_entries().
        self.ui.preset_choice.current(self.ui.preset_ids.index("monitor"))
        self.ui.load_preset()
        self.pump(.3)
        self.assertEqual(self.mixer.values["Direct 1/2 Playback Switch"], "on")
        self.assertIn("restart", self.ui.notice["text"])

    def test_hint_and_notice_text_follow_driver_version(self):
        # Addendum (2026-10-16, v0.1.12-plan.md section 11): the standing
        # hint label and the per-write notice both describe codec-apply
        # behavior differently depending on whether the *loaded* driver
        # actually has live apply (>=0.1.12) - not fixed wording, and not
        # dependent on whatever happens to be loaded on the machine
        # running this suite (see setUp's version_guard).
        cases = (
            (None, False),          # module not loaded (e.g. --demo)
            ((0, 1, 11, 5), False),
            ((0, 1, 12), True),
            ((0, 1, 12, 1), True),
        )
        for version, expect_live in cases:
            with mock.patch.object(panel, "read_driver_version", return_value=version):
                root = panel.tk.Tk()
                mixer = panel.FakeMixer(demo=True)
                ui = panel.Panel(root, mixer, None)
                try:
                    self.assertEqual(ui.live_apply_capable, expect_live, version)
                    if expect_live:
                        self.assertIn("live", ui.hint["text"])
                        self.assertNotIn("v0.1.12", ui.hint["text"])
                    else:
                        self.assertIn("v0.1.12", ui.hint["text"])

                    ui.control_widgets["In 3/4 Capture Source"][0].invoke()
                    deadline = time.monotonic() + .3
                    while time.monotonic() < deadline:
                        root.update()
                        time.sleep(.005)
                    if expect_live:
                        self.assertEqual(ui.notice["text"], "Applied live.")
                    else:
                        self.assertIn("restart", ui.notice["text"])
                finally:
                    ui.close()

    def test_save_preset_selects_it_by_id_afterwards(self):
        with mock.patch.object(panel.simpledialog, "askstring", return_value="My custom"), \
                tempfile.TemporaryDirectory() as directory:
            self.ui.presets = panel.Presets(directory)
            self.ui.save_preset()
            self.pump(.2)
            self.assertIn("My custom", self.ui.preset_ids)
            self.assertEqual(self.ui.preset_choice.get(), "My custom")

    def test_hidden_and_obscured_windows_do_not_poll_peaks(self):
        with mock.patch.object(self.mixer, "peaks", wraps=self.mixer.peaks) as peaks:
            self.pump(.2)
            self.assertGreater(peaks.call_count, 0)
            self.root.withdraw()
            self.pump(.1)
            count = peaks.call_count
            self.pump(.2)
            self.assertEqual(peaks.call_count, count)
            self.root.deiconify()
            self.pump(.2)
            self.assertGreater(peaks.call_count, count)
            self.ui.visibility(types.SimpleNamespace(widget=self.root, type="Visibility", state="VisibilityFullyObscured"))
            self.pump(.1)
            count = peaks.call_count
            self.pump(.2)
            self.assertEqual(peaks.call_count, count)

    def test_midi_status_and_leds_are_independent_of_pcm(self):
        # Establish a byte-counter baseline while PCM is idle, then advance
        # Rx only. Availability comes from the rawmidi device and activity
        # comes from /proc rawmidi counters; neither may consult
        # stream_active(). LEDs are driven by update_midi_leds() directly
        # now (the fast ~50ms "peaks" poll - see PanelIO.run()'s own
        # comment), not through render_state()'s tuple (the slow ~1s
        # "state" poll) any more.
        values = self.mixer.contents()
        self.ui.midi_last_counters = [None, None]
        self.ui.midi_led_until = [0.0, 0.0]
        self.ui.render_state((values, 48000, False, "fake", True))
        self.ui.update_midi_leds((10, 20))
        self.assertIn("Stream: idle", self.ui.status["text"])
        self.assertIn("MIDI: available", self.ui.status["text"])
        self.assertNotEqual(self.ui.midi_leds[0]["fg"], panel.ACCENT)

        self.ui.update_midi_leds((11, 20))
        self.assertEqual(self.ui.midi_leds[0]["fg"], panel.ACCENT)
        self.assertNotEqual(self.ui.midi_leds[1]["fg"], panel.ACCENT)
        self.ui.update_midi_leds(now=self.ui.midi_led_until[0] + .01)
        self.assertNotEqual(self.ui.midi_leds[0]["fg"], panel.ACCENT)

        # Changing only PCM state cannot light either MIDI LED or change the
        # MIDI availability field.
        self.ui.midi_led_until = [0.0, 0.0]
        self.ui.render_state((values, 48000, True, "fake", True))
        self.ui.update_midi_leds((11, 20))  # same counters as before - no new activity
        self.assertIn("MIDI: available", self.ui.status["text"])
        self.assertNotEqual(self.ui.midi_leds[0]["fg"], panel.ACCENT)
        self.assertNotEqual(self.ui.midi_leds[1]["fg"], panel.ACCENT)

    def test_midi_status_bar_is_independent_of_monitor_window(self):
        # Addendum (2026-10-19, hardware test): this field used to read
        # the monitor *window's* connection state, so it misleadingly
        # read "closed" whenever the window was closed even though MIDI
        # itself worked fine (the LEDs blinked, the port was there). It
        # must track the rawmidi device's own presence instead,
        # regardless of whether the window has ever been opened.
        values = self.mixer.contents()
        self.ui.render_state((values, 48000, True, "fake", True))
        self.assertIn("MIDI: available", self.ui.status["text"])
        self.assertIsNone(self.ui.midi_window)

        self.ui.render_state((values, 48000, True, "fake", False))
        self.assertIn("MIDI: unavailable", self.ui.status["text"])

        self.ui.open_midi_monitor()
        self.pump(.3)
        self.ui.render_state((values, 48000, True, "fake", True))
        self.assertIn("MIDI: available", self.ui.status["text"],
                     "status bar must not read the monitor window's own state")
        self.ui.close_midi_monitor()
        self.pump(.3)

    def test_midi_monitor_batches_and_bounds_history(self):
        # Addendum: drain_midi_events() early-returns while no monitor
        # Toplevel is open (self.midi_monitor_widget is None) - the
        # agreed design has no embedded monitor, so the window has to be
        # opened first via the real code path (FakeMidiMonitor, no real
        # subprocess) for there to be anything to drain into. An earlier
        # version of this test pushed events and looped
        # `while not events.empty(): drain_midi_events()` without ever
        # opening the window - drain_midi_events() never removed
        # anything, so that loop never terminated (100% CPU, no
        # progress) - caught by running the suite, not by inspection.
        self.ui.open_midi_monitor()
        self.pump(.3)
        self.assertIsNotNone(self.ui.midi_monitor_widget)

        for number in range(panel.MIDI_MONITOR_MAX_LINES + 20):
            self.mixer.midi_monitor.events.put_nowait(f"16:0 Note on {number}")
        # Bounded by construction, not by queue-empty: batch size times
        # enough iterations to drain everything, plus margin - never an
        # unbounded wait on a condition drain_midi_events() might not
        # satisfy.
        iterations = (panel.MIDI_MONITOR_MAX_LINES + 20) // panel.MIDI_MONITOR_BATCH_SIZE + 2
        for _ in range(iterations):
            self.ui.drain_midi_events()
        self.assertEqual(len(self.ui.midi_monitor_lines), panel.MIDI_MONITOR_MAX_LINES)
        text = self.ui.midi_monitor_widget.get("1.0", "end-1c")
        self.assertNotIn("Note on 0\n", text)
        self.assertIn("Note on 219", text)

        self.ui.close_midi_monitor()
        self.pump(.3)
        self.assertIsNone(self.ui.midi_window)

    def test_midi_monitor_hint_warns_when_no_stream_is_running(self):
        # Addendum (2026-10-19): informational only, per analysis/NOTES.md
        # "Hardware device-gate result" - must never gate the LEDs/button/
        # window (test_midi_status_and_leds_are_independent_of_pcm already
        # covers that), just set the right expectation in the monitor's
        # own hint label.
        self.ui.open_midi_monitor()
        self.pump(.3)
        values = self.mixer.contents()

        self.ui.render_state((values, 48000, True, "fake", True))
        self.assertNotEqual(self.ui.midi_stream_hint["fg"], panel.AMBER)
        self.assertNotIn("No audio stream", self.ui.midi_stream_hint["text"])

        self.ui.render_state((values, None, False, "fake", True))
        self.assertEqual(self.ui.midi_stream_hint["fg"], panel.AMBER)
        self.assertIn("No audio stream", self.ui.midi_stream_hint["text"])

        self.ui.close_midi_monitor()
        self.pump(.3)

    def test_midi_monitor_filters_clock_by_default_with_a_toggle(self):
        # A running clock alone is 48 messages/second - filtered out by
        # default; the checkbox re-enables it for new messages, not
        # retroactively for history already drained.
        self.ui.open_midi_monitor()
        self.pump(.3)

        self.mixer.midi_monitor.events.put_nowait(" 16:0   Clock")
        self.mixer.midi_monitor.events.put_nowait(" 16:0   Active Sensing")
        self.mixer.midi_monitor.events.put_nowait(" 16:0   Note on                 1, note 60, velocity 100")
        self.ui.drain_midi_events()
        self.assertEqual(self.ui.midi_monitor_lines, [
            " 16:0   Note on                 1, note 60, velocity 100"])

        self.ui.midi_show_clock_var.set(True)
        self.mixer.midi_monitor.events.put_nowait(" 16:0   Clock")
        self.ui.drain_midi_events()
        self.assertEqual(self.ui.midi_monitor_lines, [
            " 16:0   Note on                 1, note 60, velocity 100",
            " 16:0   Clock"])

        self.ui.close_midi_monitor()
        self.pump(.3)

    def test_midi_monitor_filters_system_events_by_default_with_a_toggle(self):
        # Addendum (2026-10-19, hardware test): other ALSA clients'
        # connect/disconnect traffic (source client 0) leaked into the
        # monitor - "0:1  Port subscribed  143:0 -> 128:0" in the
        # original report. Separate toggle from Clock/Active Sensing -
        # see _MIDI_SYSTEM_EVENT_RE's own comment for why.
        self.ui.open_midi_monitor()
        self.pump(.3)

        self.mixer.midi_monitor.events.put_nowait(" 0:1   Port subscribed       143:0 -> 128:0")
        self.mixer.midi_monitor.events.put_nowait(" 16:0  Note on                 1, note 60, velocity 100")
        self.ui.drain_midi_events()
        self.assertEqual(self.ui.midi_monitor_lines, [
            " 16:0  Note on                 1, note 60, velocity 100"])

        self.ui.midi_show_system_var.set(True)
        self.mixer.midi_monitor.events.put_nowait(" 0:1   Port subscribed       143:0 -> 128:0")
        self.ui.drain_midi_events()
        self.assertEqual(self.ui.midi_monitor_lines, [
            " 16:0  Note on                 1, note 60, velocity 100",
            " 0:1   Port subscribed       143:0 -> 128:0"])

        self.ui.close_midi_monitor()
        self.pump(.3)

    def test_midi_monitor_wraps_long_lines_instead_of_truncating(self):
        # Addendum (2026-10-19, hardware test): wrap="none" truncated an
        # 18-byte SysEx dump at the window edge with no horizontal
        # scrollbar. The fix is wrap="word", not a layout change that
        # needs pixel measurement to verify - what matters is that the
        # full text is still *in* the widget (Text.get() returns
        # everything regardless of how it's visually wrapped), and that
        # the widget's own wrap option is actually "word".
        self.ui.open_midi_monitor()
        self.pump(.3)
        self.assertEqual(str(self.ui.midi_monitor_widget.cget("wrap")), "word")

        long_sysex = " 16:0  System exclusive          " + " ".join(f"{b:02X}" for b in range(64))
        self.mixer.midi_monitor.events.put_nowait(long_sysex)
        self.ui.drain_midi_events()
        text = self.ui.midi_monitor_widget.get("1.0", "end-1c")
        self.assertIn(long_sysex, text)

        self.ui.close_midi_monitor()
        self.pump(.3)

    def test_meter_missing_message_and_demo_peaks(self):
        self.mixer.signal = True
        self.pump(.2)
        self.assertTrue(any(level > -60 for meter in self.ui.meters.values() for level in meter.levels))
        self.mixer.meters_present = False
        self.pump(.2)
        for meter in self.ui.meters.values():
            self.assertIn("needs driver ≥ 0.1.11.5", [meter.itemcget(i, "text") for i in meter.find_all()
                                                  if meter.type(i) == "text"])

    def test_meter_readout_holds_peak_then_falls_back(self):
        # Real hardware, 2026-10-16: the readout tracked the raw per-poll
        # peak; on sustained/compressed music that can sit on the same
        # ceiling for minutes and reads as a stuck display rather than a
        # meter. It's now an explicit hold: jump up immediately on a new,
        # higher peak; hold for HOLD_SECONDS even if the live peak drops;
        # then re-latch to whatever is live. A click forces an early
        # re-latch (see _reset_hold).
        meter = panel.PeakMeter(self.root)

        def readout_text():
            return meter.itemcget(meter.find_withtag("readout")[0], "text")

        try:
            loud = int(panel.PEAK_MAX * 10 ** (-6 / 20))
            quiet = int(panel.PEAK_MAX * 10 ** (-40 / 20))

            meter.draw((loud, loud))
            self.assertIn("-6.0 dBFS", readout_text())

            meter.draw((quiet, quiet))  # lower peak, still within the hold window
            self.assertIn("-6.0 dBFS", readout_text())

            meter.hold_started -= panel.PeakMeter.HOLD_SECONDS + 1  # simulate hold expiry
            meter.draw((quiet, quiet))
            self.assertIn("-40.0 dBFS", readout_text())

            meter.draw((loud, loud))
            self.assertIn("-6.0 dBFS", readout_text())
            meter._reset_hold()
            meter.draw((quiet, quiet))
            self.assertIn("-40.0 dBFS", readout_text())
        finally:
            meter.destroy()

    def test_meter_dict_keys_are_untranslated_control_names(self):
        # Real hardware bug, 2026-10-16: self.meters used to be keyed by
        # the *translated* column title (title + " Peak"), not the real
        # ALSA control name. In English this is a no-op (titles match
        # PEAK_NAMES by coincidence) - every existing meter test here
        # runs in whatever LANG the test process happens to use, almost
        # always English, which is exactly why this wasn't caught before
        # (the real bug report came from a human looking at the Dutch
        # panel on real hardware, not from this suite). So: don't rely on
        # ambient LANG - mock a translation that reproduces the actual
        # nl.po shape that exposed it (translates "Out 1/2"/"Out 3/4",
        # leaves "In 1/2"/"In 3/4" unchanged), which fails against the old
        # buggy code and passes against the fix regardless of which
        # locale this suite is actually run under.
        translated = {"Out 1/2": "Uit 1/2", "Out 3/4": "Uit 3/4"}
        with mock.patch.object(panel, "_", side_effect=lambda s: translated.get(s, s)):
            root = panel.tk.Tk()
            mixer = panel.FakeMixer(demo=True)
            ui = panel.Panel(root, mixer, None)
            try:
                self.assertEqual(set(ui.meters), set(panel.PEAK_NAMES))
            finally:
                ui.close()

    def test_status_bar_merges_rate_into_stream_field(self):
        # Fix: panel hardware review 2026-10-16 - "idle"/"Stream: no" used
        # to be two separate fields saying the same thing; now one field,
        # "Stream: idle" or "Stream: running 48 kHz".
        self.pump(.2)
        self.assertIn("Stream: idle", self.ui.status["text"])
        self.mixer.signal = True
        self.pump(1.3)
        self.assertIn("Stream: running 48 kHz", self.ui.status["text"])

    def test_every_interactive_widget_has_a_tooltip(self):
        # Fix: panel hardware review 2026-10-16, item 8 - every interactive
        # control in the native/plain UI gets a Tooltip (Tk has none
        # built-in). Walk the whole widget tree and check coverage
        # directly, rather than re-asserting each call site individually.
        interactive = (panel.tk.Button, panel.tk.Checkbutton, panel.tk.Radiobutton,
                       panel.ttk.Combobox, panel.PeakMeter)

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        widgets = [w for w in walk(self.root) if isinstance(w, interactive)]
        self.assertGreater(len(widgets), 10)  # sanity: the walk actually found controls
        missing = [str(w) for w in widgets if getattr(w, "_tooltip", None) is None]
        self.assertEqual(missing, [])

    def test_direct_mono_tooltip_explains_why_disabled(self):
        tip = self.ui.direct_mono_tooltip
        text_disabled = tip()
        self.assertIn("needs", text_disabled)
        self.mixer.set("In 1/2 Capture Source", "0")
        self.mixer.set("Direct 1/2 Playback Switch", "on")
        self.pump(1.3)
        text_enabled = tip()
        self.assertNotIn("needs", text_enabled)

    def test_loopback_indicator_follows_capture_source(self):
        # Addendum (2026-10-19): the live "you're recording the other
        # pair's output" indicator must reflect the *current* Capture
        # Source on the same poll cycle as everything else (render_
        # controls()'s ENUM_BUTTONS loop) - no separate timer. Exercises
        # both pairs independently and both directions (selecting the
        # Loopback item, then switching away from it), driven via
        # self.mixer.set() (an external change, e.g. amixer from another
        # process) rather than a panel click, since that's the actual
        # live-apply/refresh path being tested here, not the write path.
        in12 = "In 1/2 Capture Source"
        in34 = "In 3/4 Capture Source"
        loopback_12 = str(panel.ADC_SOURCE_ITEMS[in12].index("Loopback 3/4"))
        loopback_34 = str(panel.ADC_SOURCE_ITEMS[in34].index("Loopback 1/2"))
        aux_12 = str(panel.ADC_SOURCE_ITEMS[in12].index("Aux"))

        self.assertEqual(self.ui.loopback_labels[in12]["text"], "")
        self.assertEqual(self.ui.loopback_labels[in34]["text"], "")

        self.mixer.set(in12, loopback_12)
        self.pump(1.3)
        self.assertEqual(self.ui.loopback_labels[in12]["text"], panel.LOOPBACK_STATUS[in12])
        self.assertEqual(self.ui.loopback_labels[in34]["text"], "", "other pair unaffected")

        self.mixer.set(in34, loopback_34)
        self.pump(1.3)
        self.assertEqual(self.ui.loopback_labels[in34]["text"], panel.LOOPBACK_STATUS[in34])

        self.mixer.set(in12, aux_12)
        self.pump(1.3)
        self.assertEqual(self.ui.loopback_labels[in12]["text"], "", "clears when switched away")
        self.assertEqual(self.ui.loopback_labels[in34]["text"], panel.LOOPBACK_STATUS[in34],
                         "still set - independent of the other pair")

    def test_loopback_indicator_tooltip_warns_about_feedback_loop(self):
        # The live indicator's own tooltip must spell out the
        # feedback-loop risk too, not just ITEM_TOOLTIPS (reachable only
        # by hovering the source item *before* selecting it). tip.text is
        # a callable (see loopback_tooltip()) - this exercises its
        # non-feedback-loop branch; the feedback-loop branch has its own
        # test below.
        for name in panel.LOOPBACK_ITEM:
            label = self.ui.loopback_labels[name]
            tip = getattr(label, "_tooltip", None)
            self.assertIsNotNone(tip, name)
            self.assertTrue(callable(tip.text), name)
            self.assertIn("feedback loop", tip.text())

    def test_item_tooltips_warn_about_feedback_loop(self):
        # Same warning reachable before the source is even selected, on
        # the enum radio buttons themselves (see ITEM_TOOLTIPS).
        self.assertIn("feedback loop", panel.ITEM_TOOLTIPS["Loopback 3/4"])
        self.assertIn("feedback loop", panel.ITEM_TOOLTIPS["Loopback 1/2"])

    def test_capture_56_label_present_with_tooltip(self):
        # No ALSA control of its own for capture 5/6 - this static label
        # (near the Out 1/2 meter) is the only place the panel explains
        # it exists at all.
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        labels = [w for w in walk(self.root)
                  if isinstance(w, panel.tk.Label) and w.cget("text") == panel.CAPTURE_56_TEXT]
        self.assertEqual(len(labels), 1)
        tip = getattr(labels[0], "_tooltip", None)
        self.assertIsNotNone(tip)
        self.assertIn("Out 1/2", tip.text if not callable(tip.text) else tip.text())

    def test_meter_rows_align_across_columns(self):
        # Addendum (2026-10-19): the Out 1/2 card's extra capture-5/6
        # note must not push its meter row lower than the other three
        # columns' - each column is a separate Frame with its own
        # independent grid (see NOTE_ROW_MINSIZE's own comment), so this
        # only holds if that note's row is reserved identically on all
        # four, not just populated on one.
        self.root.update_idletasks()
        ys = {name: m.winfo_rooty() for name, m in self.ui.meters.items()}
        self.assertEqual(len(set(ys.values())), 1, ys)

    def test_feedback_loop_detection_is_a_pure_function_of_values(self):
        # feedback_loop_active() itself, independent of any live Panel -
        # the four conditions, and that any single one missing is enough
        # to clear it.
        in12, in34 = "In 1/2 Capture Source", "In 3/4 Capture Source"
        loop = {
            in12: str(panel.ADC_SOURCE_ITEMS[in12].index("Loopback 3/4")),
            in34: str(panel.ADC_SOURCE_ITEMS[in34].index("Loopback 1/2")),
            "Direct 1/2 Playback Switch": "on",
            "Direct 3/4 Playback Switch": "on",
        }
        self.assertTrue(panel.feedback_loop_active(dict(loop)))
        for key, off in ((in12, "0"), (in34, "0"),
                         ("Direct 1/2 Playback Switch", "off"),
                         ("Direct 3/4 Playback Switch", "off")):
            partial = dict(loop)
            partial[key] = off
            self.assertFalse(panel.feedback_loop_active(partial), key)

    def test_feedback_loop_warning_shows_on_both_indicators_and_clears(self):
        # Integration: driving the Panel's own four controls through the
        # same external-change path as test_loopback_indicator_follows_
        # capture_source, checking the live indicators switch to the
        # warning state together and clear the moment any one condition
        # is removed - same poll cycle, no new timer.
        in12, in34 = "In 1/2 Capture Source", "In 3/4 Capture Source"
        d12, d34 = "Direct 1/2 Playback Switch", "Direct 3/4 Playback Switch"
        self.mixer.set(in12, str(panel.ADC_SOURCE_ITEMS[in12].index("Loopback 3/4")))
        self.mixer.set(in34, str(panel.ADC_SOURCE_ITEMS[in34].index("Loopback 1/2")))
        self.mixer.set(d12, "on")
        self.mixer.set(d34, "on")
        self.pump(1.3)

        for name in (in12, in34):
            label = self.ui.loopback_labels[name]
            self.assertEqual(label["text"], panel.FEEDBACK_LOOP_TEXT, name)
            self.assertEqual(label["fg"], panel.AMBER, name)
            self.assertIn("-68.7", label._tooltip.text())

        self.mixer.set(d34, "off")
        self.pump(1.3)
        for name in (in12, in34):
            label = self.ui.loopback_labels[name]
            self.assertNotEqual(label["text"], panel.FEEDBACK_LOOP_TEXT, name)
            self.assertEqual(label["fg"], panel.ACCENT, name)

    def test_about_dialog_has_a_reasonable_minimum_width(self):
        # about() calls wait_window() internally (modal, same contract as
        # the messagebox.showinfo() it replaces) - it blocks until the
        # dialog is destroyed, so the destroy has to be scheduled via
        # after() *before* calling it; wait_window() runs a nested copy
        # of the same Tk event loop, which still services that callback.
        captured = {}

        def grab_and_destroy():
            tops = [w for w in self.root.winfo_children() if isinstance(w, panel.tk.Toplevel)]
            self.assertEqual(len(tops), 1)
            top = tops[0]
            captured["title"] = top.title()
            captured["width"] = top.winfo_width()
            top.destroy()

        self.root.after(100, grab_and_destroy)
        self.ui.about()
        self.assertEqual(captured.get("title"), "About PHASE X64")
        self.assertGreaterEqual(captured.get("width", 0), panel.ABOUT_MIN_WIDTH)


@unittest.skipUnless(os.environ.get("PX64_GUI_TESTS") == "1", "run explicitly under Xvfb")
class SkinnedLoopbackIndicatorTest(unittest.TestCase):
    """Addendum (2026-10-19): build_skin()'s branch of Panel.__init__ has
    no existing test coverage at all - every test above passes skin=None
    (plain mode). Pre-existing gap, not introduced by this change, but
    the loopback/capture-56 widgets added to that branch (phasex64-panel's
    __init__, right after build_skin()) need at least a smoke test, since
    build_plain()'s equivalent is already covered by StudioGUITest. A
    synthetic minimal skin (one blank PNG, a script.txt with just a
    [MAIN]/BACKGROUND line, no buttons) is enough to exercise
    build_skin() without needing a real vendor skin asset."""

    def setUp(self):
        self.guard = mock.patch.object(panel.subprocess, "run", side_effect=AssertionError("hardware access"))
        self.guard.start()
        self.version_guard = mock.patch.object(panel, "read_driver_version", return_value=None)
        self.version_guard.start()
        self.skin_dir_ctx = tempfile.TemporaryDirectory()
        skin_dir = self.skin_dir_ctx.name
        with open(os.path.join(skin_dir, "script.txt"), "w", encoding="latin-1") as f:
            f.write("[MAIN]\nBACKGROUND=bg.png\n")
        # Tk() must exist before PhotoImage() - it needs a default root to
        # attach to (found the hard way: "Too early to create image: no
        # default root window").
        self.root = panel.tk.Tk()
        img = panel.tk.PhotoImage(master=self.root, width=10, height=10)
        img.write(os.path.join(skin_dir, "bg.png"), format="png")
        self.mixer = panel.FakeMixer(demo=True)
        self.ui = panel.Panel(self.root, self.mixer, skin_dir)
        self.pump(.2)

    def tearDown(self):
        self.ui.close()
        self.version_guard.stop()
        self.guard.stop()
        self.skin_dir_ctx.cleanup()
        gc.collect()  # see StudioGUITest.tearDown's own comment

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)

    def test_loopback_and_capture_56_widgets_exist_under_a_skin(self):
        self.assertIn("In 1/2 Capture Source", self.ui.loopback_labels)
        self.assertIn("In 3/4 Capture Source", self.ui.loopback_labels)

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        labels = [w for w in walk(self.root)
                  if isinstance(w, panel.tk.Label) and w.cget("text") == panel.CAPTURE_56_TEXT]
        self.assertEqual(len(labels), 1)

    def test_loopback_indicator_updates_under_a_skin(self):
        name = "In 1/2 Capture Source"
        loopback = str(panel.ADC_SOURCE_ITEMS[name].index("Loopback 3/4"))
        self.mixer.set(name, loopback)
        self.pump(1.3)
        self.assertEqual(self.ui.loopback_labels[name]["text"], panel.LOOPBACK_STATUS[name])


if __name__ == "__main__":
    unittest.main(verbosity=2)
