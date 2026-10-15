"""Per-job model preset support for cron jobs.

A cron job may name its own model preset (``modelPreset`` in ``jobs.json``).
The preset applies to that job's turn only: the chat's session preset is left
untouched for every other turn.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.cron import CronTool
from nanobot.cron.bound_runner import run_bound_cron_job
from nanobot.cron.service import CronService
from nanobot.cron.session_turns import CRON_MODEL_PRESET_META, cron_model_preset
from nanobot.cron.types import CronJob, CronPayload, CronSchedule

_BOUND_CHAT = {
    "session_key": "telegram:42",
    "origin_channel": "telegram",
    "origin_chat_id": "42",
}


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "cron" / "jobs.json"


def _flush_to_disk(service: CronService) -> None:
    """Merge the action log into jobs.json, as a gateway restart would."""
    service._running = True
    try:
        service._load_store()
    finally:
        service._running = False


class _SvcStub:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def add_job(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return types.SimpleNamespace(id="id1", name=kwargs.get("name", "x"))


def test_jobs_json_round_trips_model_preset(tmp_path: Path) -> None:
    store_path = _store_path(tmp_path)
    service = CronService(store_path)
    service.add_job(
        name="pjj",
        schedule=CronSchedule(kind="cron", expr="0 6 * * *", tz="Asia/Makassar"),
        message="cek berita",
        model_preset="fast",
        **_BOUND_CHAT,
    )
    _flush_to_disk(service)

    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["jobs"][0]["payload"]["modelPreset"] == "fast"
    jobs, _version = CronService(store_path)._load_jobs()
    assert jobs is not None
    assert jobs[0].payload.model_preset == "fast"


def test_cron_model_preset_reads_only_non_empty_strings() -> None:
    assert cron_model_preset({CRON_MODEL_PRESET_META: " fast "}) == "fast"
    assert cron_model_preset({CRON_MODEL_PRESET_META: "   "}) is None
    assert cron_model_preset({CRON_MODEL_PRESET_META: 7}) is None
    assert cron_model_preset(None) is None


async def test_bound_runner_marks_job_preset_on_its_turn() -> None:
    job = CronJob(
        id="j1",
        name="pjj",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="cek berita", model_preset="fast", **_BOUND_CHAT),
    )
    seen: dict[str, Any] = {}

    class _Agent:
        async def submit_cron_turn(self, msg: Any) -> None:
            seen["msg"] = msg
            return None

    class _Recorder:
        def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
            pass

    await run_bound_cron_job(job, agent=_Agent(), cron=_Recorder())
    assert seen["msg"].metadata[CRON_MODEL_PRESET_META] == "fast"


class _ResolverStub:
    def resolve_preset(self, name: str) -> str:
        if name != "fast":
            raise KeyError(name)
        return "runtime:fast"


def test_cron_turn_uses_job_preset_and_falls_back_on_unknown() -> None:
    fake = types.SimpleNamespace(runtime_resolver=_ResolverStub())
    assert AgentLoop._cron_preset_runtime(fake, {CRON_MODEL_PRESET_META: "fast"}) == "runtime:fast"
    assert AgentLoop._cron_preset_runtime(fake, {CRON_MODEL_PRESET_META: "gone"}) is None
    assert AgentLoop._cron_preset_runtime(fake, None) is None


def test_cron_tool_records_preset_and_rejects_unknown() -> None:
    stub = _SvcStub()
    tool = CronTool(stub, default_timezone="UTC", model_presets=["fast", "deep"])
    with request_context(
        RequestContext(channel="telegram", chat_id="42", session_key="telegram:42")
    ):
        created = asyncio.run(
            tool.execute(action="add", message="cek", cron_expr="0 6 * * *", model_preset="DEEP")
        )
        rejected = asyncio.run(
            tool.execute(action="add", message="cek", cron_expr="0 6 * * *", model_preset="nope")
        )

    assert "deep" in created
    assert stub.kwargs is not None and stub.kwargs["model_preset"] == "deep"
    assert "nope" in rejected and "fast" in rejected
