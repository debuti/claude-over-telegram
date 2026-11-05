import stat
import time

import pytest

from cot import login, usage

SCRIPT = """#!/bin/sh
echo "Opening browser to sign in…"
echo "If the browser didn't open, visit: https://example.com/auth?code=true&state=abc"
printf "Paste code here if prompted > "
read code
[ "$code" = good-code ] && exit 0
echo "Login failed: Request failed with status code 400"
exit 1
"""


@pytest.fixture
def cli(tmp_path):
    path = tmp_path / "claude"
    path.write_text(SCRIPT)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_a_good_code_logs_in(cli):
    run = login.Login(cli, "key")
    assert await run.start() == "https://example.com/auth?code=true&state=abc"
    assert run.waiting
    await run.finish("good-code")
    assert not run.waiting


async def test_a_bad_code_says_why(cli):
    run = login.Login(cli, "key")
    await run.start()
    with pytest.raises(login.LoginError, match="status code 400"):
        await run.finish("bad-code")
    assert not run.waiting


async def test_no_address_is_an_error(tmp_path):
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\necho nothing\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(login.LoginError):
        await login.Login(str(path), "key").start()


def test_renewed_since(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert not usage.renewed_since(0)  # no login at all
    good = '{"claudeAiOauth": {"accessToken": "t", "expiresAt": %d}}' % ((time.time() + 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(good)
    assert usage.renewed_since(0)
    assert not usage.renewed_since(time.time() + 10)  # this one is older than the failure
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "", "expiresAt": 0}}')
    assert not usage.renewed_since(0)  # what a failed refresh leaves
