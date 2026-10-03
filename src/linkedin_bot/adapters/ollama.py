"""Local feasibility probe, not a released personal-data inference task."""

import asyncio
import json
import time

import httpx
from pydantic import Field, ValidationError

from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.domain.models import Contract
from linkedin_bot.runtime.config import InferenceConfig


class SyntheticAnswer(Contract):
    skills: tuple[str, ...]
    work_authorization: str | None
    fact_ids: tuple[str, ...]


class InstalledModel(Contract):
    name: str
    digest: str
    size: int = Field(ge=0)


class OllamaProbe:
    def __init__(self, config: InferenceConfig) -> None:
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.local_endpoint, timeout=5, trust_env=False)
        self.lane = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def inventory(self) -> tuple[InstalledModel, ...]:
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            content = response.json()
            return tuple(
                InstalledModel(name=m["name"], digest=m["digest"], size=m["size"])
                for m in content["models"]
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise AdapterError("ollama_unavailable") from exc

    async def synthetic(self, model: str) -> dict[str, object]:
        models = await self.inventory()
        selected = next((m for m in models if m.name == model), None)
        if selected is None:
            raise AdapterError("model_not_installed")
        async with self.lane:
            start = time.monotonic()
            try:
                async with asyncio.timeout(self.config.task_deadline_seconds):
                    response = await self.client.post(
                        "/api/generate",
                        json={
                            "model": model,
                            "stream": False,
                            "keep_alive": 0,
                            "format": SyntheticAnswer.model_json_schema(),
                            "prompt": "Return JSON using only confirmed synthetic facts. "
                            "Fact f1: Python skill, confirmed. Fact f2: Rust skill, unconfirmed. "
                            "Work authorization is unknown. List confirmed skills and their IDs; "
                            "leave work_authorization null.",
                            "options": {"num_ctx": 4096, "num_predict": 200, "temperature": 0},
                        },
                        timeout=self.config.task_deadline_seconds,
                    )
                    response.raise_for_status()
                    output = response.json()
                    answer = SyntheticAnswer.model_validate_json(output["response"])
                    passed = (
                        answer.skills == ("Python",)
                        and answer.fact_ids == ("f1",)
                        and answer.work_authorization is None
                        and output.get("done") is True
                    )
            except (httpx.HTTPError, TimeoutError, ValidationError, KeyError, ValueError) as exc:
                raise AdapterError("inference_failed") from exc
            return {
                "model": selected.model_dump(),
                "latency_seconds": time.monotonic() - start,
                "synthetic_grounding_passed": passed,
                "context_tokens": 4096,
                "eval_count": output.get("eval_count"),
                "qualification": "single probe; M4 evaluation gate remains pending",
                "answer": json.loads(answer.model_dump_json()),
            }


class OllamaGenerator:
    """Local structured generation for the evaluated model named in the config."""

    def __init__(self, config: InferenceConfig, transport: httpx.AsyncBaseTransport | None = None):
        if not config.model:
            raise AdapterError("model_not_configured")
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.local_endpoint, timeout=5, trust_env=False, transport=transport
        )
        self.lane = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def generate(self, prompt: str, schema: dict[str, object]) -> str:
        async with self.lane:
            try:
                async with asyncio.timeout(self.config.task_deadline_seconds):
                    response = await self.client.post(
                        "/api/generate",
                        json={
                            "model": self.config.model,
                            "stream": False,
                            "keep_alive": 0,
                            "format": schema,
                            "prompt": prompt,
                            "options": {"num_ctx": 4096, "num_predict": 800, "temperature": 0},
                        },
                        timeout=self.config.task_deadline_seconds,
                    )
                    response.raise_for_status()
                    output = response.json()
                    if output.get("done") is not True or not isinstance(output["response"], str):
                        raise ValueError("truncated")
                    return output["response"]
            except (httpx.HTTPError, TimeoutError, KeyError, ValueError) as exc:
                raise AdapterError("inference_failed") from exc
