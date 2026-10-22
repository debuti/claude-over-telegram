"""Runs each conversation's Claude in its own Docker container (SANDBOX_DOCKER).

The SDK is pointed at claude-docker.sh instead of the claude binary; that script
starts the real binary in a container limited in CPU and memory, which sees only
the conversation's folder and the user's Claude and git settings.
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


def prepare(cfg: Config) -> tuple[str, dict[str, str]]:
    """Image ready and leftovers gone; returns the CLI path and env to give the SDK."""
    if not shutil.which("docker"):
        raise SystemExit("SANDBOX_DOCKER is on but docker is not installed (or set SANDBOX_DOCKER=false)")
    try:
        _docker("info", capture_output=True)
    except subprocess.CalledProcessError:
        raise SystemExit("SANDBOX_DOCKER is on but docker is not usable by this user (or set SANDBOX_DOCKER=false)")

    # Containers left behind if the bot was killed before its sessions closed.
    stale = _docker("ps", "-aq", "--filter", "label=cot.agent=1", capture_output=True).stdout.split()
    if stale:
        log.info("removing %d leftover agent containers", len(stale))
        _docker("rm", "-f", *stale, capture_output=True)

    claude = cfg.cli_path or str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")
    # The SDK checks the CLI version on every connect: a whole container each time.
    os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
    env = {
        "COT_IMAGE": _image(),
        "COT_CLAUDE": str(Path(claude).resolve()),
        "COT_CPUS": cfg.docker_cpus,
        "COT_MEMORY": cfg.docker_memory,
    }
    return str(WRAPPER), env
