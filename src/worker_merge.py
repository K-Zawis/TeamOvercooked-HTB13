"""Final reduction worker: interpolates depth, merges lithology seams, and exports results."""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
import socket
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import uuid

import numpy as np

from .lithology.intervals import merge_intervals
from .lithology.types import LithologyInterval
from .redis_client import LogStatus, PipelineRedisClient

logger = logging.getLogger(__name__)


class WellLogMergerWorker:
    """Consolidates cross-chunk well log intervals, computes depth models, and exports CSVs."""

    CSV_FIELDS: Tuple[str, ...] = (
        "top_depth",
        "bottom_depth",
        "lithology",
        "confidence",
        "top_y",
        "bottom_y",
        "cluster_id",
    )

    def __init__(
        self,
        client: Optional[PipelineRedisClient] = None,
        output_dir: Optional[str | Path] = None,
        consumer_name: Optional[str] = None,
        block_ms: int = 2000,
    ) -> None:
        """Initializes the merger worker with Redis connectivity and export targets.

        Args:
            client (Optional[PipelineRedisClient], optional): Active Redis interface. Defaults to None.
            output_dir (Optional[str | Path], optional): Target directory for CSV exports. Defaults to None.
            consumer_name (Optional[str], optional): Unique consumer identifier. Defaults to None.
            block_ms (int, optional): Blocking poll timeout in milliseconds. Defaults to 2000.
        """
        self.client = client or PipelineRedisClient()
        self.output_dir = Path(output_dir or os.getenv("OUTPUT_DIR", "/output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.consumer_name = consumer_name or os.getenv(
            "CONSUMER_NAME",
            f"merger-{socket.gethostname()}-{uuid.uuid4().hex[:6]}",
        )
        self.block_ms = block_ms

    # -------------------------------------------------------------------------
    # DEPTH CALIBRATION & MONOTONIC INTERPOLATION
    # -------------------------------------------------------------------------

    def filter_monotonic_depth_points(
        self,
        y_to_depth_map: Dict[int, float],
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Filters OCR depth points using global three-anchor support.

        Eliminates scale track noise (e.g. 0-100 markings) and OCR blunders
        by enforcing consensus with the dominant well log pixel pitch.

        Args:
            y_to_depth_map (Dict[int, float]): Mapping of vertical pixel Y positions to detected depths.

        Returns:
            Tuple[np.ndarray, np.ndarray]: Monotonically filtered (y_coords, depth_values) arrays.
        """
        from types import SimpleNamespace
        from .depth.legacy_ocr import supported_anchors
        sorted_items = sorted(y_to_depth_map.items(), key=lambda item: float(item[0]))
        raw_y = np.array([float(y) for y, d in sorted_items], dtype=np.float64)
        raw_d = np.array([float(d) for y, d in sorted_items], dtype=np.float64)
        if (not np.all(np.isfinite(raw_y)) or not np.all(np.isfinite(raw_d))
                or np.any(np.diff(raw_y) <= 0)):
            raise ValueError("Depth anchors must have finite distinct coordinates and values")
        if len(raw_y) > 1 and np.any(np.diff(raw_d) <= 0) and np.ptp(raw_y) <= 300:
            raise ValueError("Nonmonotonic short-span depth anchors")
        # Apply the legacy support gates once over all independent observations.
        # Pixel residuals are unit-independent; never assume feet or a pitch range.
        anchors = [SimpleNamespace(y=float(y), depth=float(d), confidence=1., text=str(d))
                   for y, d in zip(raw_y, raw_d)]
        accepted = supported_anchors(anchors, 5.)
        return (np.array([a.y for a in accepted], dtype=np.float64),
                np.array([a.depth for a in accepted], dtype=np.float64))

    def interpolate_depth_curve(
        self,
        y_query: np.ndarray,
        ref_y: np.ndarray,
        ref_depths: np.ndarray,
    ) -> np.ndarray:
        """Interpolate within anchor support; unsupported queries return NaN.

        Args:
            y_query (np.ndarray): Array of vertical pixel positions to interpolate.
            ref_y (np.ndarray): Anchor pixel Y coordinates.
            ref_depths (np.ndarray): Anchor measured depth values.

        Returns:
            np.ndarray: Array of interpolated measured depths aligned with y_query.
        """
        y_query = np.asarray(y_query, dtype=np.float64)
        ref_y = np.asarray(ref_y, dtype=np.float64)
        ref_depths = np.asarray(ref_depths, dtype=np.float64)
        if (ref_y.ndim != 1 or ref_depths.ndim != 1
                or len(ref_y) != len(ref_depths) or len(ref_y) < 2
                or not np.all(np.isfinite(ref_y))
                or not np.all(np.isfinite(ref_depths))
                or not np.all(np.isfinite(y_query))
                or np.any(np.diff(ref_y) <= 0)
                or np.any(np.diff(ref_depths) <= 0)):
            raise ValueError("Need two finite strictly increasing depth anchors")

        # No pitch is inferred beyond the observed calibration support.
        depths = np.interp(y_query, ref_y, ref_depths, left=np.nan, right=np.nan)
        return depths

    # -------------------------------------------------------------------------
    # LITHOLOGY RECONCILIATION & CROSS-CHUNK SEAM STITCHING
    # -------------------------------------------------------------------------

    def reconcile_intervals(self, log_id: str) -> List[LithologyInterval]:
        """Retrieves and stitches segmented intervals across 2,000 px slice seams.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            List[LithologyInterval]: Unified sequence of stitched lithology intervals.
        """
        raw_data = self.client.get_raw_intervals(log_id)
        if not raw_data:
            raise ValueError(
                f"No lithology intervals found in Redis for log '{log_id}'"
            )

        legend_map = self.client.get_legend_map(log_id)
        intervals: List[LithologyInterval] = []

        seen = set()
        for item in raw_data:
            cluster_id = item.get("cluster_id")
            lithology = item.get("lithology")
            if not lithology and cluster_id:
                lithology = legend_map.get(cluster_id, "UNKNOWN")

            key = (item["top_y"], item["bottom_y"], lithology,
                   float(item.get("confidence", 0.0)), cluster_id)
            if key in seen:
                continue
            seen.add(key)
            intervals.append(
                LithologyInterval(
                    top_y=item["top_y"],
                    bottom_y=item["bottom_y"],
                    lithology=lithology,
                    confidence=float(item.get("confidence", 0.0)),
                    cluster_id=cluster_id,
                )
            )

        return merge_intervals(intervals)

    # -------------------------------------------------------------------------
    # DOCUMENT REDUCTION & DELIVERABLE EXPORT
    # -------------------------------------------------------------------------

    def export_merged_document(self, log_id: str) -> Path:
        """Executes full document reduction, maps the depth curve, and outputs CSV.

        Args:
            log_id (str): Target well log identifier.

        Returns:
            Path: Filesystem path to the exported CSV deliverable.
        """
        stitched_intervals = self.reconcile_intervals(log_id)
        depth_map = self.client.get_depth_points(log_id)
        ref_y, ref_depths = self.filter_monotonic_depth_points(depth_map)

        y_coords = np.array(
            [y for inv in stitched_intervals for y in (inv.top_y, inv.bottom_y)],
            dtype=np.float64,
        )
        calculated_depths = (
            self.interpolate_depth_curve(y_coords, ref_y, ref_depths)
            if len(ref_y) >= 2 else np.full(y_coords.shape, np.nan)
        )

        top_depths = calculated_depths[0::2]
        bottom_depths = calculated_depths[1::2]

        if (not isinstance(log_id, str) or not log_id
                or Path(log_id).name != log_id or "\\" in log_id):
            raise ValueError("log_id must be a filename component")
        if np.any(np.isinf(calculated_depths)) or np.any(bottom_depths <= top_depths):
            raise ValueError("Invalid calculated depths")
        export_path = self.output_dir / f"{log_id}.csv"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                             dir=self.output_dir, delete=False) as file:
                temporary_path = Path(file.name)
                writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
                writer.writeheader()
                for i, interval in enumerate(stitched_intervals):
                    writer.writerow(
                        {
                            "top_depth": f"{top_depths[i]:.3f}" if np.isfinite(top_depths[i]) else None,
                            "bottom_depth": f"{bottom_depths[i]:.3f}" if np.isfinite(bottom_depths[i]) else None,
                            "lithology": interval.lithology or "UNKNOWN",
                            "confidence": f"{interval.confidence:.3f}",
                            "top_y": interval.top_y,
                            "bottom_y": interval.bottom_y,
                            "cluster_id": interval.cluster_id or "",
                        }
                    )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, export_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return export_path

    # -------------------------------------------------------------------------
    # TASK CONSUMPTION & LIFECYCLE MANAGEMENT
    # -------------------------------------------------------------------------

    def process_merge_task(self, message_id: str, payload: Dict[str, Any]) -> None:
        """Processes a single merge task event delivered by the stream.

        Args:
            message_id (str): Redis Stream entry ID.
            payload (Dict[str, Any]): Parsed task payload dictionary.
        """
        log_id = payload["log_id"]
        logger.info("Starting final reduction merge for log '%s'...", log_id)

        destination = self.output_dir / f"{log_id}.csv"
        if (self.client.get_document_status(log_id) != LogStatus.COMPLETED
                or not destination.is_file()):
            destination = self.export_merged_document(log_id)
        logger.info("Successfully exported merged log '%s' to %s", log_id, destination)

        self.client.set_document_status(log_id, LogStatus.COMPLETED)
        self.client.ack_task(
            self.client.STREAM_MERGE,
            self.client.GROUP_MERGE,
            message_id,
        )

    def run(self) -> None:
        """Starts continuous task consumption on the merge stream."""
        self.client.init_consumer_groups()
        logger.info(
            "WellLogMergerWorker '%s' listening for tasks on stream '%s'...",
            self.consumer_name,
            self.client.STREAM_MERGE,
        )

        for message_id, payload in self.client.listen_tasks(
            self.client.STREAM_MERGE,
            self.client.GROUP_MERGE,
            self.consumer_name,
            block_ms=self.block_ms,
        ):
            log_id = payload.get("log_id", "unknown")
            try:
                self.process_merge_task(message_id, payload)
            except Exception:
                logger.exception(
                    "Merge task %r for log '%s' failed", message_id, log_id
                )
                # Keep the task pending and its existing status recoverable.
                # Redelivery/claiming remains the shared broker's responsibility.


def main() -> None:
    """Initializes logging and launches the WellLogMergerWorker service."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    worker = WellLogMergerWorker()
    worker.run()


if __name__ == "__main__":
    main()
