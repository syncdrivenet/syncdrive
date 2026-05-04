"""
Camera node API server entry point.
Runs the HTTP API server. Recorder and uploader run as separate processes.
"""

import uvicorn

from config import init_config, get_config
from logger import setup_logging, log_info
from api import app


def main():
    """Run API server."""
    config = init_config()
    setup_logging()

    log_info("api", f"Camera API starting: {config.node.name}")

    # Ensure directories exist
    config.recording.recordings_path.mkdir(parents=True, exist_ok=True)

    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
