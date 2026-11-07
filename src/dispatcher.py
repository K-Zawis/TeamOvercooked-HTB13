import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from src.redis_client import PipelineRedisClient, LogStatus


class LogDispatcher:
    """
    Parses JSON bounding box files, partitions column heights into slices,
    and initializes task distribution streams in Redis.
    """

    def __init__(
        self,
        data_dir: Optional[str | Path] = None,
        slice_height: Optional[int] = None,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        client: Optional[PipelineRedisClient] = None,
    ) -> None:
        # File System Config
        self.data_dir = Path(data_dir or os.getenv("DATA_DIR", "./dataset")).expanduser().resolve()
        self.images_dir = self.data_dir / "images"
        self.annotations_dir = self.data_dir / "bbox_annotation"
        self.slice_height = int(slice_height if slice_height is not None else os.getenv("SLICE_HEIGHT", 2000))
        if self.slice_height <= 0:
            raise ValueError("slice_height must be positive")

        # Redis Connection Config
        self.redis_host = redis_host or os.getenv("REDIS_HOST", "localhost")
        self.redis_port = int(redis_port or os.getenv("REDIS_PORT", 6379))

        # Client Initialization
        self.client = client or PipelineRedisClient(
            host=self.redis_host,
            port=self.redis_port,
        )
        self.client.init_consumer_groups()

    def _generate_slices(self, start_y: int, end_y: int) -> List[Tuple[int, int]]:
        """Splits a vertical range into [y_start, y_end] intervals."""
        slices = []
        for y in range(start_y, end_y, self.slice_height):
            slices.append((y, min(y + self.slice_height, end_y)))
        return slices

    def dispatch_single_log(self, json_path: Path) -> Dict[str, Any]:
        """Parses one bounding box JSON and pushes all chunk tasks to Redis."""
        with open(json_path, "r") as f:
            data = json.load(f)

        image_name = data["image_name"]
        image_path = self.images_dir / image_name
        # A prefix can identify several distinct scans (e.g. log01/log02 or
        # multiple Rio_* wells). The full annotation stem identifies a document.
        log_id = json_path.stem

        bboxes = data.get("bounding_boxes", {})
        depth_bbox = bboxes.get("depth_column")
        lith_bbox = bboxes.get("interpreted_lithology_column")
        legend_bbox = bboxes.get("lithology_legend")

        if not depth_bbox or not lith_bbox:
            raise ValueError(f"Missing required columns in {json_path.name}")

        depth_slices = self._generate_slices(depth_bbox[1], depth_bbox[3])
        lith_slices = self._generate_slices(lith_bbox[1], lith_bbox[3])

        # Initialize document metadata with strict LogStatus enum
        self.client.set_document_meta(
            log_id=log_id,
            total_depth=len(depth_slices),
            total_lith=len(lith_slices),
            image_name=image_name,
            status=LogStatus.PROCESSING,
        )

        # Dispatch depth chunk tasks with typed parameters
        for y_start, y_end in depth_slices:
            self.client.push_depth_task(
                log_id=log_id,
                image_path=image_path,
                bbox=depth_bbox,
                y_start=y_start,
                y_end=y_end,
            )

        # Dispatch lithology chunk tasks with typed parameters
        for y_start, y_end in lith_slices:
            self.client.push_lith_task(
                log_id=log_id,
                image_path=image_path,
                column_bbox=lith_bbox,
                legend_bbox=legend_bbox,
                y_start=y_start,
                y_end=y_end,
            )

        return {
            "log_id": log_id,
            "image_name": image_name,
            "depth_chunks": len(depth_slices),
            "lith_chunks": len(lith_slices),
        }

    def dispatch_all(self) -> None:
        """Dispatches all JSON annotation logs present in the annotation folder."""
        json_files = sorted(list(self.annotations_dir.glob("*.json")))
        if not json_files:
            print(f"No JSON files found in {self.annotations_dir}")
            return

        print(
            f"Connecting to Redis at {self.redis_host}:{self.redis_port} | "
            f"Found {len(json_files)} annotation files."
        )
        for json_path in json_files:
            try:
                res = self.dispatch_single_log(json_path)
                print(
                    f"Dispatched {res['image_name']} ({res['log_id']}) -> "
                    f"{res['depth_chunks']} depth chunks, {res['lith_chunks']} lith chunks."
                )
            except Exception as e:
                print(f"Failed to dispatch {json_path.name}: {e}")


if __name__ == "__main__":
    import sys

    dispatcher = LogDispatcher()

    # Target specific test file (pass via CLI or default to log 0000000009)
    target_name = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "0000000009_d-582_0000020435-0001_log01_fe_1in_md.json"
    )

    target_path = dispatcher.annotations_dir / target_name

    # If exact name wasn't found, attempt fuzzy match by prefix/ID
    if not target_path.exists():
        matches = list(dispatcher.annotations_dir.glob(f"*{target_name}*"))
        if matches:
            target_path = matches[0]
        else:
            print(
                f"Error: Could not find '{target_name}' in {dispatcher.annotations_dir}"
            )
            sys.exit(1)

    print(f"Connecting to Redis at {dispatcher.redis_host}:{dispatcher.redis_port}")
    print(f"Dispatching single test log: {target_path.name}")

    try:
        res = dispatcher.dispatch_single_log(target_path)
        print(
            f"Done! Dispatched {res['image_name']} ({res['log_id']}) -> "
            f"{res['depth_chunks']} depth chunks, {res['lith_chunks']} lith chunks."
        )
    except Exception as e:
        print(f"Failed to dispatch {target_path.name}: {e}")
