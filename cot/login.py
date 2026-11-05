"""Claude's login from Telegram: `claude auth login` prints a URL, and takes the code that page shows."""

import asyncio
import os
import re

URL = re.compile(rb"https://\S+")
CODE = re.compile(r"\S{20,}")  # any other text while a login waits is a message for Claude
WAIT_URL = 30  # seconds for the URL to appear
WAIT_CODE = 10 * 60  # seconds for the user to send the code
WAIT_DONE = 60  # seconds for Claude to check it


class LoginError(Exception):
    pass


class Login:
    """One run of the CLI's login, from showing the URL to checking the code."""

    def __init__(self, cli: str, key) -> None:
        self.cli, self.key = cli, key
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def waiting(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> str:
        """Run it (no browser here to open), and return the URL to sign in at."""
        self.proc = await asyncio.create_subprocess_exec(
            self.cli, "auth", "login", "--claudeai",
            env={**os.environ, "BROWSER": "/bin/true"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.get_running_loop().call_later(WAIT_CODE, self.close)
        try:
            return await asyncio.wait_for(self._url(), WAIT_URL)
        except (asyncio.TimeoutError, LoginError):
            self.close()
            raise LoginError("Claude did not give a sign-in address") from None

    async def _url(self) -> str:
        while line := await self.proc.stdout.readline():
            if found := URL.search(line):
                return found.group().decode()
        raise LoginError

    async def finish(self, code: str) -> None:
        """Give it the code; LoginError with what it said if it isn't accepted."""
        proc, self.proc = self.proc, None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(code.encode() + b"\n"), WAIT_DONE)
        except asyncio.TimeoutError:
            proc.kill()
            raise LoginError("Claude did not answer") from None
        if proc.returncode:
            words = out.decode(errors="replace").rpartition(">")[2].strip()
            raise LoginError(words or f"exit code {proc.returncode}")

    def close(self) -> None:
        if self.waiting:
            self.proc.kill()
