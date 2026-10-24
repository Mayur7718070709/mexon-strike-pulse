from app.dhan.monitor import DhanConnectionMonitor


class ProviderHealthService:
    """Public-safe provider health summary; reports REST validation and feed separately."""

    def __init__(self, dhan_monitor: DhanConnectionMonitor):
        self.dhan_monitor = dhan_monitor

    async def get_status(self) -> dict:
        status = await self.dhan_monitor.status()
        return {"provider": "DHANHQ", **status}
