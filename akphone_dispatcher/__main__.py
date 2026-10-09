"""Root CLI entrypoint executing `python -m akphone_dispatcher` seamlessly."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from akphone_dispatcher.gui.app import AKPhoneApp


def main():
    app = AKPhoneApp()
    app.mainloop()


if __name__ == "__main__":
    main()
