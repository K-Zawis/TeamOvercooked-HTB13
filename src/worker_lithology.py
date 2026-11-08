"""Consume Redis lithology tasks and publish document-local ML results."""

from __future__ import annotations

import argparse
import logging
import socket
import uuid

lazy from .lithology.task import (
    LithologyTask,
    LithologyTaskResult,
    process_lithology_task,
)
lazy from .lithology.text import TextRecognizer
lazy from .lithology.types import LegendPattern
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
    """Process and publish one task delivered by ``listen_tasks``."""
    task = LithologyTask.from_payload(payload)
    result = process_lithology_task(
        task,
        recognizer=recognizer,
        legend_cache=legend_cache,
    )
    serialized = result.to_dict()
    client.save_legend_map(result.log_id, serialized["legend"])
    client.append_raw_intervals(result.log_id, serialized["intervals"])
    client.complete_lith_chunk(
        result.log_id,
        message_id,
        task.image_path,
        list(task.column_bbox),
    )
    return result


def consume_tasks(
    client: PipelineRedisClient,
    *,
    consumer_name: str,
    recognizer: TextRecognizer | None = None,
    block_ms: int = 2000,
) -> None:
    """Process the lithology stream exposed by ``PipelineRedisClient``."""
    legend_cache: dict[tuple, tuple[LegendPattern, ...]] = {}
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-host", default=None)
    parser.add_argument("--redis-port", type=int, default=None)
    parser.add_argument("--redis-db", type=int, default=0)
    parser.add_argument("--consumer", default=None)
    parser.add_argument("--block-ms", type=int, default=2000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    client = PipelineRedisClient(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
    )
    client.init_consumer_groups()
    consumer_name = args.consumer or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    consume_tasks(
        client,
        consumer_name=consumer_name,
        block_ms=args.block_ms,
    )


if __name__ == "__main__":
    main()
