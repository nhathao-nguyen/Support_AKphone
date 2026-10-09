"""Root wrapper package forwarding to src/akphone_dispatcher."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_PKG = os.path.join(_SRC, "akphone_dispatcher")
if os.path.isdir(_PKG) and _PKG not in __path__:
    __path__.append(_PKG)

__version__ = "2.0.0"
