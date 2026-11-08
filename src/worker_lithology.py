"""Consume Redis lithology tasks and publish document-local ML results."""

from __future__ import annotations

import logging
import os
import socket
import uuid

from .lithology.task import (
    LithologyTask,
    LithologyTaskResult,
    process_lithology_task,
)
from .lithology.text import TextRecognizer
from .lithology.types import LegendPattern
from .redis_client import PipelineRedisClient

logger = logging.getLogger(__name__)


def handle_task(
    client: PipelineRedisClient,
    message_id: str,
    payload: dict,
    *,
    recognizer: TextRecognizer | None = None,
    legend_cache: dict[tuple, tuple[LegendPattern, ...]] | None = None,
) -> LithologyTaskResult:
    """Process and publish one task delivered by ``listen_tasks``.

    Args:
        client (PipelineRedisClient): Active Redis client interface.
        message_id (str): Redis Stream entry ID.
        payload (dict): Parsed task payload dictionary from stream.
        recognizer (TextRecognizer | None, optional): OCR text recognizer instance. Defaults to None.
        legend_cache (dict[tuple, tuple[LegendPattern, ...]] | None, optional): Cached legend patterns. Defaults to None.

    Returns:
        LithologyTaskResult: Output ML result containing segmented intervals and legend maps.
    """
    task = LithologyTask.from_payload(payload)
    result = process_lithology_task(
        task,
        recognizer=recognizer,
        legend_cache=legend_cache,
    )
    serialized = result.to_dict()

    if serialized.get("legend"):
        client.save_legend_map(result.log_id, serialized["legend"])

    client.append_raw_intervals(result.log_id, serialized["intervals"])

    # Pass slice_bounds to ensure idempotent slice tracking in Redis sets
    client.complete_lith_chunk(
        log_id=result.log_id,
        msg_id=message_id,
        image_path=task.image_path,
        column_bbox=list(task.column_bbox),
        slice_bounds=(result.y_start, result.y_end),
    )
    return result


def consume_tasks(
    client: PipelineRedisClient,
    *,
    consumer_name: str,
    recognizer: TextRecognizer | None = None,
    block_ms: int = 2000,
) -> None:
    """Process the lithology stream exposed by ``PipelineRedisClient``.

    Args:
        client (PipelineRedisClient): Active Redis client interface.
        consumer_name (str): Unique consumer name registered in Redis consumer group.
        recognizer (TextRecognizer | None, optional): OCR engine instance. Defaults to None.
        block_ms (int, optional): Blocking socket timeout in milliseconds. Defaults to 2000.
    """
    legend_cache: dict[tuple, tuple[LegendPattern, ...]] = {}
    logger.info("Lithology worker '%s' listening for tasks on stream...", consumer_name)

    for message_id, payload in client.listen_tasks(
        client.STREAM_LITH,
        client.GROUP_LITH,
        consumer_name,
        block_ms=block_ms,
    ):
        try:
            result = handle_task(
                client,
                message_id,
                payload,
                recognizer=recognizer,
                legend_cache=legend_cache,
            )
        except Exception:
            # Person 1's consumer-group recovery policy owns retries. A failed
            # task is deliberately not completed or acknowledged here.
            logger.exception("Lithology task %r failed", message_id)
            continue

        logger.info(
            "Lithology task %r: log=%s rows=%s:%s intervals=%s",
            message_id,
            result.log_id,
            result.y_start,
            result.y_end,
            len(result.intervals),
        )


def main() -> None:
    """Initializes logging and starts task consumption configured via environment variables."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    client = PipelineRedisClient()
    client.init_consumer_groups()

    consumer_name = os.getenv(
        "CONSUMER_NAME",
        f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
    )
    block_ms = int(os.getenv("BLOCK_MS", 2000))

    consume_tasks(
        client,
        consumer_name=consumer_name,
        block_ms=block_ms,
    )


if __name__ == "__main__":
    main()
