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

    return Config(
        telegram_token=token,
        allowed_user_ids=ids,
        workdir=workdir.expanduser().resolve(),
        permission_mode=os.environ.get("PERMISSION_MODE") or "default",
        state_dir=state_dir,
        cli_path=os.environ.get("CLAUDE_CLI_PATH") or None,
        sandbox_bash=os.environ.get("SANDBOX_BASH", "").lower() in ("1", "true", "yes"),
    )
