from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
import socket
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
        """Filters OCR depth points to enforce strictly increasing depth down the log.

        Args:
            y_to_depth_map (Dict[int, float]): Mapping of vertical pixel Y positions to detected depths.

        Returns:
            Tuple[np.ndarray, np.ndarray]: Monotonically filtered (y_coords, depth_values) arrays.
        """
        if len(y_to_depth_map) < 2:
            y_coords = np.array(list(y_to_depth_map.keys()), dtype=np.float64)
            depth_vals = np.array(list(y_to_depth_map.values()), dtype=np.float64)
            return y_coords, depth_vals

        sorted_points = sorted(y_to_depth_map.items(), key=lambda item: item[0])
        valid_y: List[float] = []
        valid_depths: List[float] = []

        last_depth = -float("inf")
        for y_pixel, depth_val in sorted_points:
            # Physical well logs strictly increase in depth with vertical pixel Y
            if depth_val > last_depth:
                valid_y.append(float(y_pixel))
                valid_depths.append(float(depth_val))
                last_depth = depth_val
            else:
                logger.warning(
                    "Filtered non-monotonic depth OCR outlier: Y=%s depth=%s (previous=%s)",
                    y_pixel,
                    depth_val,
                    last_depth,
                )

        return (
            np.array(valid_y, dtype=np.float64),
            np.array(valid_depths, dtype=np.float64),
        )

    def interpolate_depth_curve(
        self,
        y_query: np.ndarray,
        ref_y: np.ndarray,
        ref_depths: np.ndarray,
    ) -> np.ndarray:
        """Interpolates and extrapolates pixel Y coordinates to measured depth values.

        Args:
            y_query (np.ndarray): Array of vertical pixel positions to interpolate.
            ref_y (np.ndarray): Anchor pixel Y coordinates.
            ref_depths (np.ndarray): Anchor measured depth values.

        Returns:
            np.ndarray: Array of interpolated measured depths aligned with y_query.
        """
        if len(ref_y) == 0:
            # Fallback if no OCR depth markers were captured
            return y_query

        if len(ref_y) == 1:
            # Single depth anchor fallback (assumes nominal 0.1 depth units per pixel)
            return ref_depths[0] + (y_query - ref_y[0]) * 0.1

        # Interior linear interpolation
        depths = np.interp(y_query, ref_y, ref_depths)

        # Linear boundary extrapolation
        dy_top = ref_y[1] - ref_y[0]
        if dy_top > 0:
            slope_top = (ref_depths[1] - ref_depths[0]) / dy_top
            top_mask = y_query < ref_y[0]
            depths[top_mask] = (
                ref_depths[0] + (y_query[top_mask] - ref_y[0]) * slope_top
            )

        dy_bottom = ref_y[-1] - ref_y[-2]
        if dy_bottom > 0:
            slope_bottom = (ref_depths[-1] - ref_depths[-2]) / dy_bottom
            bottom_mask = y_query > ref_y[-1]
            depths[bottom_mask] = (
                ref_depths[-1] + (y_query[bottom_mask] - ref_y[-1]) * slope_bottom
            )

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

        for item in raw_data:
            cluster_id = item.get("cluster_id")
            lithology = item.get("lithology")
            if not lithology and cluster_id:
                lithology = legend_map.get(cluster_id, "UNKNOWN")

            intervals.append(
                LithologyInterval(
                    top_y=int(item["top_y"]),
                    bottom_y=int(item["bottom_y"]),
                    lithology=lithology,
                    confidence=float(item.get("confidence", 0.0)),
                    cluster_id=cluster_id,
                )
            )

        # Merge touching/overlapping intervals with identical classifications
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
        calculated_depths = self.interpolate_depth_curve(y_coords, ref_y, ref_depths)

        top_depths = calculated_depths[0::2]
        bottom_depths = calculated_depths[1::2]

        export_path = self.output_dir / f"{log_id}.csv"
        with export_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writeheader()
            for i, interval in enumerate(stitched_intervals):
                writer.writerow(
                    {
                        "top_depth": f"{top_depths[i]:.3f}",
                        "bottom_depth": f"{bottom_depths[i]:.3f}",
                        "lithology": interval.lithology or "UNKNOWN",
                        "confidence": f"{interval.confidence:.3f}",
                        "top_y": interval.top_y,
                        "bottom_y": interval.bottom_y,
                        "cluster_id": interval.cluster_id or "",
                    }
                )

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
                self.client.set_document_status(log_id, LogStatus.FAILED)


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
