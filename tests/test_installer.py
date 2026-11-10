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
    "verify-image-pipeline.ps1",
)

PINS = {
    "python_url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe",
    "python_md5": "5eddb0b6f12c852725de071ae681dde4",
    "ollama_url": "https://github.com/ollama/ollama/releases/download/v0.40.14/OllamaSetup.exe",
    "ollama_sha256": "e069c7cc7d4808f77684e81efa0b1f853a5190b4b8f070bc8fd63041180e3eba",
    "comfy_url": "https://github.com/Comfy-Org/ComfyUI/releases/download/v0.41.0/ComfyUI_windows_portable_nvidia.7z",
    "comfy_sha256": "7ff446d6904a9eecdfa07870a06238cc33a6204dcd31726edc6783efd080ac34",
    "fp8_sha256": "97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6",
    "text_sha256": "3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8",
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
            marker = "'@" if quote == "'" else '"@'
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
    paths = [INSTALLER / name for name in SCRIPTS]
    paths.append(ROOT / "tests" / "installer_python_runtime.ps1")
    for path in paths:
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
    assert (
        pins["python"]["sha256"]
        == "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
    )
    assert pins["python"]["expected_bytes"] == 26964224
    assert pins["ollama"]["url"] == PINS["ollama_url"]
    assert pins["ollama"]["checksum"] == PINS["ollama_sha256"]
    assert pins["ollama"]["expected_bytes"] == 1675999728
    assert pins["comfyui"]["url"] == PINS["comfy_url"]
    assert pins["comfyui"]["checksum"] == PINS["comfy_sha256"]
    assert pins["comfyui"]["expected_bytes"] == 2010075321
    models = {item["filename"]: item for item in pins["comfy_models"]}
    assert set(models) == {
        "flux-2-klein-4b-fp8.safetensors",
        "qwen_3_4b_fp4_flux2.safetensors",
        "flux2-vae.safetensors",
    }
    assert "qwen_3_4b.safetensors" not in models
    text_encoder = models["qwen_3_4b_fp4_flux2.safetensors"]
    assert text_encoder["checksum"] == PINS["text_sha256"]
    assert text_encoder["expected_bytes"] == 3848213998
    assert text_encoder["revision"] == "8556e4d870cda7c53c7942b190bfeea5be9bd411"
    assert text_encoder["revision"] in text_encoder["url"]
    assert "/resolve/main/" not in text_encoder["url"]
    assert models["flux-2-klein-4b-fp8.safetensors"]["checksum"] == PINS["fp8_sha256"]
    assert models["flux2-vae.safetensors"]["checksum"] == PINS["vae_sha256"]
    seven_zip = pins["seven_zip"]
    assert seven_zip["version"] == "26.05"
    assert (
        seven_zip["url"]
        == "https://github.com/ip7z/7zip/releases/download/26.05/7zr.exe"
    )
    assert (
        seven_zip["checksum"]
        == "9bf582b894dfa665f852d7e9112b02290a1c303c2d95a21fcdc41ffcb03143b7"
    )
    assert seven_zip["expected_bytes"] == 602624
    assert seven_zip["algorithm"] == "SHA256"
    assert models["flux-2-klein-4b-fp8.safetensors"]["url"].startswith(
        "https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/"
    )
    assert "Comfy-Org/Flux-2-Klein-4B" in text_encoder["url"]
    assert "Comfy-Org/Flux-2-Klein-4B" in models["flux2-vae.safetensors"]["url"]
    for item in models.values():
        assert item["checksum"]
        assert item["expected_bytes"] > 0
        lowered_name = item["filename"].lower()
        lowered = item["url"].lower()
        assert "fp16" not in lowered_name
        assert "fp16" not in lowered
        assert "9b" not in lowered_name
        assert "klein-9b" not in lowered
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
    assert "RemoveSecrets" not in uninstall
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


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_root_alias_and_workflow_file_parameter() -> None:
    install = _read("install.ps1")
    assert install.index("[Alias('Root')]") < install.index("[string]$InstallRoot")
    assert "[string]$WorkflowFile" in install
    assert "SupportsShouldProcess" in install
    assert "load_manifest" in install
    assert "raw ComfyUI API export" in install
    assert "does not invent node ids" in install


def test_clean_install_does_not_require_python_git_ollama_comfy_or_winget() -> None:
    install = _read("install.ps1")
    preflight = _between(install, "function Get-MachinePreflight", "function Install-PythonRuntime")
    assert "facts.python = 'missing'" in preflight
    assert "facts.ollama = 'missing'" in preflight
    assert "facts.git = 'missing'" in preflight
    assert "Git is optional and is not required before installation." in preflight
    assert "warnings.Add('Git" not in preflight
    for line in preflight.splitlines():
        if "errors.Add" not in line:
            continue
        lowered = line.lower()
        assert "python" not in lowered
        assert "ollama" not in lowered
        assert "git" not in lowered
        assert "winget" not in lowered
        assert "comfy" not in lowered
    assert "winget" not in _all_text().lower()
    runtime = _between(install, "function Install-PythonRuntime", "function Install-OllamaRuntime")
    assert "python-3.12.10-amd64.exe" in runtime
    assert "Kept the existing Python runtime." in runtime


def test_existing_ollama_and_comfyui_are_kept() -> None:
    install = _read("install.ps1")
    ollama = _between(install, "function Install-OllamaRuntime", "function Install-ComfyPortable")
    comfy = _between(install, "function Install-ComfyPortable", "function Install-OllamaModel")
    assert "Kept the existing Ollama installation." in ollama
    assert "Keeping Ollama" in ollama
    assert "Kept the existing ComfyUI portable tree." in comfy
    assert "Find-ComfyPortableRoot" in comfy


def test_downloads_show_remaining_progress() -> None:
    common = _read("Common.ps1")
    install = _read("install.ps1")
    update = _read("update.ps1")
    assert "function Write-TransferProgress" in common
    assert "left " in common
    assert "ETA " in common
    assert "Write-Progress" not in common
    receive = _between(common, "function Receive-PinnedHttpFile", "function Invoke-PinnedTransfer")
    transfer = _between(common, "function Invoke-PinnedTransfer", "function Save-ReplacementDownload")
    assert "Write-TransferProgress" in receive
    assert "Wait-TransferProcess" in transfer
    assert "--continue-at" in transfer
    ollama = _between(install, "function Install-OllamaModel", "function Install-ComfyModels")
    assert "Invoke-ConsoleCommand" in ollama
    assert "pull" in ollama
    assert "Invoke-PipInstall" in install
    assert "Invoke-PipInstall" in update
    assert "--progress-bar" in common


def test_interrupted_download_keeps_partial_and_uses_http_range() -> None:
    common = _read("Common.ps1")
    assert "--continue-at" in common
    assert "Resuming" in common
    assert "The partial file was kept" in common
    assert "RangeRejected" in common
    assert "byte ranges" in common
    assert "416" in common
    wiped = (
        "if (Test-Path -LiteralPath $partial) {\n"
        "        Remove-Item -LiteralPath $partial -Force\n"
        "    }"
    )
    assert wiped not in common
    assert "is already present and the checksum matches." in common
    assert "It was left in place." in common


def test_rerun_keeps_verified_files_and_env() -> None:
    install = _read("install.ps1")
    common = _read("Common.ps1")
    assert "Save-StateManifest" in install
    assert "Kept the existing .env file." in install
    assert "is already present and the checksum matches." in common
    assert "Leaving it in place. Use update.ps1 to change it." in install


def test_existing_user_configuration_is_not_replaced() -> None:
    install = _read("install.ps1")
    assert "ForceConfig" in install
    assert "The existing .env does not keep Ollama and ComfyUI on loopback" in install
    assert "An existing Modelfile differs" in install
    assert "Existing workflow" in install
    assert "Pass -ForceConfig to replace it." in install
    assert "differs and was left unchanged" in install


def test_installer_and_client_do_not_handle_credentials() -> None:
    installer = _all_text().lower()
    client = (ROOT / "client" / "macos" / "generate_asset.py").read_text(encoding="utf-8").lower()
    example = (ROOT / "client" / "macos" / "config.json.example").read_text(encoding="utf-8")
    forbidden = (
        "keychain",
        "token.txt",
        "las_token",
        "las_api_token",
        "new-gatewaytoken",
        "replacetoken",
        "add-generic-password",
        "find-generic-password",
        "mac-client.json",
        "randomnumbergenerator",
        "icacls",
        'add_header("authorization"',
        "bearer ",
    )
    for name in forbidden:
        assert name not in installer, name
        assert name not in client, name
    assert "authorization" not in installer
    payload = json.loads(example)
    assert list(payload) == ["gateway_url"]
    api = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "local_ai_switchboard" / "api").glob("*.py")
    )
    settings = (ROOT / "src" / "local_ai_switchboard" / "config" / "settings.py").read_text(encoding="utf-8")
    assert "authorized(" not in api
    assert "from .auth import" not in api
    assert "access_token" not in settings
    assert not (ROOT / "src" / "local_ai_switchboard" / "api" / "auth.py").exists()


def test_firewall_rule_stays_private_and_local_subnet() -> None:
    common = _read("Common.ps1")
    install = _read("install.ps1")
    rule = next(line for line in common.splitlines() if "New-NetFirewallRule" in line)
    assert "-Profile Private" in rule
    assert "-RemoteAddress LocalSubnet" in rule
    assert "Public" not in rule
    assert "Domain" not in rule
    assert "-LocalPort $ApiPort" in rule
    assert "8787" in install
    assert "needs-admin" in common
    assert "function Start-ElevatedPowerShell" in common
    assert "Verb = 'runas'" in common
    assert r'[\s"()]' in common
    assert "FirewallOnly" in install
    assert "Start-ElevatedPowerShell -ArgumentList $arguments" in install
    assert "Start-Process -FilePath $shell -ArgumentList $arguments -Verb RunAs" not in install
    assert "Start-ElevatedPowerShell -ArgumentList $arguments" in _read("uninstall.ps1")
    assert "portproxy" not in _all_text().lower()
    assert common.count("New-NetFirewallRule") == 1


def test_missing_nvidia_driver_stops_design_profile() -> None:
    install = _read("install.ps1")
    preflight = _between(install, "function Get-MachinePreflight", "function Install-PythonRuntime")
    assert "nvidia-smi did not report a GPU. The Design profile stops" in preflight
    assert "us.download.nvidia.com" not in _all_text().lower()


def test_insufficient_disk_space_stops_before_downloads() -> None:
    install = _read("install.ps1")
    pins = json.loads((INSTALLER / "pins.json").read_text(encoding="utf-8"))
    preflight = _between(install, "function Get-MachinePreflight", "function Install-PythonRuntime")
    assert "design_free_bytes" in preflight
    assert "Get-FreeBytesForPath" in preflight
    assert "$free -lt $required" in preflight
    assert pins["disk"]["design_free_bytes"] == 53687091200
    assert pins["disk"]["basic_free_bytes"] == 21474836480


def test_seven_zip_fallback_is_pinned_and_last() -> None:
    common = _read("Common.ps1")
    expand = _between(common, "function Expand-PinnedArchive", "function Find-SevenZipExecutable")
    assert expand.index("tar.exe") < expand.index("Find-SevenZipExecutable")
    assert "seven_zip" in expand
    assert "7zr.exe" in expand
    assert "Install 7-Zip from https://www.7-zip.org/" not in common


def test_ollama_model_consent_is_not_bypassed() -> None:
    install = _read("install.ps1")
    model = _between(install, "function Install-OllamaModel", "function Install-ComfyModels")
    assert "AcceptUnchecksummedModels" in model
    assert "if (-not $allowed)" in model
    assert "Consent was not recorded for the unchecksummed Ollama model" in model


def test_missing_ollama_profile_is_created_instead_of_aborting() -> None:
    install = _read("install.ps1")
    doctor = _read("doctor.ps1")
    common = _read("Common.ps1")
    model = _between(install, "function Install-OllamaModel", "function Install-ComfyModels")
    assert "function Invoke-CapturedCommand" in common
    assert "function Wait-OllamaApi" in common
    assert "function Get-OllamaApplicationPath" in common
    assert "ollama app.exe" in common
    assert "function Read-FilePrefix" in common
    assert "FileShare]::ReadWrite" in common
    assert "ReadAllBytes" not in install
    assert "ReadAllBytes" not in doctor
    assert "NativeCommandError" in common
    captured = _between(common, "function Invoke-CapturedCommand", "function Invoke-ConsoleCommand")
    assert "2>&1" not in captured
    assert "run.cmd" in captured
    assert "2>&1" not in model
    assert "Checking installed Ollama models." in model
    assert model.index("Wait-OllamaApi") < model.index("Invoke-CapturedCommand")
    assert "already installed. It will not be downloaded again." in model
    assert "The model files were left in place." in model
    assert "Invoke-CapturedCommand" in model
    assert "Creating $($Pins.ollama.profile)" in model
    assert "$createCode = Invoke-ConsoleCommand -FilePath $ollama.Executable -ArgumentList @('create', [string]$Pins.ollama.profile, '-f', $modelFile)" in model
    assert "$pullCode = Invoke-ConsoleCommand -FilePath $ollama.Executable -ArgumentList @('pull', [string]$Pins.ollama.model)" in model
    gateway = _between(install, "function Install-GatewaySoftware", "function Invoke-StackVerification")
    assert "$pipCode = Invoke-PipInstall -Python $venvPython -SourceRoot $SourceRoot" in gateway
    assert "metadata.version('local-ai-switchboard')" in gateway
    assert "$needsInstall = $probe.ExitCode -ne 0" in gateway
    assert "2>$null" not in gateway
    assert gateway.count("Invoke-CapturedCommand") >= 4
    assert "2>&1" not in doctor
    assert "Invoke-CapturedCommand" in doctor


def test_readiness_keeps_a_missing_workflow_out_of_gateway_health() -> None:
    doctor = _read("doctor.ps1")
    install = _read("install.ps1")
    for label in (
        "Gateway operational:",
        "Ollama operational:",
        "ComfyUI operational:",
        "Image workflow:",
        "Image generation: not ready",
    ):
        assert label in doctor
    image_status = _between(doctor, "function Update-ImageWorkflowStatus", "$manifestPath")
    assert "$script:failed" not in image_status
    assert "A missing image workflow does not change gateway readiness." in doctor
    assert "registered, not tested" in doctor
    assert "Invoke-InstallDoctor" in install
    assert "image_generation_tested" in install
    assert "Image generation: ready" in doctor
    assert "does not measure VRAM" in doctor
    assert "$script:gatewayReady = $false" not in image_status


def test_existing_env_port_and_firewall_rule_stay_together() -> None:
    common = _read("Common.ps1")
    install = _read("install.ps1")
    update = _read("update.ps1")
    uninstall = _read("uninstall.ps1")
    assert "function Get-ConfiguredApiPort" in common
    assert "function Get-FirewallRuleName" in common
    assert 'return "LocalAISwitchboard-$ApiPort"' in common
    assert "LocalAISwitchboard-8787" not in common
    assert "Get-ConfiguredApiPort" in install
    assert "was not applied" in install
    assert "Get-ConfiguredApiPort" in update
    assert "http://127.0.0.1:8787/health/live" not in update
    assert "Get-ConfiguredApiPort" in uninstall
    assert "qwen_3_4b_fp4_flux2.safetensors and was not reused" in install


def test_python_runtime_discovery_and_install_contract() -> None:
    common = _read("Common.ps1")
    install = _read("install.ps1")
    runtime = _between(install, "function Install-PythonRuntime", "function Install-OllamaRuntime")
    gateway = _between(install, "function Install-GatewaySoftware", "function Invoke-StackVerification")
    workflow = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
    cases = (ROOT / "tests" / "installer_python_runtime.ps1").read_text(encoding="utf-8")
    cases.encode("ascii")
    _scan_powershell(cases)
    assert "function Find-Python312" in common
    assert "function Test-RejectedPythonPath" in common
    assert "WindowsApps execution alias" in common
    assert "python_embeded" in common
    assert "No suitable Python runtime found" in common
    assert "-0p" in common
    assert "-3.12" not in common
    assert "-c $code" not in common
    assert 'metadata.version("local-ai-switchboard")' not in _all_text()
    assert 'sys.stdout.write("LAS_WORKFLOW' not in common
    assert "las-python-probe-" in common
    assert re.search(r"(?im)^[ \t]*\$probeCommand\s*=", common) is None
    assert "py.exe -3" not in install
    assert "Prefix = @('-3.12')" not in common
    assert "TargetDir=" in common
    assert "InstallLauncherAllUsers=0" in common
    assert "SHA256" in common
    assert "python_executable" in install
    assert runtime.index("Resolve-InstallerPython") < runtime.index("python-3.12.10-amd64.exe")
    assert "Kept the existing Python runtime." in runtime
    assert "-m venv" in gateway
    assert "Invoke-GatewayVenvCreate" in gateway
    assert "py -3" not in gateway
    assert "installer_python_runtime.ps1" in workflow
    assert "PSVersion.Major -ne 5" in workflow
    for name in (
        "No Python installed",
        "Python launcher without a runtime",
        "Python 3.11 only",
        "Compatible Python 3.12",
        "Compatible Python 3.13",
        "Python available only through an absolute path",
        "WindowsApps execution alias",
        "Python installation success",
        "Python installation failure",
        "Non-zero installer exit code",
        "Missing pip",
        "Failed venv creation",
        "Existing valid venv",
        "ComfyUI embedded Python only",
        "Interrupted installation recovery",
    ):
        assert name in cases


def test_image_pipeline_verifier_does_not_download_or_start_a_second_scheduler() -> None:
    verify = _read("verify-image-pipeline.ps1")
    install = _read("install.ps1")
    update = _read("update.ps1")
    doctor = _read("doctor.ps1")
    start = _read("start.ps1")
    assert "Save-PinnedFile" not in verify
    assert "huggingface" not in verify.lower()
    assert "GpuScheduler" not in verify
    assert "main.py" not in verify
    assert "image-pipeline\\latest" in verify
    assert "/latest/" not in verify
    assert "workflows\\candidates" in install
    assert "flux2-klein-t2i" in install
    assert "Model files were not downloaded" in install
    assert "workflows\\candidates" in update
    assert "Model files were not downloaded" in update
    assert "verify-image-pipeline.ps1" in install
    assert "verify-image-pipeline.ps1" in update
    assert "ReserveVramMb" in start
    assert "--listen" not in start
    assert "image-pipeline.json" in doctor
    assert "verified on this machine" in doctor
    assert "does not claim the model fits fully in VRAM" in doctor
    assert "flux2-klein-4b" in verify
    assert "generated-image.png" in verify
    assert "environment.json" in verify
    assert "gateway.log" in verify
    assert "comfyui.log" in verify
    assert "yyyyMMdd-HHmmss" in verify
    assert "qwen3.5:4b" in verify
    assert "qwen_3_4b.safetensors" in verify
    assert "/v1/assets" in verify
    assert "/v1/chat/completions" in verify
    assert "scheduler state" in verify
    assert "exit 2" in verify
    assert "active_jobs" in verify
    assert "&&" not in verify
    assert "?." not in verify
    assert "taskkill" not in verify
    assert "Stop-Process" not in verify
    assert "Invoke-PipInstall" not in verify
    assert "function Invoke-QuietNative" in verify
    assert "las-pipeline-" in verify
    assert "ArgumentList @('-c', $Code)" not in verify
    assert "function Get-ErrorDetail" in verify
    assert "Get-RunningStage" in verify
    assert "ErrorActionPreference = 'Continue'" in verify
    launcher = (INSTALLER / "verify-image-pipeline.cmd").read_text(encoding="ascii")
    assert launcher.startswith("@echo off")
    assert "powershell.exe" in launcher
    assert "pwsh" not in launcher
    assert "verify-image-pipeline.ps1" in launcher
    assert "-Width 768" in launcher
    assert "-Height 768" in launcher
    assert r"%USERPROFILE%\AI\local-ai-switchboard" in launcher
    assert "pause" in launcher
    assert "verify-image-pipeline.cmd" in install
    assert "verify-image-pipeline.cmd" in update
