"""
Configuration loader for controller node.
Loads from /data/ctlr/config.yml with sensible defaults.
"""

import os
import socket
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List


@dataclass
class NodeConfig:
    name: str = field(default_factory=socket.gethostname)


@dataclass
class CameraConfig:
    """Camera node connection info."""
    name: str = ""
    host: str = ""
    port: int = 8080

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class StorageConfig:
    recordings_dir: str = "/mnt/storage/sessions"
    export_dir: str = "/mnt/export"
    temp_suffix: str = ".tmp"

    @property
    def recordings_path(self) -> Path:
        return Path(self.recordings_dir)

    @property
    def export_path(self) -> Path:
        return Path(self.export_dir)


@dataclass
class RecordingConfig:
    segment_duration: int = 120  # seconds


@dataclass
class LoggingConfig:
    dir: str = "/data/logs/ctlr"
    level: str = "INFO"

    @property
    def log_path(self) -> Path:
        return Path(self.dir)


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class Config:
    node: NodeConfig = field(default_factory=NodeConfig)
    cameras: List[CameraConfig] = field(default_factory=list)
    storage: StorageConfig = field(default_factory=StorageConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


def load_config(config_path: Optional[str] = None) -> Config:
    """Load configuration from YAML file."""

    if config_path is None:
        config_path = os.environ.get("CTLR_CONFIG", "/data/ctlr/config.yml")

    path = Path(config_path)

    config_dict = {}
    if path.exists():
        with open(path) as f:
            config_dict = yaml.safe_load(f) or {}

    # Parse cameras list
    cameras = []
    for cam in config_dict.get("cameras", []):
        cameras.append(CameraConfig(
            name=cam.get("name", ""),
            host=cam.get("host", ""),
            port=cam.get("port", 8080),
        ))

    return Config(
        node=NodeConfig(**config_dict.get("node", {})),
        cameras=cameras,
        storage=StorageConfig(**config_dict.get("storage", {})),
        recording=RecordingConfig(**config_dict.get("recording", {})),
        logging=LoggingConfig(**config_dict.get("logging", {})),
        api=ApiConfig(**config_dict.get("api", {})),
    )


# Global config
_config: Optional[Config] = None


def init_config(config_path: Optional[str] = None) -> Config:
    """Initialize global config."""
    global _config
    _config = load_config(config_path)
    return _config


def get_config() -> Config:
    """Get global config."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
