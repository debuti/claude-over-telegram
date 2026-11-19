from cot.bot import Bot


def test_service_command(tmp_path):
    assert Bot._service_command(tmp_path) is None
    (tmp_path / ".tg-service").write_text("\n# start the bot\n  ./run.sh  \nother\n")
    assert Bot._service_command(tmp_path) == "./run.sh"
    (tmp_path / ".tg-service").write_text("# nothing\n\n")
    assert Bot._service_command(tmp_path) is None
