"""
Controller node entry point.
Initializes all components and starts the HTTP API server.
"""

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config import init_config, get_config
from logger import setup_logging, log_info
from orchestrator import start_orchestrator, stop_orchestrator
from api import app as api_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    config = get_config()

    log_info("main", f"Controller starting: {config.node.name}")

    # Start orchestrator
    await start_orchestrator()

    log_info("main", f"Controller ready on port {config.api.port}")

    yield

    # Shutdown
    log_info("main", "Controller shutting down")

    await stop_orchestrator()

    log_info("main", "Controller stopped")


# Apply lifespan to API app
api_app.router.lifespan_context = lifespan


def main():
    """Main entry point."""
    config = init_config()
    setup_logging()

    log_info("main", f"Configuration loaded: {config.node.name}")

    # Ensure storage directory exists
    config.storage.recordings_path.mkdir(parents=True, exist_ok=True)

    # Run server
    uvicorn.run(
        api_app,
        host=config.api.host,
        port=config.api.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
