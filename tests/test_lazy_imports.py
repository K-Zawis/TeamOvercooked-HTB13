"""Verify startup paths leave native imaging/OCR dependencies unloaded."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "src.depth", "src.depth.worker", "src.depth.extraction", "src.depth.validation",
    "src.worker_depth", "src.dispatcher",
    "src.worker_lithology",
])
def test_import_defers_heavy_dependencies(module):
    code = f"""
import importlib
import sys
importlib.import_module({module!r})
heavy = {{'numpy', 'PIL', 'tifffile', 'zarr', 'imagecodecs', 'rasterio', 'redis'}}
loaded = heavy.intersection(sys.modules)
assert not loaded, f'Unexpected eager imports: {{loaded}}'
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["src.depth.validation", "src.worker_lithology"])
def test_cli_help_defers_heavy_dependencies(module):
    code = f"""
import runpy
import sys
sys.argv = [{module!r}, '--help']
try:
    runpy.run_module({module!r}, run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError('help did not exit')
heavy = {{'numpy', 'PIL', 'tifffile', 'zarr', 'imagecodecs', 'rasterio', 'redis'}}
loaded = heavy.intersection(sys.modules)
assert not loaded, f'Unexpected eager imports: {{loaded}}'
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
