"""CLI entrypoint for running the package directly via `python -m akphone_dispatcher`."""

import sys
from akphone_dispatcher.gui.app import AKPhoneApp


def main():
    app = AKPhoneApp()
    app.mainloop()


if __name__ == "__main__":
    main()
