"""A fake phone answering the shell commands AdbService sends, for resilience tests."""

import re
import threading

DONE = "/storage/emulated/0/Android/Done"
CMT = "/storage/emulated/0/Android/CMT"


class ShellPhone:
    """Flag folders of one phone, driven through ``AdbService.run_shell``.

    ``gone = True`` behaves like a phone unplugged mid-command: adb prints nothing.
    ``cut = True`` returns only the first line, like a socket read that timed out.
    """

    def __init__(self):
        self.dirs = {}
        self.gone = False
        self.cut = False
        self.lock = threading.Lock()

    def flags(self, folder):
        with self.lock:
            return set(self.dirs.get(folder, ()))

    def remove(self, folder, *names):
        with self.lock:
            for name in names:
                self.dirs.get(folder, set()).discard(name)

    def run_shell(self, serial, command, timeout=8):
        with self.lock:
            if self.gone:
                return ""
            out, code = [], 0
            for part in re.split(r" && |; ", command):
                words = part.split()
                if words[:2] == ["mkdir", "-p"]:
                    self.dirs.setdefault(words[2], set())
                elif words[:2] == ["rm", "-f"]:
                    folder, _, name = words[2].rpartition("/")
                    if name == "*":
                        self.dirs.get(folder, set()).clear()
                    else:
                        self.dirs.get(folder, set()).discard(name[:-4])
                elif words[0] == "touch":
                    for path in words[1:]:
                        folder, _, name = path.rpartition("/")
                        self.dirs[folder].add(name[:-4])
                elif words[0] == "ls":
                    folder = words[1]
                    if folder in self.dirs:
                        out += sorted(f"{n}.txt" for n in self.dirs[folder])
                        code = 0
                    else:
                        out.append(f"ls: {folder}: No such file or directory")
                        code = 1
                elif words[0] == "echo":
                    out.append(words[1].replace("$?", str(code)))
            text = "\n".join(out)
            return text.splitlines()[0] if self.cut and text else text
