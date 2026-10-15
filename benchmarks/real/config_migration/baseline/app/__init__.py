"""Reporting service configuration package."""

from .client import Client, build_client
from .config import load_config

__all__ = ["Client", "build_client", "load_config"]
