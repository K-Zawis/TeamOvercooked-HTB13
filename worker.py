import gc
import psutil
import numpy as np
from PIL import Image
from pathlib import Path
from typing import Dict, List, Any, Generator, Tuple
from models import DepthExtractor, LithologyClusterer


class WellLogProcessor:
    def __init__(self):
        Image.MAX_IMAGE_PIXELS = None
        self.depth_model = DepthExtractor()
        self.lith_model = LithologyClusterer()

    @staticmethod
    def stream_image_strips(
        image: Image.Image, bounding_box: List[int], strip_height: int = 2000
    ) -> Generator[Tuple[np.ndarray, int], None, None]:
        """Streams memory-safe NumPy arrays of an image one horizontal strip at a time.

        Args:
            image (Image.Image): The Pillow image object to be processed.
            bounding_box (List[int]): The bounding box coordinates to crop, formatted as [left, top, right, bottom].
            strip_height (int, optional): The maximum pixel height of each image strip. Defaults to 2000.

        Yields:
            Generator[Tuple[np.ndarray, int], None, None]: A tuple containing the RGB image strip as a NumPy array, and its corresponding top y-coordinate offset within the original bounding box.
        """

        left, top, right, bottom = bounding_box

        for strip_top_y in range(top, bottom, strip_height):
            strip_bottom_y = min(strip_top_y + strip_height, bottom)

            cropped_strip = image.crop(
                (left, strip_top_y, right, strip_bottom_y)
            ).convert("RGB")

            yield np.array(cropped_strip), strip_top_y

            del cropped_strip
            gc.collect()

    def process_log(
        self, image_path: Path, bbox_dict: Dict[str, List[int]]
    ) -> Dict[str, Any]:
        """
        Lazy-loads the image and streams it to the ML models in flat-memory chunks.
        """
        img = Image.open(image_path)

        global_y_map = {}
        final_intervals = []

        # 1. STREAM DEPTH COLUMN
        # Process depth OCR in 2000px chunks to keep RAM completely flat
        for depth_chunk, y_offset in self.stream_image_strips(
            img, bbox_dict["depth_column"]
        ):

            # Emergency worker-level circuit breaker
            if psutil.virtual_memory().percent > 95.0:
                return {"file": image_path.name, "status": "OOM_ABORT", "results": []}

            chunk_data = self.depth_model.extract(depth_chunk)

            # Adjust local y-coordinates to global image y-coordinates
            for local_y, depth_val in chunk_data.get("y_to_depth_map", {}).items():
                global_y_map[local_y + y_offset] = depth_val

        # 2. STREAM LITHOLOGY & LEGEND
        # The legend is small enough to load at once
        legend_crop = np.array(img.crop(bbox_dict["lithology_legend"]).convert("RGB"))

        for lith_chunk, y_offset in self.stream_image_strips(
            img, bbox_dict["interpreted_lithology_column"]
        ):

            if psutil.virtual_memory().percent > 95.0:
                return {"file": image_path.name, "status": "OOM_ABORT", "results": []}

            chunk_intervals = self.lith_model.process(lith_chunk, legend_crop)

            # Adjust interval coordinates and calculate depths
            for interval in chunk_intervals:
                global_top_y = interval["top_y"] + y_offset
                global_bottom_y = interval["bottom_y"] + y_offset

                final_intervals.append(
                    {
                        "top_y": global_top_y,
                        "bottom_y": global_bottom_y,
                        "cluster_id": interval["cluster_id"],
                        "lithology": interval["lithology"],
                        "top_depth": round(
                            self.calculate_depth(global_top_y, global_y_map), 2
                        ),
                        "base_depth": round(
                            self.calculate_depth(global_bottom_y, global_y_map), 2
                        ),
                    }
                )

        return {
            "file": image_path.name,
            "status": "success",
            "results": final_intervals,
        }

    @staticmethod
    def calculate_depth(target_y: int, y_to_depth_map: Dict[int, float]) -> float:
        # (Same interpolation logic as before)
        pass


# =============================================================================
# Multiprocessing Hooks
# =============================================================================
_PROCESSOR_INSTANCE: WellLogProcessor | None = None


def init_worker():
    global _PROCESSOR_INSTANCE
    _PROCESSOR_INSTANCE = WellLogProcessor()


def process_single_log_task(
    image_path: Path, bbox_dict: Dict[str, List[int]]
) -> Dict[str, Any]:
    return _PROCESSOR_INSTANCE.process_log(image_path, bbox_dict)
