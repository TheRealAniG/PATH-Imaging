"""Push committed work, then release local copies of uploaded LFS captures.

Run: uv run scripts/push_and_release_captures.py
"""
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    root = Path(git("rev-parse", "--show-toplevel"))
    if Path.cwd().resolve() != root:
        raise RuntimeError("Run from the worktree root")
    if git("status", "--porcelain"):
        raise RuntimeError("Commit your changes before pushing and releasing captures")
    branch = git("symbolic-ref", "--short", "HEAD")
    commit = git("rev-parse", "HEAD")
    media = next(line.removeprefix("LocalMediaDir=") for line in
                 git("lfs", "env").splitlines() if line.startswith("LocalMediaDir="))
    captures = []
    for name in git("ls-files", "-z", "--", "captures/").split("\0"):
        if not name:
            continue
        pointer = subprocess.check_output(["git", "show", f"HEAD:{name}"])
        match = re.fullmatch(
            rb"version https://git-lfs.github.com/spec/v1\n"
            rb"oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n", pointer)
        if match is None:
            continue
        oid, size = match[1].decode(), int(match[2])
        path = root / name
        data = path.read_bytes()
        if data != pointer and (len(data) != size or hashlib.sha256(data).hexdigest() != oid):
            raise RuntimeError(f"Local image differs from committed data: {name}")
        captures.append((path, pointer, oid))
    subprocess.run(["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"], check=True)
    remote = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if not remote or remote.split()[0] != commit or git("rev-parse", "HEAD") != commit:
        raise RuntimeError("Could not verify pushed commit; keeping local image data")
    release_oids = {oid for path, pointer, oid in captures
                    if path.read_bytes() != pointer or
                    (Path(media) / oid[:2] / oid[2:4] / oid).is_file()}
    if release_oids:
        # Verify only objects with local data to release. Already-released
        # pointers have no local object for git-lfs to upload again.
        subprocess.run(["git", "lfs", "push", "--object-id", "origin",
                        *sorted(release_oids)], check=True)
    # These settings apply to all worktrees sharing this repository's config.
    subprocess.run(["git", "config", "--local", "filter.lfs.smudge",
                    "git-lfs smudge --skip -- %f"], check=True)
    subprocess.run(["git", "config", "--local", "filter.lfs.process",
                    "git-lfs filter-process --skip"], check=True)
    released = 0
    for path, pointer, oid in captures:
        data = path.read_bytes()
        if data != pointer:
            if hashlib.sha256(data).hexdigest() != oid:
                raise RuntimeError(f"Image changed during push: {path}")
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp.write(pointer)
                replacement = Path(temp.name)
            replacement.replace(path)
            released += len(data) - len(pointer)
        cached = Path(media) / oid[:2] / oid[2:4] / oid
        if cached.is_file():
            if hashlib.sha256(cached.read_bytes()).hexdigest() != oid:
                raise RuntimeError(f"Unexpected LFS cache contents: {cached}")
            released += cached.stat().st_size
            cached.unlink()
    if captures:
        # Pointer bytes already match HEAD; refresh the index's file metadata.
        subprocess.run(["git", "add", "--",
                        *[str(path.relative_to(root)) for path, _, _ in captures]], check=True)
    print(f"Pushed {branch}; {len(captures)} captures are local pointers. "
          f"Released {released / 1024**2:.1f} MiB of image data.")
    print("Restore images when needed with: git lfs pull")


if __name__ == "__main__":
    main()
