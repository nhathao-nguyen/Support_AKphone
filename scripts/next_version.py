"""Version for the next release, printed as GitHub Actions outputs (run in CI on main).

Next = patch + 1 of the newest v* tag, unless __version__ in the code was raised above it
(set by hand with scripts/set_version.py for a bigger step, e.g. 2.1.0). Also writes the
release notes (commit subjects since the previous tag) to release_notes.md.
"""

from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Tuple

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "akphone_dispatcher" / "__init__.py"


def parse(text: str) -> Tuple[int, ...]:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def pad(v: Tuple[int, ...]) -> Tuple[int, ...]:
    return (v + (0, 0, 0))[:3] if len(v) < 3 else v


def next_version(code_version: str, tags: Iterable[str]) -> str:
    code = pad(parse(code_version))
    released = [pad(parse(t)) for t in tags if parse(t)]
    latest = max(released, default=())
    if not latest or code > latest:
        return ".".join(map(str, code))
    return ".".join(map(str, latest[:-1] + (latest[-1] + 1,)))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def main() -> int:
    code = re.search(r'__version__ = "([^"]*)"', INIT.read_text(encoding="utf-8")).group(1)
    tags = git("tag", "--list", "v*").split()
    version = next_version(code, tags)
    previous = max((t for t in tags if parse(t)), key=lambda t: pad(parse(t)), default="")
    span = f"{previous}..HEAD" if previous else "HEAD"
    subjects = [
        s for s in git("log", span, "--no-merges", "--pretty=%s").splitlines()
        if s.strip() and not s.startswith("release:")
    ]
    notes = "\n".join(f"- {s}" for s in subjects[:30]) or "- Cập nhật nhỏ"
    (ROOT / "release_notes.md").write_text(notes + "\n", encoding="utf-8")
    print(f"version={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
