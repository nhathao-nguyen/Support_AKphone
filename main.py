#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Root application launcher for AKPhone Smart Dispatcher & Live Feeder."""

import os
import sys

# Add src to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from akphone_dispatcher.gui.app import AKPhoneApp


def main():
    app = AKPhoneApp()
    app.mainloop()


if __name__ == "__main__":
    main()
