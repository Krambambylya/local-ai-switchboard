"""Static review of the Windows installer. These tests do not run it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer"
SCRIPTS = (
    "install.ps1",
    "uninstall.ps1",
    "update.ps1",
    "start.ps1",
    "stop.ps1",
    "doctor.ps1",
    "Common.ps1",
)

PINS = {
    "python_url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe",
    "python_md5": "5eddb0b6f12c852725de071ae681dde4",
    "ollama_url": "https://github.com/ollama/ollama/releases/download/v0.40.14/OllamaSetup.exe",
    "ollama_sha256": "e069c7cc7d4808f77684e81efa0b1f853a5190b4b8f070bc8fd63041180e3eba",
    "comfy_url": "https://github.com/Comfy-Org/ComfyUI/releases/download/v0.41.0/ComfyUI_windows_portable_nvidia.7z",
    "comfy_sha256": "7ff446d6904a9eecdfa07870a06238cc33a6204dcd31726edc6783efd080ac34",
    "fp8_sha256": "97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6",
    "text_sha256": "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
    "vae_sha256": "868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3",
}


def _read(name: str) -> str:
    return (INSTALLER / name).read_text(encoding="utf-8")


def _all_text() -> str:
    return "\n".join(_read(name) for name in SCRIPTS)


def _scan_powershell(text: str) -> None:
    """Reject unbalanced quotes or braces outside comments and strings."""

    i = 0
    braces = 0
    parens = 0
    while i < len(text):
        if text.startswith("<#", i):
            end = text.find("#>", i + 2)
            assert end != -1, "unclosed PowerShell block comment"
            i = end + 2
            continue
        if text[i] == "#" and (i == 0 or text[i - 1] != "`"):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        if text.startswith("@'", i) or text.startswith('@"', i):
            quote = text[i + 1]
            marker = f"'{quote}@" if quote == "'" else f'"{quote}@'
            end = text.find(marker, i + 2)
            assert end != -1, "unclosed PowerShell here-string"
            i = end + len(marker)
            continue
        if text[i] in {"'", '"'}:
            quote = text[i]
            i += 1
            while i < len(text):
                if quote == "'" and text.startswith("''", i):
                    i += 2
                    continue
                if quote == '"' and text[i] == "`" and i + 1 < len(text):
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            else:
                raise AssertionError("unclosed PowerShell string")
            continue
        if text[i] == "{":
            braces += 1
        elif text[i] == "}":
            braces -= 1
            assert braces >= 0, "extra closing brace"
        elif text[i] == "(":
            parens += 1
        elif text[i] == ")":
            parens -= 1
            assert parens >= 0, "extra closing parenthesis"
        i += 1
    assert braces == 0, f"unbalanced braces ({braces})"
    assert parens == 0, f"unbalanced parentheses ({parens})"


def test_installer_scripts_exist_and_are_ascii() -> None:
    for name in SCRIPTS:
        text = _read(name)
        text.encode("ascii")
        _scan_powershell(text)


def test_powershell_parser_when_available() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip(
            "PowerShell is not installed here. The installer was reviewed as text and was not parsed or executed."
        )
    for name in SCRIPTS:
        path = INSTALLER / name
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-Command",
                (
                    "$tokens=$null; $errors=$null; "
                    "[void][System.Management.Automation.Language.Parser]::ParseFile("
                    f"'{path}', [ref]$tokens, [ref]$errors); "
                    "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


def test_pins_name_exact_vendor_assets() -> None:
    pins = json.loads((INSTALLER / "pins.json").read_text(encoding="utf-8"))
    blob = json.dumps(pins)
    assert "latest" not in blob.lower()
    assert pins["python"]["url"] == PINS["python_url"]
    assert pins["python"]["checksum"] == PINS["python_md5"]
    assert pins["python"]["algorithm"] == "MD5"
    assert pins["ollama"]["url"] == PINS["ollama_url"]
    assert pins["ollama"]["checksum"] == PINS["ollama_sha256"]
    assert pins["ollama"]["expected_bytes"] == 1675999728
    assert pins["comfyui"]["url"] == PINS["comfy_url"]
    assert pins["comfyui"]["checksum"] == PINS["comfy_sha256"]
    assert pins["comfyui"]["expected_bytes"] == 2010075321
    models = {item["filename"]: item for item in pins["comfy_models"]}
    assert set(models) == {
        "flux-2-klein-4b-fp8.safetensors",
        "qwen_3_4b.safetensors",
        "flux2-vae.safetensors",
    }
    assert models["flux-2-klein-4b-fp8.safetensors"]["checksum"] == PINS["fp8_sha256"]
    assert models["qwen_3_4b.safetensors"]["checksum"] == PINS["text_sha256"]
    assert models["flux2-vae.safetensors"]["checksum"] == PINS["vae_sha256"]
    assert models["flux-2-klein-4b-fp8.safetensors"]["url"].startswith(
        "https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/"
    )
    assert "Comfy-Org/Flux-2-Klein-4B" in models["qwen_3_4b.safetensors"]["url"]
    assert "Comfy-Org/Flux-2-Klein-4B" in models["flux2-vae.safetensors"]["url"]
    for item in models.values():
        assert item["checksum"]
        assert item["expected_bytes"] > 0
        lowered = item["url"].lower()
        assert "fp16" not in lowered
        assert "9b" not in lowered
    assert not list(ROOT.glob("**/*.safetensors"))


def test_scripts_do_not_execute_remote_code() -> None:
    text = _all_text()
    lowered = text.lower()
    assert "invoke-expression" not in lowered
    assert "downloadstring" not in lowered
    assert "encodedcommand" not in lowered
    assert "ollama.com/install.ps1" not in lowered
    assert "releases/latest" not in lowered
    assert "/latest/" not in lowered
    assert "portproxy" not in lowered
    assert "new-netnat" not in lowered
    assert re.search(r"(?i)(^|[^\w])iex(\.exe)?\b", text) is None
    assert "us.download.nvidia.com" not in lowered


def test_security_and_lifecycle_contracts() -> None:
    text = _all_text()
    install = _read("install.ps1")
    common = _read("Common.ps1")
    start = _read("start.ps1")
    stop = _read("stop.ps1")
    update = _read("update.ps1")
    uninstall = _read("uninstall.ps1")
    assert "SupportsShouldProcess" in install
    assert "SupportsShouldProcess" in uninstall
    assert "SupportsShouldProcess" in update
    assert "LocalSubnet" in common
    assert "-Profile Private" in common
    assert "RandomNumberGenerator" in common
    assert "icacls.exe" in common
    assert "qwen3.5:4b" in install
    assert "num_ctx" in install
    assert "8192" in install
    assert "AcceptUnchecksummedModels" in install
    assert "checksum_verified" in install
    assert "python_embeded\\python.exe" in install
    assert "'--listen'" in install
    assert "'127.0.0.1'" in install
    assert "'--disable-auto-launch'" in install
    assert "'--windows-standalone-build'" in install
    assert "Dry run finished. No component was installed." in install
    assert "python_embeded" not in start
    assert "--listen" not in start
    assert "IncludeComfyUI" in stop
    assert "AllowDowngrade" in update
    assert "RemoveData" in uninstall
    assert "RemoveModels" in uninstall
    assert "RemoveSecrets" in uninstall
    assert "enabled -ne $false" in install
    assert "OLLAMA_HOST" in text
    assert "127.0.0.1:11434" in text
    assert "New-NetFirewallRule" in common
    assert common.count("New-NetFirewallRule") == 1
    assert "Start-Transcript" not in text
    for line in text.splitlines():
        if "Write-InstallLog" in line and re.search(r"\$token\b", line, re.IGNORECASE):
            raise AssertionError(line)
    workflow = json.loads(
        (ROOT / "src/local_ai_switchboard/workflows/flux2-klein-4b.json").read_text(
            encoding="utf-8"
        )
    )
    assert workflow["enabled"] is False
    assert workflow["workflow"] is None
    assert "class_type" not in json.dumps(workflow)
