#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Developer-only screenshot helper. Run under Xvfb; requires ImageMagick.

These tools are NOT runtime dependencies of the panel. Always uses --demo.
Usage: xvfb-run -a python3 demo_screenshots.py /tmp/panel-shots
"""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys

loader = importlib.machinery.SourceFileLoader("panel", str(Path(__file__).with_name("phasex64-panel")))
spec = importlib.util.spec_from_loader(loader.name, loader)
panel = importlib.util.module_from_spec(spec)
loader.exec_module(panel)
destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
original = panel.Panel


class ScreenshotPanel(original):
    def __init__(self, root, mixer, skin):
        root.tk.call("tk", "scaling", 1.25)
        super().__init__(root, mixer, skin)
        root.after(1500, self.capture_normal)

    def capture(self, name):
        self.root.update_idletasks()
        subprocess.run(["import", "-window", str(self.root.winfo_id()),
                        str(destination / name)], check=True, timeout=10)

    def capture_normal(self):
        self.capture("demo-normal.png")
        self.demo_signal.set(True)
        self.toggle_demo()
        self.root.after(1700, self.capture_active)

    def capture_active(self):
        self.capture("demo-active.png")
        self.mixer.meters_present = False
        self.root.after(300, self.capture_missing)

    def capture_missing(self):
        self.capture("demo-missing-controls.png")
        self.close()


panel.Panel = ScreenshotPanel
sys.argv = ["phasex64-panel", "--demo"]
panel.main()
