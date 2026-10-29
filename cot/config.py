"""Settings loaded from the environment (and a .env file next to the repo)."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    workdir: Path
    permission_mode: str
    state_dir: Path
    cli_path: str | None
    sandbox_bash: bool
    docker: bool
    docker_cpus: str
    docker_memory: str
    docker_exclude: frozenset[str]
    docker_mounts: dict[str, list[str]]  # folder ("*": all) -> host paths it also sees, "path[:rw]"
    docker_path: str  # prepended to PATH in containers, e.g. for binaries in a mounted folder
    docker_limits: dict[str, tuple[str, str]]  # folder -> (cpus, memory)
    compact_at: int  # context tokens at which a conversation compacts, also mid-turn; 0: off
    cache_ttl: str  # auto | 5m | 1h: the prompt cache's lifetime (see costguard.cache_ttl)
    api_key: bool  # ANTHROPIC_API_KEY set: billed per use, 5-minute cache by default
    cache_alarms: tuple[int, ...]  # minutes before the cache expires at which to warn; (): off
    debug_ttl: int  # COT_DEBUG_TTL_SECONDS: the TTL the bot assumes, for testing; 0: off


def _limits(spec: str, cpus: str, memory: str) -> dict[str, tuple[str, str]]:
    """"folder=cpus[:memory],…" into {folder: (cpus, memory)}, the defaults filling gaps."""
    limits = {}
    for item in spec.replace(" ", "").split(","):
        if not item:
            continue
        folder, _, value = item.partition("=")
        c, _, m = value.partition(":")
        limits[folder] = (c or cpus, m or memory)
    return limits


def _mounts(spec: str) -> dict[str, list[str]]:
    """"folder=path[:rw][+path…],…" into {folder: ["path", "path:rw", …]}; folder "*" is every one."""
    mounts: dict[str, list[str]] = {}
    for item in spec.replace(" ", "").split(","):
        folder, _, paths = item.partition("=")
        for p in filter(None, paths.split("+")):
            path, rw = (p[:-3], ":rw") if p.endswith(":rw") else (p.removesuffix(":ro"), "")
            mounts.setdefault(folder, []).append(str(Path(path).expanduser()) + rw)
    return mounts


def _minutes(spec: str) -> tuple[int, ...]:
    """"30,15,…" into (30, 15, …); empty or "off": ()."""
    if spec.strip().lower() in ("", "off", "0"):
        return ()
    try:
        return tuple(sorted({int(x) for x in spec.replace(" ", "").split(",") if x}, reverse=True))
    except ValueError:
        raise SystemExit(f"CACHE_ALARMS must be minutes separated by commas, not {spec}") from None


def load(workdir: Path) -> Config:
    load_dotenv(REPO_DIR / ".env")

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is not set (see .env.example)")

    ids = frozenset(
        int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
    )
    if not ids:
        # Still start, so the owner can message the bot and read their id from the log.
        logging.getLogger(__name__).warning(
            "ALLOWED_USER_IDS is empty: every message is ignored and its sender id logged"
        )

    state_dir = Path(os.environ.get("STATE_DIR") or REPO_DIR / "state").expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    cache_ttl = (os.environ.get("CACHE_TTL") or "auto").strip().lower()
    if cache_ttl not in ("auto", "5m", "1h"):
        raise SystemExit(f"CACHE_TTL must be auto, 5m or 1h, not {cache_ttl}")

    return Config(
        telegram_token=token,
        allowed_user_ids=ids,
        workdir=workdir.expanduser().resolve(),
        permission_mode=os.environ.get("PERMISSION_MODE") or "auto",
        state_dir=state_dir,
        cli_path=os.environ.get("CLAUDE_CLI_PATH") or None,
        sandbox_bash=os.environ.get("SANDBOX_BASH", "").lower() in ("1", "true", "yes"),
        docker=os.environ.get("SANDBOX_DOCKER", "true").lower() in ("1", "true", "yes"),
        docker_cpus=os.environ.get("DOCKER_CPUS") or "2",
        docker_memory=os.environ.get("DOCKER_MEMORY") or "4g",
        docker_limits=_limits(
            os.environ.get("DOCKER_LIMITS", ""),
            os.environ.get("DOCKER_CPUS") or "2",
            os.environ.get("DOCKER_MEMORY") or "4g",
        ),
        docker_mounts=_mounts(os.environ.get("DOCKER_MOUNTS", "")),
        docker_path=":".join(
            str(Path(p).expanduser()) for p in os.environ.get("DOCKER_PATH", "").replace(" ", "").split(":") if p
        ),
        compact_at=int(os.environ.get("AUTO_COMPACT_TOKENS") or 150_000),
        cache_ttl=cache_ttl,
        api_key=bool(os.environ.get("ANTHROPIC_API_KEY")),
        cache_alarms=_minutes(os.environ.get("CACHE_ALARMS", "30,15,10,5,2,1")),
        debug_ttl=int(os.environ.get("COT_DEBUG_TTL_SECONDS") or 0),
        docker_exclude=frozenset(x for x in os.environ.get("DOCKER_EXCLUDE", "").replace(" ", "").split(",") if x),
    )
