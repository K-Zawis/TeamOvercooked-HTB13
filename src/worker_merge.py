"""Final reduction worker: interpolates depth, merges lithology seams, and exports results."""

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
        """Filters OCR depth points using 1D RANSAC consensus on physical pitch.

        Eliminates scale track noise (e.g. 0-100 markings) and OCR blunders
        by enforcing consensus with the dominant well log pixel pitch.

        Args:
            y_to_depth_map (Dict[int, float]): Mapping of vertical pixel Y positions to detected depths.

        Returns:
            Tuple[np.ndarray, np.ndarray]: Monotonically filtered (y_coords, depth_values) arrays.
        """
        if len(y_to_depth_map) < 2:
            y_coords = np.array(list(y_to_depth_map.keys()), dtype=np.float64)
            depth_vals = np.array(list(y_to_depth_map.values()), dtype=np.float64)
            return y_coords, depth_vals

        sorted_items = sorted(y_to_depth_map.items(), key=lambda item: item[0])
        raw_y = np.array([float(item[0]) for item in sorted_items], dtype=np.float64)
        raw_d = np.array([float(item[1]) for item in sorted_items], dtype=np.float64)
        num_points = len(raw_y)

        # 1. Estimate dominant physical pitch (ft/pixel) via median pairwise slope
        candidate_slopes: List[float] = []
        for i in range(num_points):
            for j in range(i + 1, min(i + 20, num_points)):
                delta_y = raw_y[j] - raw_y[i]
                delta_d = raw_d[j] - raw_d[i]
                if 300 < delta_y < 10000 and delta_d > 0:
                    slope = delta_d / delta_y
                    # Standard well log pitch boundaries: 0.05 to 2.0 ft/pixel
                    if 0.05 <= slope <= 2.0:
                        candidate_slopes.append(slope)

        if not candidate_slopes:
            logger.warning(
                "No consistent depth slope detected; falling back to raw points"
            )
            return raw_y, raw_d

        median_slope = float(np.median(candidate_slopes))
        logger.info(
            "Estimated log pitch: %.4f ft/pixel (%.1f pixels per 100 ft)",
            median_slope,
            100.0 / median_slope if median_slope > 0 else 0.0,
        )

        # 2. 1D RANSAC to identify the intercept with maximum inlier consensus
        candidate_intercepts = raw_d - median_slope * raw_y
        best_inlier_mask = np.zeros(num_points, dtype=bool)
        max_inlier_count = 0

        # Typical well log grid lines are spaced 100 ft apart; 50 ft tolerance prevents grid aliasing
        tolerance = 50.0
        for test_intercept in candidate_intercepts:
            inlier_mask = np.abs(candidate_intercepts - test_intercept) < tolerance
            inlier_count = int(np.sum(inlier_mask))
            if inlier_count > max_inlier_count:
                max_inlier_count = inlier_count
                best_inlier_mask = inlier_mask

        inlier_y = raw_y[best_inlier_mask]
        inlier_d = raw_d[best_inlier_mask]

        # 3. Final safety check: enforce strict monotonicity on remaining inliers
        valid_y: List[float] = []
        valid_depths: List[float] = []
        last_accepted_depth = -float("inf")

        sort_indices = np.argsort(inlier_y)
        for idx in sort_indices:
            y_val = float(inlier_y[idx])
            d_val = float(inlier_d[idx])
            if d_val > last_accepted_depth:
                valid_y.append(y_val)
                valid_depths.append(d_val)
                last_accepted_depth = d_val

        rejected_count = num_points - len(valid_y)
        logger.info(
            "Depth consensus filtering: retained %d inliers, rejected %d noise points/outliers",
            len(valid_y),
            rejected_count,
        )

        return np.array(valid_y, dtype=np.float64), np.array(
            valid_depths, dtype=np.float64
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
            return y_query

        if len(ref_y) == 1:
            return ref_depths[0] + (y_query - ref_y[0]) * 0.1

        depths = np.interp(y_query, ref_y, ref_depths)

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
