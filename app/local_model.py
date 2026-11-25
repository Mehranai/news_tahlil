"""Run the local llama.cpp server from the project's model settings."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

from app.config.settings import ClassificationSettings

DEFAULT_MODEL = Path("data/local-ollama/models/Qwen_Qwen3.5-9B-Q5_K_S.gguf")


class LocalModelError(Exception):
    """A startup error suitable for displaying at the command line."""


def serve_model(
    settings: ClassificationSettings | None,
    *,
    model_file: Path,
    server_path: Path | None,
    gpu_layers: int | None,
    device: str | None,
) -> int:
    if settings is None or settings.provider != "llama_cpp":
        raise LocalModelError("Configure [classification] with provider = 'llama_cpp'")
    url = settings.base_url
    if (
        url.scheme != "http"
        or url.host not in {"localhost", "127.0.0.1", "[::1]"}
        or (url.path or "").rstrip("/") != "/v1"
    ):
        raise LocalModelError("Configure a local HTTP base_url ending in /v1")

    models = _list_models(str(url).rstrip("/") + "/models")
    if models is not None:
        if settings.model not in models:
            raise LocalModelError(
                "A different model is already listening at classification.base_url"
            )
        print(f"{settings.model} is already listening at {url}", flush=True)
        return 0

    model_file = model_file.resolve()
    if not model_file.is_file():
        raise LocalModelError(f"Model file is missing: {model_file}. Set --model-file to its path.")
    server = _find_server(server_path)
    if gpu_layers is None and device is None and os.name == "nt":
        device = _windows_nvidia_device(server)
        gpu_layers = 8 if device else 0
    layer_count = str(gpu_layers) if gpu_layers is not None else ("auto" if device else "0")
    args = [
        str(server),
        "--host",
        url.host.strip("[]") if url.host != "localhost" else "127.0.0.1",
        "--port",
        str(url.port),
        "--model",
        str(model_file),
        "--alias",
        settings.model,
        "--ctx-size",
        "8192",
        "--parallel",
        "1",
        "--reasoning",
        "off",
        "--temp",
        "0",
        "--batch-size",
        "512",
        "--ubatch-size",
        "128",
        "--no-ui",
        "--no-warmup",
        "--n-gpu-layers",
        layer_count,
    ]
    if layer_count == "0" or device:
        args.extend(("--device", "none" if layer_count == "0" else device))

    if os.name != "nt":
        # Let a service manager supervise and signal the actual server process.
        os.execv(server, args)
    return subprocess.run(args, cwd=server.parent, check=False).returncode


def _list_models(url: str) -> set[str] | None:
    try:
        response = httpx.get(url, timeout=2, trust_env=False)
        response.raise_for_status()
        return {row["id"] for row in response.json()["data"]}
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise LocalModelError("Another service is responding at classification.base_url") from None


def _find_server(path: Path | None) -> Path:
    if path is not None:
        if not path.is_file():
            raise LocalModelError(f"llama-server binary is missing: {path}")
        return path.resolve()

    executable = "llama-server.exe" if os.name == "nt" else "llama-server"
    runtime = Path("data/local-llama/runtime")
    bundled = next((file for file in sorted(runtime.rglob(executable)) if file.is_file()), None)
    if bundled is not None:
        return bundled.resolve()
    if found := shutil.which(executable):
        return Path(found).resolve()
    raise LocalModelError("Install llama-server on PATH or pass its location with --server")


def _windows_nvidia_device(server: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(server), "--list-devices"],
            cwd=server.parent,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    match = re.search(r"(?m)^\s*(Vulkan\d+):\s*NVIDIA\b", result.stdout)
    return match[1] if match else None
