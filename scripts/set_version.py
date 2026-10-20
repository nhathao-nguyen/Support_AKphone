"""Set __version__ in src/akphone_dispatcher/__init__.py: python scripts/set_version.py 2.1.0"""

from pathlib import Path
import re
import sys

INIT = Path(__file__).resolve().parent.parent / "src" / "akphone_dispatcher" / "__init__.py"


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"v?\d+(\.\d+){1,3}", sys.argv[1]):
        print("Usage: python scripts/set_version.py 2.1.0")
        return 1
    version = sys.argv[1].lstrip("v")
    text = INIT.read_text(encoding="utf-8")
    new, count = re.subn(r'__version__ = "[^"]*"', f'__version__ = "{version}"', text)
    if count != 1:
        print(f"__version__ not found in {INIT}")
        return 1
    INIT.write_text(new, encoding="utf-8", newline="\n")
    print(f"__version__ = {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
