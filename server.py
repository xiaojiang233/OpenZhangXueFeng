"""Run with uv run server.py, or uv run uvicorn server:app --reload."""

import logging
from pathlib import Path

from app import create_app
from config import HOST, PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
app = create_app()


if __name__ == "__main__":
    import uvicorn

    directory = Path(__file__).resolve().parent
    certificate, key = directory / "cert.pem", directory / "cert.key"
    ssl = {"ssl_keyfile": str(key), "ssl_certfile": str(certificate)} if certificate.exists() and key.exists() else {}
    uvicorn.run("server:app", host=HOST, port=PORT, log_level="info", **ssl)
