"""Async HTTP client for the Windows host agent."""
import httpx

from . import config


class AgentBusy(Exception):
    """agent is already running another device"""


class AgentError(Exception):
    """anything else that went wrong talking to the agent"""


class AgentClient:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=config.AGENT_URL,
            headers={"X-Agent-Token": config.AGENT_TOKEN},
            timeout=httpx.Timeout(30.0, connect=6.0),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def health(self) -> dict:
        resp = await self.client.get("/health")
        resp.raise_for_status()
        return resp.json()

    async def status(self) -> dict:
        resp = await self.client.get("/status")
        resp.raise_for_status()
        return resp.json()

    async def start_run(self, job: dict) -> dict:
        resp = await self.client.post("/run", json=job)
        if resp.status_code == 409:
            raise AgentBusy(resp.json().get("error", "agent busy"))
        if resp.status_code != 200:
            raise AgentError(f"agent returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def stop(self) -> dict:
        resp = await self.client.post("/stop", json={})
        resp.raise_for_status()
        return resp.json()

    async def wipe(self, avd: str) -> dict:
        resp = await self.client.post("/wipe", json={"avd": avd})
        resp.raise_for_status()
        return resp.json()

    async def kill_emulator(self) -> dict:
        resp = await self.client.post("/kill-emulator", json={})
        resp.raise_for_status()
        return resp.json()

    async def screenshot(self) -> bytes | None:
        try:
            resp = await self.client.get("/screenshot")
        except httpx.HTTPError:
            return None
        if resp.status_code == 200 and resp.content[:8] == b"\x89PNG\r\n\x1a\n":
            return resp.content
        return None
