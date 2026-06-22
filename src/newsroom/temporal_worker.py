"""Temporal worker + client helpers (plan §6, Phase 2A).

A single-file entry point that hosts :class:`~newsroom.workflows.PipelineWorkflow`
and every activity it calls, connected to the local Temporal dev server
(``docker compose up temporal``). The activities are synchronous and wrap the
existing pipeline functions, so the worker runs them in a thread pool.

Used by two CLI commands:

* ``newsroom worker``         → :func:`run_worker` (long-running host)
* ``newsroom run-temporal``   → :func:`start_pipeline` (submit one run)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from .config import settings
from .workflows import ACTIVITIES, PipelineWorkflow, RunConfig

log = logging.getLogger(__name__)

#: Sync activities run in threads; this bounds in-flight activity concurrency.
DEFAULT_MAX_WORKERS = 8


async def connect(client: Client | None = None) -> Client:
    """Connect to the configured Temporal server (or pass an existing client through)."""
    if client is not None:
        return client
    log.info(
        "connecting to Temporal host=%s namespace=%s",
        settings.temporal_host, settings.temporal_namespace,
    )
    return await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )


async def run_worker(
    *, max_workers: int = DEFAULT_MAX_WORKERS, client: Client | None = None
) -> None:
    """Start the worker and block, serving the pipeline task queue until cancelled."""
    client = await connect(client)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[PipelineWorkflow],
            activities=ACTIVITIES,
            activity_executor=executor,
        )
        log.info(
            "worker ready: queue=%s activities=%d (max_workers=%d) — Ctrl-C to stop",
            settings.temporal_task_queue, len(ACTIVITIES), max_workers,
        )
        await worker.run()


async def start_pipeline(
    cfg: RunConfig, *, client: Client | None = None, wait: bool = True
) -> dict | str:
    """Submit one :class:`PipelineWorkflow` run.

    Returns the workflow result dict when ``wait`` is True, otherwise the started
    workflow id (so the caller can watch it in the Temporal UI on :8233).
    """
    client = await connect(client)
    workflow_id = f"pipeline-{cfg.source_id}-{cfg.article_type}-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        PipelineWorkflow.run,
        cfg,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )
    log.info("submitted workflow id=%s", handle.id)
    if wait:
        return await handle.result()
    return handle.id


def main() -> None:
    """``python -m newsroom.temporal_worker`` — run the worker with INFO logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
