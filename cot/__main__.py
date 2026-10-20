import argparse
import fcntl
import logging
from pathlib import Path

from . import config
from .bot import Bot


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m cot", description="Claude Code over Telegram")
    parser.add_argument(
        "workdir",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="directory Claude works in and is confined to (default: current directory)",
    )
    args = parser.parse_args()
    if not args.workdir.is_dir():
        parser.error(f"not a directory: {args.workdir}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every getUpdates call, which leaks the bot token into the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = config.load(args.workdir)

    # Two pollers on the same token make Telegram reject both (409 Conflict).
    lock = open(cfg.state_dir / "cot.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another instance is already running")

    Bot(cfg).run()


if __name__ == "__main__":
    main()
