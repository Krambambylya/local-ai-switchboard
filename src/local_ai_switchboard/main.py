"""Application factory and command line entry point."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from local_ai_switchboard import __version__
from local_ai_switchboard.api.assets import router as assets_router
from local_ai_switchboard.api.chat import router as chat_router
from local_ai_switchboard.api.health import router as health_router
from local_ai_switchboard.api.studio import packaged_studio_dir
from local_ai_switchboard.api.studio import router as studio_router
from local_ai_switchboard.api.middleware import RequestContextMiddleware
from local_ai_switchboard.config.settings import Settings
from local_ai_switchboard.gpu.monitor import NvidiaGpuMonitor
from local_ai_switchboard.gpu.nvidia_smi import query_nvidia_smi
from local_ai_switchboard.providers.base import ProviderKind, ProviderProtocol
from local_ai_switchboard.providers.comfyui import ComfyUIProvider
from local_ai_switchboard.providers.ollama import OllamaProvider
from local_ai_switchboard.scheduler.coordinator import GpuScheduler
from local_ai_switchboard.scheduler.model import SchedulerConfig
from local_ai_switchboard.scheduler.process import (
    acquire_process_lock,
    enforce_single_worker,
    release_process_lock,
)
from local_ai_switchboard.services.logging import configure_logging
from local_ai_switchboard.services.preflight import GpuProbe, run_preflight
from local_ai_switchboard.storage.jobs import JobStore, image_job_listener


@asynccontextmanager
async def _lifespan(app: FastAPI):
    scheduler: GpuScheduler = app.state.scheduler
    lock = acquire_process_lock(app.state.settings.resolved_storage_dir() / "scheduler.lock")
    try:
        await scheduler.start()
        yield
    finally:
        try:
            await scheduler.stop()
        finally:
            provider = getattr(app.state, "ollama_provider", None)
            if isinstance(provider, OllamaProvider):
                await provider.aclose()
            comfy = getattr(app.state, "comfy_provider", None)
            if isinstance(comfy, ComfyUIProvider):
                await comfy.aclose()
            store = getattr(app.state, "job_store", None)
            if isinstance(store, JobStore):
                store.close()
            release_process_lock(lock)


def create_app(
    settings: Settings | None = None,
    gpu_probe: GpuProbe | None = None,
    providers: dict[ProviderKind, ProviderProtocol] | None = None,
    scheduler: GpuScheduler | None = None,
    studio_dir: Path | None = None,
) -> FastAPI:
    """Build the gateway app around one in-process GPU scheduler."""

    resolved = settings or Settings()
    configure_logging(resolved)
    monitor = NvidiaGpuMonitor(gpu_probe or query_nvidia_smi)
    selected = providers or _default_providers(resolved)
    active_scheduler = scheduler or GpuScheduler(
        config=SchedulerConfig.from_settings(resolved),
        providers=selected,
        monitor=monitor,
    )
    app = FastAPI(
        title="Local AI Switchboard",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.gpu_probe = gpu_probe or query_nvidia_smi
    output_root = resolved.resolved_storage_dir() / "outputs" / "images"
    store = JobStore(resolved.resolved_storage_dir() / "switchboard.db")
    store.reconcile_completed_outputs(output_root)
    store.recover_interrupted()
    if resolved.output_retention_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=resolved.output_retention_days)
        store.sweep(output_root, cutoff)
    active_scheduler.set_listener(image_job_listener(store, output_root))
    app.state.scheduler = active_scheduler
    app.state.job_store = store
    app.state.output_root = output_root
    llm = selected.get(ProviderKind.LLM)
    image = selected.get(ProviderKind.IMAGE)
    app.state.ollama_provider = llm if isinstance(llm, OllamaProvider) else None
    app.state.comfy_provider = image if isinstance(image, ComfyUIProvider) else None
    app.state.studio_dir = studio_dir if studio_dir is not None else packaged_studio_dir()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(assets_router)
    app.include_router(studio_router)
    return app


def _default_providers(settings: Settings) -> dict[ProviderKind, ProviderProtocol]:
    """Ollama and ComfyUI on their configured loopback URLs."""

    return {
        ProviderKind.LLM: OllamaProvider(str(settings.ollama_url)),
        ProviderKind.IMAGE: ComfyUIProvider.from_settings(settings),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run preflight or serve the health endpoints."""

    parser = argparse.ArgumentParser(prog="local-ai-switchboard")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="check configuration and GPU diagnostics")
    commands.add_parser("serve", help="run the single-worker gateway HTTP server")
    args = parser.parse_args(list(argv) if argv is not None else None)

    settings = Settings()
    configure_logging(settings)
    if args.command == "preflight":
        report = run_preflight(settings)
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.ready else 1

    enforce_single_worker()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
        log_config=None,
    )
    return 0
