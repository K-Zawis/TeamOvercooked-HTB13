import numpy as np
from typing import List, Dict, Any
from depth_extraction import StreamingDepthExtractor


class DepthExtractor(StreamingDepthExtractor):
    """Depth-scale OCR for ordered RGB strips. Call reset() between documents."""


class LithologyClusterer:
    def __init__(self):
        """
        Initialize clustering models and
        feature extractors here.
        """
        pass

    def process(
        self, lith_crop: np.ndarray, legend_crop: np.ndarray
    ) -> List[Dict[str, Any]]:
        """
        Segments the lithology column, clusters textures, and matches to the legend.

        Args:
            lith_crop (np.ndarray): RGB image array of the interpreted lithology column.
            legend_crop (np.ndarray): RGB image array of the lithology legend.

        Returns:
            List[Dict]: A list of dictionaries, one per geological interval.
        """
        # ML TEAM: Replace this dummy data with OpenCV/Clustering logic.
        # NOTE 1: Do not calculate top_depth or base_depth here. The pipeline will
        # calculate those automatically by cross-referencing your top_y/bottom_y.
        # NOTE 2: Rely on texture (LBP/Gabor) rather than pure color, as color
        # schemas change between different well logs.
        # NOTE 3: The key must be 'bottom_y' to match the pipeline expectations.
        return [
            {
                "top_y": 100,
                "bottom_y": 300,
                "cluster_id": "A",
                "lithology": "Siltstone",
            },
            {"top_y": 300, "bottom_y": 500, "cluster_id": "B", "lithology": "Shale"},
            {
                "top_y": 500,
                "bottom_y": 900,
                "cluster_id": "A",
                "lithology": "Siltstone",
            },
        ]
