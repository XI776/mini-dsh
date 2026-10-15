"""Thin transport wrapper around the normalized configuration."""

from dataclasses import dataclass

from .config import load_config


@dataclass(frozen=True)
class Client:
    base_url: str
    timeout_s: int
    retries: int

    def describe(self):
        return f"{self.base_url} timeout={self.timeout_s}s retries={self.retries}"


def build_client(raw):
    """Build a client from an operator configuration mapping."""
    config = load_config(raw)
    return Client(config["base_url"], config["timeout_s"], config["retries"])
