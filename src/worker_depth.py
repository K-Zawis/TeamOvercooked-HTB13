"""Depth-worker entry point, matching the other pipeline workers."""

from .depth.worker import main


if __name__ == "__main__":
    main()
