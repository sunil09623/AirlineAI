"""Offline web app for reviewing NDC AirShopping messages.

Serves a single-page chat + upload UI. Two capabilities:

* **Deterministic review** — upload an AirShoppingRQ, a baseline AirShoppingRS and
  a new AirShoppingRS, and get back exactly what is missing and what is extra. This
  path calls no LLM, so it works with no model server and no network at all.

* **Chat** — optional natural-language explanation via the locally served model.
  If no model is reachable the UI still works and simply reports that chat is
  unavailable, because the deterministic review does not depend on it.

Everything binds to localhost and never makes an outbound request.
"""

from __future__ import annotations

import json
import socket
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from airshop.agent.local import (
    AgentConfig,
    build_conversation,
    collect_agent_text,
)
from airshop.ndc.catalog import MessageCatalog, review_update
from airshop.ndc.diff import summarize_message
from airshop.offline import enforce_offline

STATIC_DIR = Path(__file__).parent / "static"

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ReviewRequest(BaseModel):
    baseline_rq: str | None = None
    baseline_rs: str
    new_rs: str


class Workspace:
    """Per-process workspace holding uploaded messages and reports."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.catalog = MessageCatalog(root / "ndc_messages")
        self.reports_dir = root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def save_upload(self, name: str, content: bytes) -> dict[str, Any]:
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file too large (max 20 MB)")
        with tempfile.NamedTemporaryFile(
            "wb", suffix=".xml", dir=self.root, delete=False
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        try:
            ref = self.catalog.register(name, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return {
            "name": ref.name,
            "kind": ref.kind,
            "root": ref.root,
            "path": str(ref.path),
        }

    def review(
        self, baseline_rs: str, new_rs: str, baseline_rq: str | None
    ) -> dict[str, Any]:
        try:
            base_path = self.catalog.resolve(baseline_rs)
            new_path = self.catalog.resolve(new_rs)
            rq_path = self.catalog.resolve(baseline_rq) if baseline_rq else None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        review = review_update(base_path, new_path, rq_path)
        report = review.report
        payload = report.to_dict()
        payload["baseline_inventory"] = review.baseline_summary["entity_counts"]
        payload["new_inventory"] = review.new_summary["entity_counts"]
        payload["narrative"] = _narrative(report, review)
        payload["render"] = review.render()
        return payload


def _narrative(report, review) -> str:
    """Plain-language summary of the diff, no LLM required."""
    if report.identical:
        return (
            "The new response is identical to the baseline. Nothing is missing and "
            "nothing has been added."
        )

    sentences: list[str] = []

    removed = [e for e in report.entities_removed if e.entity == "Offer"]
    added = [e for e in report.entities_added if e.entity == "Offer"]
    if removed:
        ids = ", ".join(e.key for e in removed)
        sentences.append(
            f"{len(removed)} offer(s) present in the baseline are MISSING from the "
            f"new response: {ids}."
        )
    if added:
        ids = ", ".join(e.key for e in added)
        sentences.append(f"{len(added)} new offer(s) appeared: {ids}.")

    modified_offers = [e for e in report.entities_modified if e.entity == "Offer"]
    price_changes = [
        e.key for e in modified_offers for miss in e.missing if "TotalAmount" in miss
    ]
    if modified_offers:
        sentences.append(
            f"{len(modified_offers)} offer(s) kept their ID but changed content."
        )
    if price_changes:
        sentences.append(f"Prices changed on: {', '.join(sorted(set(price_changes)))}.")

    if report.counts_extra:
        top = sorted(report.counts_extra.items(), key=lambda kv: -kv[1])[:5]
        sentences.append(
            "New nodes appeared under: " + ", ".join(_short(p) for p, _ in top) + "."
        )
    if report.counts_missing:
        top = sorted(report.counts_missing.items(), key=lambda kv: -kv[1])[:5]
        sentences.append(
            "Nodes no longer present under: "
            + ", ".join(_short(p) for p, _ in top)
            + "."
        )

    base_offers = review.baseline_summary["entity_counts"].get("Offer", 0)
    new_offers = review.new_summary["entity_counts"].get("Offer", 0)
    sentences.append(f"Offer count went from {base_offers} to {new_offers}.")

    return " ".join(sentences)


def _short(path: str) -> str:
    return path.replace("AirShoppingRS/Response/", "")


def create_app(workspace_dir: Path | str = "web_runs") -> FastAPI:
    """Build the FastAPI app bound to a local workspace directory."""
    enforce_offline()
    workspace_path = Path(workspace_dir)
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(workspace_path)

    app = FastAPI(title="NDC AirShopping Reviewer", docs_url=None, redoc_url=None)
    app.state.workspace = workspace

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            raise HTTPException(status_code=500, detail="UI not found")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        config = AgentConfig(workspace=str(workspace_path))
        return {
            "status": "ok",
            "offline": True,
            "model": config.model,
            "chat_available": _model_available(config.model, config.ollama_base_url),
            "messages": [m.name for m in workspace.catalog.list()],
        }

    @app.post("/api/upload")
    async def upload(
        file: UploadFile = File(...),
        name: str = Form(...),
    ) -> JSONResponse:
        content = await file.read()
        if not content.strip():
            raise HTTPException(status_code=400, detail="uploaded file is empty")
        return JSONResponse(workspace.save_upload(name, content))

    @app.get("/api/messages")
    def messages() -> dict[str, Any]:
        return {
            "messages": [
                {"name": m.name, "kind": m.kind, "root": m.root}
                for m in workspace.catalog.list()
            ]
        }

    @app.get("/api/messages/{name}/summary")
    def message_summary(name: str) -> dict[str, Any]:
        try:
            path = workspace.catalog.resolve(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return summarize_message(path.read_text(encoding="utf-8"), name)

    @app.post("/api/review")
    def review(request: ReviewRequest) -> JSONResponse:
        return JSONResponse(
            workspace.review(request.baseline_rs, request.new_rs, request.baseline_rq)
        )

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> dict[str, Any]:
        config = AgentConfig(workspace=str(workspace_path))
        if not _ollama_reachable(config.ollama_base_url):
            raise HTTPException(
                status_code=503,
                detail=(
                    "The local model server is not reachable, so chat is "
                    "unavailable. The Review tab works without it."
                ),
            )
        if not _model_available(config.model, config.ollama_base_url):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"The model {config.model!r} is not installed in the local "
                    f"runtime. Run: ollama pull {config.model}. "
                    "The Review tab works without it."
                ),
            )
        collected: list[str] = []

        try:
            conversation = build_conversation(
                config, callbacks=[collect_agent_text(collected)]
            )
            conversation.send_message(request.message)
            conversation.run()
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            raise HTTPException(status_code=502, detail=f"Chat failed: {exc}") from exc

        return {"reply": collected[-1] if collected else "(no reply)"}

    return app


def _ollama_reachable(base_url: str | None) -> bool:
    """Quick localhost-only probe of the model runtime."""
    if not base_url:
        return False
    host_port = base_url.replace("http://", "").replace("https://", "").split("/")[0]
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection(
            (host or "127.0.0.1", int(port or 11434)), timeout=1
        ):
            return True
    except OSError:
        return False


def _model_available(model: str, base_url: str | None) -> bool:
    """Whether ``model`` is actually pulled in the local runtime.

    The health badge must reflect this: a running server with the model missing
    otherwise reports "chat: ready" and only fails once the user sends a message.
    """
    if not _ollama_reachable(base_url):
        return False
    name = model.split("/", 1)[1] if "/" in model else model
    host_port = base_url.replace("http://", "").replace("https://", "").split("/")[0]
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection(
            (host or "127.0.0.1", int(port or 11434)), timeout=2
        ) as sock:
            request = (
                "GET /api/tags HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            sock.sendall(request.encode())
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        body = b"".join(chunks).split(b"\r\n\r\n", 1)
        if len(body) < 2:
            return False
        models = json.loads(body[1].decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False

    installed = {
        m.get("name", "") for m in models.get("models", []) if isinstance(m, dict)
    }
    # Ollama reports "qwen2.5:3b-instruct"; accept an implicit ":latest" too.
    return name in installed or f"{name}:latest" in installed


def run(
    host: str = "127.0.0.1", port: int = 12000, workspace_dir: str = "web_runs"
) -> None:
    """Start the web app with uvicorn."""
    import uvicorn

    uvicorn.run(create_app(workspace_dir), host=host, port=port, log_level="info")


__all__ = ["Workspace", "create_app", "run", "_narrative"]
