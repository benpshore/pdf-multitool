"""PDF Multitool. Engine work is isolated from the UI and job controller."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pdf-multitool")
except PackageNotFoundError:
    __version__ = "0+unknown"
