# btk_mcp_module/client.py

import asyncio
import io
import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from markitdown import MarkItDown
from pydantic import HttpUrl

from .models import (
    BtkDecisionSummary,
    BtkDocumentMarkdown,
    BtkSearchRequest,
    BtkSearchResult,
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


class BtkApiClient:
    """Client for BTK (Information and Communication Technologies Authority) decisions."""

    BASE_URL = "https://www.btk.tr"
    API_PATH = "/api/content/board-decisions"
    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            timeout=request_timeout,
            verify=True,
            follow_redirects=True,
        )
        self.markitdown = MarkItDown(enable_plugins=False)

    def _build_search_params(self, request: BtkSearchRequest) -> Dict[str, str]:
        params: Dict[str, str] = {
            "page": str(request.page),
            "limit": str(request.pageSize),
            "locale": "tr",
        }

        if request.keywords.strip():
            params["search"] = request.keywords.strip()
        if request.decision_no.strip():
            params["filter[decision_no]"] = request.decision_no.strip()
        if request.decision_date.strip():
            params["filter[decision_date]"] = request.decision_date.strip()
        if request.publication_date.strip():
            params["date_from"] = request.publication_date.strip()
            params["date_to"] = request.publication_date.strip()
        if request.relevant_unit.strip():
            params["filter[relevant_unit]"] = request.relevant_unit.strip()

        return params

    @staticmethod
    def _format_date(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).date().isoformat()
        except ValueError:
            return value[:10] if len(value) >= 10 else value

    @staticmethod
    def _extract_pdf_url(file_data: Any) -> Optional[str]:
        if not isinstance(file_data, dict):
            return None
        for key in ("url", "storageUrl"):
            value = file_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _parse_decision(self, item: Dict[str, Any]) -> BtkDecisionSummary:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        file_data = data.get("file_url") if isinstance(data.get("file_url"), dict) else {}
        pdf_url = self._extract_pdf_url(file_data)

        return BtkDecisionSummary(
            id=str(item.get("id") or ""),
            title=str(item.get("title") or ""),
            slug=str(item.get("slug") or ""),
            decision_no=data.get("decision_no"),
            decision_date=self._format_date(data.get("decision_date")),
            publication_date=self._format_date(item.get("publishedAt")),
            relevant_unit=data.get("relevant_unit"),
            pdf_url=HttpUrl(pdf_url) if pdf_url else None,
            original_filename=file_data.get("originalFilename") or file_data.get("filename"),
        )

    async def search_decisions(self, request: BtkSearchRequest) -> BtkSearchResult:
        params = self._build_search_params(request)
        query_string = urlencode(params, doseq=True)
        query_url = f"{self.BASE_URL}{self.API_PATH}?{query_string}"
        logger.info("BtkApiClient: searching BTK decisions with URL: %s", query_url)

        try:
            response = await self.http_client.get(self.API_PATH, params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            logger.error("BtkApiClient: error searching decisions: %s", e, exc_info=True)
            raise Exception(f"Failed to search BTK decisions: {str(e)}")

        raw_items = payload.get("data") if isinstance(payload, dict) else []
        decisions = [
            self._parse_decision(item)
            for item in raw_items
            if isinstance(item, dict)
        ]
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

        return BtkSearchResult(
            decisions=decisions,
            total_results=int(meta.get("total") or len(decisions)),
            page=int(meta.get("page") or request.page),
            pageSize=int(meta.get("limit") or request.pageSize),
            total_pages=int(meta.get("totalPages") or 0),
            query_url=query_url,
        )

    def _convert_pdf_to_markdown(self, pdf_bytes: bytes) -> str:
        pdf_stream = io.BytesIO(pdf_bytes)
        result = self.markitdown.convert_stream(pdf_stream, file_extension=".pdf")
        return (result.text_content or "").strip()

    async def get_document_markdown(self, pdf_url: str, page_number: int = 1) -> BtkDocumentMarkdown:
        if not pdf_url or not pdf_url.strip():
            return BtkDocumentMarkdown(
                source_url=HttpUrl(f"{self.BASE_URL}/kurul-kararlari"),
                markdown_chunk=None,
                current_page=max(1, page_number),
                total_pages=0,
                is_paginated=False,
                error_message="pdf_url is required.",
            )

        pdf_url = pdf_url.strip()
        if not pdf_url.startswith(("https://www.btk.gov.tr/", "https://www.btk.tr/")):
            return BtkDocumentMarkdown(
                source_url=HttpUrl(pdf_url),
                markdown_chunk=None,
                current_page=max(1, page_number),
                total_pages=0,
                is_paginated=False,
                error_message="Invalid BTK document URL. URL must start with https://www.btk.gov.tr/ or https://www.btk.tr/.",
            )

        try:
            response = await self.http_client.get(pdf_url)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            if "pdf" not in content_type and not pdf_url.lower().endswith(".pdf"):
                raise Exception(f"Expected a PDF document, got content type: {content_type}")

            markdown_content = await asyncio.to_thread(self._convert_pdf_to_markdown, response.content)
            total_pages = max(1, math.ceil(len(markdown_content) / self.DOCUMENT_MARKDOWN_CHUNK_SIZE))
            current_page = max(1, min(page_number, total_pages))
            start_index = (current_page - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE
            end_index = start_index + self.DOCUMENT_MARKDOWN_CHUNK_SIZE

            return BtkDocumentMarkdown(
                source_url=HttpUrl(pdf_url),
                markdown_chunk=markdown_content[start_index:end_index],
                current_page=current_page,
                total_pages=total_pages,
                is_paginated=total_pages > 1,
                error_message=None,
            )
        except Exception as e:
            logger.error("BtkApiClient: error retrieving BTK PDF %s: %s", pdf_url, e, exc_info=True)
            return BtkDocumentMarkdown(
                source_url=HttpUrl(pdf_url),
                markdown_chunk=None,
                current_page=max(1, page_number),
                total_pages=0,
                is_paginated=False,
                error_message=f"Failed to retrieve BTK document: {str(e)}",
            )

    async def close_client_session(self):
        if hasattr(self, "http_client") and self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            logger.info("BtkApiClient: HTTP client session closed.")
