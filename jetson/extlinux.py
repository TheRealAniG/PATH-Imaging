#!/usr/bin/env python3
"""Add or remove the PATH camera boot entry in /boot/extlinux/extlinux.conf. Used by install.sh and uninstall.sh.

    extlinux.py add <extlinux.conf> <fdt> <overlay> <menu text>
    extlinux.py remove <extlinux.conf>

'add' copies the "primary" entry (kernel, initrd, kernel arguments) into a new entry "path-cameras" that
also names the base device tree and our overlay, and makes it the default. The entry it copied is left
exactly as it was, so it stays as the way back if the new one does not boot. 'remove' deletes the entry
and puts the previous default back, restoring the file byte for byte.
"""
import os
import re
import sys
from pathlib import Path

LABEL = "path-cameras"
BEGIN, END = f"# >>> {LABEL}", f"# <<< {LABEL}"  # Fence around our entry; the previous default is recorded on the first line


def keyword(line):  # First word of a line, upper case; "" for blank lines and comments
    words = line.split()
    return words[0].upper() if words and not words[0].startswith("#") else ""


def default_of(lines):
    return next((line.split()[1] for line in lines if keyword(line) == "DEFAULT" and len(line.split()) > 1), None)


def entry(lines, label):
    """The lines of one LABEL entry, from its LABEL line up to and including its APPEND line."""
    start = next((i for i, line in enumerate(lines) if keyword(line) == "LABEL" and line.split()[1:2] == [label]), None)
    if start is None:
        return None
    for end in range(start + 1, len(lines)):
        if keyword(lines[end]) == "LABEL":
            break
        if keyword(lines[end]) == "APPEND":
            return lines[start:end + 1]
    sys.exit(f"extlinux.py: entry '{label}' has no APPEND line")


def without_ours(lines):
    """(lines without our fenced entry, previous default recorded in it or None)."""
    if not any(line.startswith(BEGIN) for line in lines):
        return lines, None
    start = next(i for i, line in enumerate(lines) if line.startswith(BEGIN))
    end = next((i for i in range(start, len(lines)) if lines[i].startswith(END)), None)
    if end is None:
        sys.exit(f"extlinux.py: found '{BEGIN}' without '{END}'; fix the file by hand")
    previous = re.search(r"previous DEFAULT: (\S+)\)", lines[start])
    if start and not lines[start - 1].strip():  # The blank line 'add' put in front
        start -= 1
    return lines[:start] + lines[end + 1:], previous[1] if previous else None


def set_default(lines, label):
    return [re.sub(r"^(\s*DEFAULT\s+)\S+", lambda m: m[1] + label, line) if keyword(line) == "DEFAULT" else line for line in lines]


def write(path, lines):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines))
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)  # In one step: a half-written boot file is the one thing to avoid here


def add(path, fdt, overlay, menu):
    lines = path.read_text().split("\n")
    lines, recorded = without_ours(lines)  # Installing again replaces our entry
    previous = recorded or default_of(lines) or "primary"
    base = entry(lines, "primary") or entry(lines, previous)
    if base is None:
        sys.exit("extlinux.py: no 'primary' entry to copy the kernel settings from")
    new, overlays = [], [overlay]
    for line in base:
        key = keyword(line)
        indent = line[:len(line) - len(line.lstrip())]
        if key == "LABEL":
            new.append(f"LABEL {LABEL}")
        elif key == "MENU":
            new.append(f"{indent}MENU LABEL {menu}")
        elif key == "FDT":
            fdt = line.split()[1]  # The base entry already names its device tree: keep that one
        elif key in ("OVERLAYS", "FDTOVERLAYS"):
            overlays = [o for o in line.split()[1].split(",") if o != overlay] + [overlay]
        elif key == "APPEND":
            new += [f"{indent}FDT {fdt}", f"{indent}OVERLAYS {','.join(overlays)}", line]
        elif key:
            new.append(line)
    trailing_newline = lines[-1:] == [""]
    body = lines[:-1] if trailing_newline else lines
    body += ["", f"{BEGIN} (added by PATH-Imaging jetson/install.sh; previous DEFAULT: {previous})", *new, END]
    write(path, set_default(body + ([""] if trailing_newline else []), LABEL))
    print(f"extlinux.conf: entry '{LABEL}' added and made the default (was '{previous}'); '{previous}' is unchanged")


def remove(path):
    lines, previous = without_ours(path.read_text().split("\n"))
    if previous is None:
        print(f"extlinux.conf: no '{LABEL}' entry, nothing to remove")
        return
    if entry(lines, previous) is None:
        previous = "primary"
    if default_of(lines) == LABEL:
        lines = set_default(lines, previous)
    write(path, lines)
    print(f"extlinux.conf: entry '{LABEL}' removed, default is '{default_of(lines)}'")


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "add":
        add(Path(sys.argv[2]), *sys.argv[3:6])
    elif len(sys.argv) == 3 and sys.argv[1] == "remove":
        remove(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
