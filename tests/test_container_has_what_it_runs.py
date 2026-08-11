"""Whatever the deploy config runs must exist in the image.

The Dockerfile copies files by name, so it is an allow-list: a script committed
to the repo is absent from the container until someone remembers to name it.
Nothing catches that — not the test suite, which runs against a checkout, and
not the build, which succeeds either way. It surfaces as one line at deploy
time:

    bash: scripts/migrate.sh: No such file or directory

That cost a day, on top of eight days production had already spent on a stale
build. The check is a directory listing; the bug it prevents is invisible until
production is down.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _copied_paths() -> list[str]:
    """Top-level paths the Dockerfile puts into the image."""
    copied = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        match = re.match(r"\s*COPY\s+(?!--from)(\S+)\s+(\S+)", line)
        if match:
            copied.append(match.group(1))
    return copied


def _commands_run_on_deploy() -> list[str]:
    """Commands railway.toml executes inside the container."""
    text = (ROOT / "railway.toml").read_text()
    return re.findall(r'^\s*(?:preDeploy|start)Command\s*=\s*"([^"]+)"', text, re.M)


def test_every_file_the_deploy_runs_is_in_the_image():
    copied = _copied_paths()
    missing = []
    for command in _commands_run_on_deploy():
        for token in command.split():
            # A path-looking argument: has a slash or a known script suffix.
            if "/" not in token and not token.endswith((".sh", ".py")):
                continue
            path = ROOT / token
            if not path.exists():
                continue  # not one of ours (a flag, a URL, an interpreter)
            top = token.split("/")[0]
            if top not in copied and token not in copied:
                missing.append(f"{token} (deploy runs it; Dockerfile never copies {top}/)")
    assert not missing, "\n".join(missing)


def test_the_check_knows_what_the_dockerfile_copies():
    """Guard the guard: if the COPY parsing silently matched nothing, the test
    above would pass for every possible Dockerfile."""
    copied = _copied_paths()
    assert {"app", "migrations", "scripts"} <= set(copied), copied
    assert _commands_run_on_deploy(), "railway.toml declares no commands to check"
