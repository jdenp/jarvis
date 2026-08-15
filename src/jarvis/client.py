"""Client for a running voice service.

Used by both the CLI and the MCP server, so the two cannot drift apart.
"""

from __future__ import annotations

import httpx

from .config import ServiceConfig


class ServiceUnavailable(RuntimeError):
    """No voice service is listening. Usually means `jarvis serve` is not running."""


class VoiceClient:
    """Thin wrapper over the service's loopback HTTP API."""

    def __init__(self, config: ServiceConfig | None = None, client: httpx.Client | None = None):
        self.config = config or ServiceConfig()
        self._client = client or httpx.Client(
            base_url=f"http://{self.config.host}:{self.config.port}",
            # Read timeout has to outlast the longest wait the service allows.
            timeout=httpx.Timeout(self.config.max_wait_seconds + 15, connect=3.0),
        )

    def status(self) -> dict:
        return self._get("/status", {})

    def heard(
        self,
        since: int = 0,
        wait: float = 0.0,
        addressed_only: bool = False,
        settle: float | None = None,
    ) -> dict:
        """Utterances after ``since``. With ``wait``, blocks until there is one.

        ``addressed_only`` holds out for speech aimed at JARVIS rather than
        waking on overheard chatter. Everything after the cursor comes back
        either way, so the caller still sees the context around an instruction.
        """
        params: dict[str, object] = {
            "since": since,
            "wait": max(0.0, min(wait, self.config.max_wait_seconds)),
            "addressed": "1" if addressed_only else "0",
        }
        if settle is not None:
            params["settle"] = max(0.0, settle)
        return self._get("/heard", params)

    def say(self, text: str) -> dict:
        try:
            response = self._client.post("/say", json={"text": text})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

    def _unavailable(self, exc: Exception) -> ServiceUnavailable:
        return ServiceUnavailable(
            f"No voice service at {self.config.host}:{self.config.port} ({exc}). "
            "Start one with `jarvis serve`."
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VoiceClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
