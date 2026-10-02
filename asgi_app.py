"""
ASGI application for Yargı MCP Server

This module provides ASGI/HTTP access to the Yargı MCP server,
allowing it to be deployed as a web service with FastAPI wrapper.

Usage:
    uvicorn asgi_app:app --host 0.0.0.0 --port 8000
"""

import os
import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from mcp_server_main import create_app

# Setup logging
logger = logging.getLogger(__name__)

# Configure CORS
cors_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Create MCP app
mcp_server = create_app()

# Create MCP Starlette sub-application
mcp_app = mcp_server.http_app(path="/")


# Configure JSON encoder for proper Turkish character support
class UTF8JSONResponse(JSONResponse):
    def __init__(self, content=None, status_code=200, headers=None, **kwargs):
        if headers is None:
            headers = {}
        headers["Content-Type"] = "application/json; charset=utf-8"
        super().__init__(content, status_code, headers, **kwargs)

    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")

custom_middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "X-Session-ID"],
    ),
]

# Create FastAPI wrapper application
app = FastAPI(
    title="Yargı MCP Server",
    description="MCP server for Turkish legal databases",
    version="0.1.0",
    middleware=custom_middleware,
    default_response_class=UTF8JSONResponse,
    redirect_slashes=False,
)


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy",
        "service": "Yargı MCP Server",
        "version": "0.1.0",
        "tools_count": len(mcp_server._tool_manager._tools),
    }


@app.api_route("/mcp", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def redirect_to_slash(request: Request):
    """Redirect /mcp to /mcp/ preserving HTTP method with 308"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/mcp/", status_code=308)


@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Yargı MCP Server",
        "description": "MCP server for Turkish legal databases",
        "endpoints": {
            "mcp": "/mcp",
            "health": "/health",
            "status": "/status",
        },
        "transports": {
            "http": "/mcp"
        },
        "supported_databases": [
            "Yargıtay (Court of Cassation)",
            "Danıştay (Council of State)",
            "Emsal (Precedent)",
            "Uyuşmazlık Mahkemesi (Court of Jurisdictional Disputes)",
            "Anayasa Mahkemesi (Constitutional Court)",
            "Kamu İhale Kurulu (Public Procurement Authority)",
            "Rekabet Kurumu (Competition Authority)",
            "Sayıştay (Court of Accounts)",
            "KVKK (Personal Data Protection Authority)",
            "BDDK (Banking Regulation and Supervision Agency)",
            "BTK (Information and Communication Technologies Authority)",
            "Bedesten API (Multiple courts)",
            "Sigorta Tahkim Komisyonu (Insurance Arbitration Commission)",
        ],
    }


@app.get("/status")
async def status():
    """Status endpoint with detailed information"""
    tools = []
    for tool in mcp_server._tool_manager._tools.values():
        tools.append({
            "name": tool.name,
            "description": tool.description[:100] + "..." if len(tool.description) > 100 else tool.description
        })

    return {
        "status": "operational",
        "tools": tools,
        "total_tools": len(tools),
        "transport": "streamable_http",
    }


# Mount MCP app at /mcp/
app.mount("/mcp/", mcp_app)

# Set the lifespan context after mounting
app.router.lifespan_context = mcp_app.lifespan

# Export for uvicorn
__all__ = ["app"]
