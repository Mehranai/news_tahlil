import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from app.cli import main

CONFIG = """[classification]
provider = "llama_cpp"
base_url = "http://127.0.0.1:18085/v1"
model = "local-test"
"""


def test_serve_model_reuses_a_matching_running_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "test.toml"
    config.write_text(CONFIG, encoding="utf-8")

    def list_models(url: str) -> set[str]:
        assert url == "http://127.0.0.1:18085/v1/models"
        return {"local-test"}

    monkeypatch.setattr("app.local_model._list_models", list_models)
    assert main(["--config", str(config), "serve-model"]) == 0
    assert "already listening" in capsys.readouterr().out


def test_serve_model_passes_configured_alias_and_loopback_port_to_llama(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "test.toml"
    config.write_text(CONFIG, encoding="utf-8")
    binary = tmp_path / "mock llama-server"
    weights = tmp_path / "model weights.gguf"
    binary.touch()
    weights.touch()
    monkeypatch.setattr("app.local_model._list_models", lambda _: None)
    args: list[str] = []
    if os.name == "nt":

        def launch(command: list[str], *, cwd: Path, check: bool) -> CompletedProcess:
            assert cwd == binary.parent and not check
            args.extend(command)
            return CompletedProcess(command, 0)

        monkeypatch.setattr("app.local_model.subprocess.run", launch)
    else:

        def exec_server(path: Path, command: list[str]) -> None:
            assert path == binary
            args.extend(command)
            raise SystemExit(0)

        monkeypatch.setattr("app.local_model.os.execv", exec_server)

    try:
        assert (
            main(
                [
                    "--config",
                    str(config),
                    "serve-model",
                    "--server",
                    str(binary),
                    "--model-file",
                    str(weights),
                    "--gpu-layers",
                    "0",
                ]
            )
            == 0
        )
    except SystemExit as exited:
        assert exited.code == 0
    assert args[:9] == [
        str(binary),
        "--host",
        "127.0.0.1",
        "--port",
        "18085",
        "--model",
        str(weights),
        "--alias",
        "local-test",
    ]
    assert args[-4:] == ["--n-gpu-layers", "0", "--device", "none"]
