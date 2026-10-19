"""Compatibility entry point; canonical ARQ settings live in backend/app/worker.py."""
from app.worker import WorkerSettings

__all__ = ["WorkerSettings"]
