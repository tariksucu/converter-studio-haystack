"""A small stand-in for the Converter Studio API, served through ``httpx.MockTransport``.

It reads a file's format from its bytes first (``%PDF``) and then from its name, as the API does,
and makes each conversion's result from a few rules (see :meth:`FakeAPI.result`).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE = "https://api.test/api"
KEY = "cvs_live_test_key_0123456789"

FORMATS: list[dict[str, Any]] = [
    {
        "id": "pdf",
        "name": "PDF",
        "category": "document",
        "extensions": ["pdf"],
        "to": ["jpg", "pdf", "docx", "png", "txt"],
    },
    {
        "id": "docx",
        "name": "Word",
        "category": "document",
        "extensions": ["docx"],
        "to": ["pdf", "txt", "md"],
    },
    {"id": "doc", "name": "Word 97", "category": "document", "extensions": ["doc"], "to": ["docx"]},
    {
        "id": "txt",
        "name": "TXT",
        "category": "document",
        "extensions": ["txt", "text"],
        "to": ["pdf", "docx"],
    },
    {
        "id": "pptx",
        "name": "PowerPoint",
        "category": "presentation",
        "extensions": ["pptx"],
        "to": ["pdf", "ppt"],
    },
    {
        "id": "xlsx",
        "name": "Excel",
        "category": "spreadsheet",
        "extensions": ["xlsx"],
        "to": ["pdf", "csv", "json"],
    },
    {
        "id": "csv",
        "name": "CSV",
        "category": "spreadsheet",
        "extensions": ["csv"],
        "to": ["xlsx", "pdf"],
    },
    {
        "id": "png",
        "name": "PNG",
        "category": "image",
        "extensions": ["png"],
        "to": ["jpg", "pdf", "txt", "webp"],
    },
    {
        "id": "jpg",
        "name": "JPG",
        "category": "image",
        "extensions": ["jpg", "jpeg"],
        "to": ["png", "pdf", "txt"],
    },
    {
        "id": "heic",
        "name": "HEIC",
        "category": "image",
        "extensions": ["heic", "heif"],
        "to": ["jpg", "pdf", "txt"],
    },
    {
        "id": "cr2",
        "name": "Canon RAW",
        "category": "image",
        "extensions": ["cr2"],
        "to": ["jpg", "pdf"],
    },
    {
        "id": "json",
        "name": "JSON",
        "category": "data",
        "extensions": ["json"],
        "to": ["csv", "xml"],
    },
    {"id": "mp4", "name": "MP4", "category": "video", "extensions": ["mp4"], "to": ["mp3", "gif"]},
    {
        "id": "mov",
        "name": "QuickTime",
        "category": "video",
        "extensions": ["mov"],
        "to": ["mp4", "mp3"],
    },
]
BY_ID = {item["id"]: item for item in FORMATS}
BY_EXTENSION = {ext: item["id"] for item in FORMATS for ext in item["extensions"]}

#: Bytes that make the fake behave in a certain way.
SCAN = b"%PDF-1.7 scanned pages, no text layer"
TEXT_PDF = b"%PDF-1.7 with a text layer"
BROKEN = b"broken bytes"


@dataclass
class Job:
    id: str
    source: str
    to: str
    name: str
    status: str
    credits: int
    max_credits: int | None
    output: bytes = b""
    error: dict[str, Any] | None = None

    def view(self) -> dict[str, Any]:
        done = self.status == "done"
        stem = self.name.rsplit(".", 1)[0] if "." in self.name else self.name
        return {
            "id": self.id,
            "status": self.status,
            "from": self.source,
            "to": self.to,
            "inputName": self.name,
            "inputSize": 10,
            "credits": self.credits,
            "maxCredits": self.max_credits,
            "progress": 1 if done else None,
            "outputName": f"{stem}.{self.to}",
            "outputSize": len(self.output) if done else None,
            "downloadUrl": f"{BASE}/jobs/{self.id}/download?token=tok_{self.id}" if done else None,
            "expiresAt": "2026-11-23T07:00:00.000Z",
            "error": self.error if self.status == "failed" else None,
            "next": "Done." if done else "Check again.",
        }


@dataclass
class FakeAPI:
    credits: int = 1
    balance: int = 100
    requests: list[httpx.Request] = field(default_factory=list)
    conversions: list[dict[str, Any]] = field(default_factory=list)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)
    queue: dict[str, deque[httpx.Response]] = field(default_factory=dict)
    counter: int = 0
    #: Links and the bytes "behind" them.
    links: dict[str, bytes] = field(default_factory=dict)
    in_flight: int = 0
    most_in_flight: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def async_transport(self, delay: float = 0.0) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/v1/conversions"):
                self.in_flight += 1
                self.most_in_flight = max(self.most_in_flight, self.in_flight)
                try:
                    await asyncio.sleep(delay)
                    return self.handle(request)
                finally:
                    self.in_flight -= 1
            return self.handle(request)

        return httpx.MockTransport(handle)

    def push(self, path: str, *answers: httpx.Response) -> None:
        self.queue.setdefault(path, deque()).extend(answers)

    def paths(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == f"/api{path}"]

    def upload_names(self) -> list[str]:
        return [json.loads(r.content)["fileName"] for r in self.paths("POST", "/v1/uploads")]

    # Answers

    def handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        queued = self.queue.get(request.url.path)
        if queued:
            return queued.popleft()
        path = request.url.path.removeprefix("/api")
        method = request.method
        if method == "GET" and path == "/v1/formats":
            return _json(200, {"formats": FORMATS})
        if path.startswith("/v1/uploads/") and method == "PUT":
            upload = self.uploads.get(path.rsplit("/", 1)[-1])
            if upload is None:
                return _error(404, "notFound", "We couldn't find that conversion.")
            upload["data"] = request.content
            return _json(200, {"status": "ready"})
        if method == "GET" and path.startswith("/jobs/"):
            job = self.jobs[path.split("/")[2]]
            return httpx.Response(200, content=job.output)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return _error(401, "invalidApiKey", "This API key does not work.")
        if method == "POST" and path == "/v1/uploads":
            self.counter += 1
            upload_id = f"up{self.counter:04d}"
            self.uploads[upload_id] = {"name": json.loads(request.content)["fileName"], "data": b""}
            return _json(
                201,
                {
                    "id": upload_id,
                    "uploadUrl": f"{BASE}/v1/uploads/{upload_id}?token=t",
                    "method": "PUT",
                    "maxBytes": 10_000_000,
                    "partBytes": 95 * 1024 * 1024,
                    "expiresAt": "2026-11-23T02:00:00.000Z",
                },
            )
        if method == "POST" and path == "/v1/conversions":
            return self._create(json.loads(request.content))
        if method == "GET" and path.startswith("/v1/conversions/"):
            return _json(200, self.jobs[path.split("/")[3]].view())
        return _json(404, {"error": "notFound"})

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        self.conversions.append(body)
        if "uploadId" in body:
            upload = self.uploads.pop(body["uploadId"])
            name, data, job_id = upload["name"], upload["data"], body["uploadId"]
        else:
            name = body["url"].rsplit("/", 1)[-1]
            data, job_id = self.links.get(body["url"], b""), f"ln{len(self.conversions):04d}"
        source = detect(name, data)
        to = body["to"]
        if source is None or to not in BY_ID[source]["to"]:
            return _error(
                422,
                "unsupportedConversion",
                f"We can't convert {source or 'this file'} to {to} yet.",
                **({"from": source, "to": to} if source else {}),
            )
        if self.credits > self.balance:
            return _error(
                402,
                "notEnoughCredits",
                "You don't have enough credits for this.",
                needed=self.credits,
                balance=self.balance,
            )
        max_credits = body.get("maxCredits", 20)
        job = Job(job_id, source, to, name, "done", self.credits, max_credits)
        if self.credits > max_credits:
            job.status = "quoted"
        else:
            outcome = self.result(source, to, data, body.get("options") or {})
            if isinstance(outcome, bytes):
                job.output = outcome
            else:
                job.status, job.error = "failed", outcome
        self.jobs[job_id] = job
        return _json(201, job.view())

    def result(
        self, source: str, to: str, data: bytes, options: dict[str, Any]
    ) -> bytes | dict[str, Any]:
        """What a conversion makes: bytes, or the error it fails with."""
        if data == BROKEN:
            return {
                "code": "conversionFailed",
                "message": "This file could not be converted. Your credits were given back.",
            }
        if source == "pdf" and to == "txt":
            if options.get("ocr"):
                return b"Text read by OCR\n"
            if data.startswith(SCAN):
                return {
                    "code": "noText",
                    "message": "We couldn't find any text in this file.",
                }
            if data.startswith(b"%PDF-1.7 slides"):
                return b"Slide one\n\x0cSlide two\n\x0c"
            return b"Page one\n\x0cPage two\n\x0c"
        if BY_ID[source]["category"] == "image" and to == "txt":
            return b"Text in a picture\n"
        if BY_ID[source]["category"] == "image" and to == "pdf":
            return SCAN
        if source == "pptx" and to == "pdf":
            return b"%PDF-1.7 slides"
        if source == "xlsx" and to == "csv":
            return b"name,age\nAda,36\n"
        if source == "docx" and to == "txt":
            return "﻿Word text: café\n".encode()
        if to == "pdf":
            return b"%PDF-1.7 made from " + source.encode()
        return f"{source} as {to}".encode()


def detect(name: str, data: bytes) -> str | None:
    """The format of a file: from its bytes when they say, else from its name."""
    if data.startswith(b"%PDF"):
        return "pdf"
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return BY_EXTENSION.get(extension)


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, json=body)


def _error(status: int, code: str, message: str, **params: object) -> httpx.Response:
    error: dict[str, Any] = {"code": code, "message": message}
    if params:
        error["params"] = params
    return _json(status, {"error": error})


def error_response(status: int, code: str, message: str, **params: object) -> httpx.Response:
    return _error(status, code, message, **params)
