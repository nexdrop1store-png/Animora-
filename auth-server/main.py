"""Animora Auth Server — FastAPI application."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .oauth import router as oauth_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Animora Auth Server", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://animora.tech", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(oauth_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
