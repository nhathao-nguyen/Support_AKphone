#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AKPhone Smart Dispatcher & Live Feeder System (v2.0 Modular).

This launcher maintains 100% backward compatibility for existing batch scripts
while redirecting execution to the standard `akphone_dispatcher` package architecture.
"""

import os
import sys

# Add src to sys.path
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(SRC_DIR) and SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from akphone_dispatcher.gui.app import AKPhoneApp


def main():
    app = AKPhoneApp()
    app.mainloop()


if __name__ == "__main__":
    main()
