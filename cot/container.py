"""Runs each conversation's Claude in its own Docker container (SANDBOX_DOCKER).

The SDK is pointed at claude-docker.sh instead of the claude binary; that script
starts the real binary in the conversation's container, limited in CPU and memory,
which sees only the conversation's folder and the user's Claude and git settings.
Containers outlive Claude (and the bot) while something runs in them.
"""

import getpass
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

import claude_agent_sdk

from .config import REPO_DIR, Config

log = logging.getLogger(__name__)

WRAPPER = Path(__file__).with_name("claude-docker.sh")
DOCKERFILE = REPO_DIR / "docker" / "Dockerfile"
# What runs in an idle container: its init, the process keeping it up, a Claude on its way out.
KEEPERS = {"docker-init", "sleep", "claude"}


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True, **kwargs)


def _image() -> str:
    """Build the image unless this Dockerfile and user already have one."""
    build_args = {
        "USER_NAME": getpass.getuser(),
        "USER_ID": str(os.getuid()),
        "GROUP_ID": str(os.getgid()),
        "HOME_DIR": str(Path.home()),
    }
    digest = hashlib.sha256(DOCKERFILE.read_bytes() + repr(sorted(build_args.items())).encode()).hexdigest()
    image = f"cot-agent:{digest[:12]}"
    if _docker("image", "inspect", image, capture_output=True, check=False).returncode != 0:
        log.info("building %s (the first time takes a few minutes)", image)
        _docker(
            "build", "-t", image, "-f", str(DOCKERFILE),
            *(a for k, v in build_args.items() for a in ("--build-arg", f"{k}={v}")),
            str(DOCKERFILE.parent),
        )
    return image


def stop_claude(name: str) -> None:
    """docker exec leaves Claude running when its connection closes; alone, it would go on
    acting on finished background jobs with nobody to read it."""
    _docker("exec", name, "pkill", "-x", "claude", capture_output=True, check=False)


def reap(name: str) -> None:
    """End Claude in the container, and remove it unless something else still runs there."""
    stop_claude(name)
    top = _docker("top", name, "-eo", "pid,comm", capture_output=True, check=False)
    if top.returncode == 0:  # not running: nothing to wait for
        busy = [line.split(None, 1)[1] for line in top.stdout.splitlines()[1:] if line.split(None, 1)[1:]]
        busy = [comm for comm in busy if comm.strip() not in KEEPERS]
        if busy:
            log.info("keeping %s, still running: %s", name, ", ".join(busy[:5]))
            return
    _docker("rm", "-f", name, capture_output=True, check=False)


def _bytes(size: str) -> int:
    units = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    size = size.strip().lower().removesuffix("b") or "0"
    unit = size[-1] if size[-1] in units else "b"
    return int(float(size.rstrip("bkmgt")) * units[unit])


def limits(cpus: str, memory: str) -> dict[str, str]:
    """Env for claude-docker.sh; swap as much again as memory, as docker does by default."""
    mem = _bytes(memory)
    return {"COT_CPUS": cpus, "COT_MEMORY": str(mem), "COT_MEMORY_SWAP": str(2 * mem)}


def bundled_cli() -> str:
    return str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")


def prepare(cfg: Config) -> tuple[str, dict[str, str]]:
    """Image ready and leftovers gone; returns the CLI path and env to give the SDK."""
    if not shutil.which("docker"):
        raise SystemExit("SANDBOX_DOCKER is on but docker is not installed (or set SANDBOX_DOCKER=false)")
    try:
        _docker("info", capture_output=True)
    except subprocess.CalledProcessError:
        raise SystemExit(
            "SANDBOX_DOCKER is on but docker is not usable by this user (or set SANDBOX_DOCKER=false)"
        ) from None

    # Containers whose work is done; those still running something are kept.
    names = _docker("ps", "-a", "--filter", "label=cot.agent=1", "--format", "{{.Names}}", capture_output=True)
    for name in names.stdout.split():
        reap(name)

    claude = cfg.cli_path or bundled_cli()
    # The SDK checks the CLI version on every connect: a whole container each time.
    os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
    env = {
        "COT_IMAGE": _image(),
        "COT_CLAUDE": str(Path(claude).resolve()),
    }
    return str(WRAPPER), env
