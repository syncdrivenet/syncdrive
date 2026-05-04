"""
Configuration loader for camera node.
Loads from /data/cam/config.yml with sensible defaults.
"""

import os
import socket
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class NodeConfig:
    name: str = field(default_factory=socket.gethostname)


@dataclass
class ControllerConfig:
    host: str = "melb-02-ctlr.local"
    port: int = 8000

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class RecordingConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: int = 4000000  # 4Mbps - good for 1080p30
    segment_duration: int = 120  # seconds
    recordings_dir: str = "/data/recordings"

    @property
    def recordings_path(self) -> Path:
        return Path(self.recordings_dir)


@dataclass
class SyncConfig:
    chunk_size: int = 262144  # 256KB
    retry_delay: int = 5
    max_retry_delay: int = 60


@dataclass
class LoggingConfig:
    dir: str = "/data/logs/cam"
    level: str = "INFO"

    @property
    def log_path(self) -> Path:
        return Path(self.dir)


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    node: NodeConfig = field(default_factory=NodeConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


def _merge_dict(base: dict, override: dict) -> dict:
    """Deep merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: Optional[str] = None) -> Config:
    """Load configuration from YAML file."""

    # Default path
    if config_path is None:
        config_path = os.environ.get("CAM_CONFIG", "/data/cam/config.yml")

    path = Path(config_path)

    # Start with defaults
    config_dict = {
        "node": {},
        "controller": {},
        "recording": {},
        "sync": {},
        "logging": {},
        "api": {},
    }

    # Load from file if exists
    if path.exists():
        with open(path) as f:
            file_config = yaml.safe_load(f) or {}
            config_dict = _merge_dict(config_dict, file_config)

    # Build config objects
    return Config(
        node=NodeConfig(**config_dict.get("node", {})),
        controller=ControllerConfig(**config_dict.get("controller", {})),
        recording=RecordingConfig(**config_dict.get("recording", {})),
        sync=SyncConfig(**config_dict.get("sync", {})),
        logging=LoggingConfig(**config_dict.get("logging", {})),
        api=ApiConfig(**config_dict.get("api", {})),
    )


# Global config instance
CONFIG: Config = None


def init_config(config_path: Optional[str] = None) -> Config:
    """Initialize global config."""
    global CONFIG
    CONFIG = load_config(config_path)
    return CONFIG


def get_config() -> Config:
    """Get global config, initializing if needed."""
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()
    return CONFIG
