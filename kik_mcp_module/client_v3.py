# kik_mcp_module/client_v3.py

import asyncio
import base64
import httpx
import logging
import uuid
import ssl
import os
import re
import io
import math
from typing import Optional
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from markitdown import MarkItDown

# Cryptography imports for AES-256-CBC encryption of document IDs
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

from .models_v2 import (
    KikV2DecisionType, KikV2SearchPayload, KikV2SearchPayloadDk, KikV2SearchPayloadMk,
    KikV2RequestData, KikV2QueryRequest, KikV2KeyValuePair,
    KikV2SearchResponse, KikV2SearchResponseDk, KikV2SearchResponseMk,
    KikV2SearchResult, KikV2CompactDecision, KikV2DocumentMarkdown
)

logger = logging.getLogger(__name__)


class KikV3ApiClient:
    """
    KİK decision client using Tavily for search/discovery and direct HTTP
    requests for document retrieval.

    Architecture:
        Tavily
            ↓
        Search KİK indexed pages
            ↓
        Extract document URL / KararId
            ↓
        Direct HTTP request
            ↓
        MarkItDown
            ↓
        Markdown document

    This follows the same pattern as BddkApiClient.
    """

    TAVILY_API_URL = "https://api.tavily.com/search"

    BASE_URL = "https://ekapv2.kik.gov.tr"

    # Main KİK decision pages / APIs.
    # These are useful as Tavily domain restrictions and URL fallbacks.
    DOCUMENT_BASE_URL = "https://ekapv2.kik.gov.tr"

    LEGACY_DOCUMENT_BASE_URL = "https://ekap.kik.gov.tr"

    DOCUMENT_MARKDOWN_CHUNK_SIZE = 5000

    # Keep these only if you still want to support numeric IDs that need
    # encryption when constructing a document URL.
    DOCUMENT_ID_ENCRYPTION_KEY = bytes([
        236, 193, 164, 43, 12, 135, 121, 170, 4, 244, 123, 219, 82,
        158, 124, 174, 174, 228, 219, 174, 208, 104, 174, 120, 32,
        76, 250, 4, 143, 159, 211, 176
    ])

    def __init__(
            self,
            tavily_api_key: Optional[str] = None,
            request_timeout: float = 60.0,
    ):
        """
        Initialize the KİK client.

        Tavily key should preferably come from TAVILY_API_KEY.

        IMPORTANT:
            Do not hard-code Tavily API keys in source code.
        """

        self.tavily_api_key = (
                tavily_api_key
                or os.getenv("TAVILY_API_KEY")
        )

        if not self.tavily_api_key:
            raise ValueError(
                "Tavily API key is required. "
                "Set TAVILY_API_KEY or pass tavily_api_key explicitly."
            )

        # SSL configuration for KİK's legacy infrastructure.
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ssl_context.options |= ssl.OP_LEGACY_SERVER_CONNECT

        ssl_context.set_ciphers(
            "ALL:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5:!PSK:!SRP:!CAMELLIA"
        )

        self.http_client = httpx.AsyncClient(
            verify=ssl_context,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            },
            timeout=httpx.Timeout(request_timeout),
            follow_redirects=True,
        )

        # MarkItDown is synchronous, therefore calls should be executed
        # through asyncio.to_thread() when converting documents.
        self.markitdown = MarkItDown()

    # ------------------------------------------------------------------
    # URL / ID helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_karar_id(url: str) -> Optional[str]:
        """
        Extract KararId from a KİK document URL.

        Examples:
            ?KararId=12345
            ?KararId=abc123...
            &KararId=...
        """

        if not url:
            return None

        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            for key in ("KararId", "kararId", "kararid"):
                values = query.get(key)
                if values:
                    return values[0]

        except Exception:
            pass

        # Fallback regex.
        match = re.search(
            r"[?&](?:KararId|kararId|kararid)=([^&#]+)",
            url,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_numeric_document_id(url: str) -> Optional[str]:
        """
        Try to extract a numeric KİK document ID from a URL.

        This is intentionally conservative because KİK commonly uses
        encrypted KararId values in public document URLs.
        """

        if not url:
            return None

        karar_id = KikV3ApiClient._extract_karar_id(url)

        if karar_id and karar_id.isdigit():
            return karar_id

        # Common numeric ID patterns that may occur in indexed URLs.
        patterns = [
            r"gundemMaddesiId[=/](\d+)",
            r"gundem-maddesi[=/](\d+)",
            r"karar[=/](\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    @staticmethod
    def _extract_decision_number(
            title: str,
            content: str,
    ) -> str:
        """
        Try to extract a decision number from Tavily title/content.

        Typical examples:
            2025/UH.II-1801
            2024/UM.I-1234
            2025/DU.III-123
        """

        text = f"{title}\n{content}"

        patterns = [
            r"\b\d{4}/[A-ZÇĞİÖŞÜ]{1,5}\.[A-ZÇĞİÖŞÜ]{1,5}-\d+\b",
            r"\b\d{4}/[A-ZÇĞİÖŞÜ]{1,5}-\d+\b",
            r"\b\d{4}/[A-ZÇĞİÖŞÜ0-9.\-]+-\d+\b",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)

        return ""

    @staticmethod
    def _extract_date(
            title: str,
            content: str,
    ) -> str:
        """Extract a Turkish-style decision date when present."""

        text = f"{title}\n{content}"

        patterns = [
            r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b",
            r"\b\d{4}[./-]\d{1,2}[./-]\d{1,2}\b",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)

        return ""

    # ------------------------------------------------------------------
    # Tavily search
    # ------------------------------------------------------------------

    async def search_decisions(
            self,
            decision_type: "KikV2DecisionType" = None,
            karar_metni: str = "",
            karar_no: str = "",
            basvuran: str = "",
            idare_adi: str = "",
            baslangic_tarihi: str = "",
            bitis_tarihi: str = "",
            page: int = 1,
            page_size: int = 10,
    ) -> "KikV2SearchResult":
        """
        Search KİK decisions through Tavily.

        Unlike the original implementation, this method does not call
        GetKurulKararlari*. Tavily is used as the discovery layer.

        Because Tavily does not provide conventional offset pagination,
        page > 1 is currently handled by requesting additional results
        and slicing locally.
        """

        try:
            # ----------------------------------------------------------
            # Build Tavily query
            # ----------------------------------------------------------

            query_parts = [
                "site:ekapv2.kik.gov.tr",
                "Kamu İhale Kurulu",
                "Kurul Kararı",
            ]

            if karar_metni:
                query_parts.append(f'"{karar_metni}"')

            if karar_no:
                query_parts.append(f'"{karar_no}"')

            if basvuran:
                query_parts.append(f'"{basvuran}"')

            if idare_adi:
                query_parts.append(f'"{idare_adi}"')

            if baslangic_tarihi:
                query_parts.append(f'"{baslangic_tarihi}"')

            if bitis_tarihi:
                query_parts.append(f'"{bitis_tarihi}"')

            # Add decision type information to the query where possible.
            if decision_type is not None:
                decision_type_value = getattr(
                    decision_type,
                    "value",
                    str(decision_type),
                )

                if decision_type_value:
                    query_parts.append(
                        f'"{decision_type_value}"'
                    )

            query = " ".join(query_parts)

            logger.info(
                "KikV2ApiClient: Tavily search query: %s",
                query,
            )

            # ----------------------------------------------------------
            # Tavily request
            # ----------------------------------------------------------

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.tavily_api_key}",
            }

            # Tavily does not have normal page/offset pagination.
            # Request extra records so we can provide a limited local page.
            requested_results = min(
                max(page_size * page, page_size),
                50,
            )

            payload = {
                "query": query,
                "country": "turkey",
                "include_domains": [
                    "ekapv2.kik.gov.tr",
                    "ekap.kik.gov.tr",
                ],
                "max_results": requested_results,
                "search_depth": "advanced",
                "include_answer": False,
                "include_raw_content": False,
            }

            response = await self.http_client.post(
                "https://api.tavily.com/search",
                json=payload,
                headers=headers,
            )

            response.raise_for_status()

            data = response.json()

            raw_results = data.get("results", [])

            logger.info(
                "KikV2ApiClient: Tavily returned %d results",
                len(raw_results),
            )

            # ----------------------------------------------------------
            # Convert Tavily results
            # ----------------------------------------------------------

            all_decisions = []

            for result in raw_results:
                title = (result.get("title") or "").strip()
                url = (result.get("url") or "").strip()
                content = (result.get("content") or "").strip()

                if not url:
                    continue

                karar_id = self._extract_karar_id(url)

                numeric_id = self._extract_numeric_document_id(url)

                karar_no_from_result = self._extract_decision_number(
                    title,
                    content,
                )

                karar_tarihi = self._extract_date(
                    title,
                    content,
                )

                # If user requested a specific decision number, perform
                # a final local filter as Tavily may return related pages.
                if karar_no:
                    if (
                            karar_no.lower() not in title.lower()
                            and karar_no.lower() not in content.lower()
                            and karar_no.lower() not in url.lower()
                    ):
                        continue

                # Use numeric ID where available. Otherwise use KararId.
                document_id = numeric_id or karar_id

                if not document_id:
                    logger.debug(
                        "Could not extract KİK document ID from URL: %s",
                        url,
                    )

                    # We can still return the result if it is useful.
                    # The URL is preserved in the object when supported.
                    document_id = url

                compact_decision = KikV2CompactDecision(
                    kararNo=karar_no_from_result,
                    kararTarihi=karar_tarihi,
                    basvuran="",
                    idareAdi="",
                    basvuruKonusu=content[:500],
                    gundemMaddesiId=document_id,
                    decision_type=(
                        getattr(
                            decision_type,
                            "value",
                            str(decision_type or ""),
                        )
                    ),
                )

                # If your model supports source_url, populate it.
                if hasattr(compact_decision, "source_url"):
                    compact_decision.source_url = url

                all_decisions.append(compact_decision)

            # ----------------------------------------------------------
            # Local pagination
            # ----------------------------------------------------------

            start = (page - 1) * page_size
            end = start + page_size

            decisions = all_decisions[start:end]

            return KikV2SearchResult(
                decisions=decisions,
                total_records=len(all_decisions),
                page=page,
                error_code="0",
                error_message="",
            )

        except httpx.HTTPStatusError as e:
            logger.error(
                "KikV2ApiClient: Tavily HTTP error: "
                "%s - %s",
                e.response.status_code,
                e.response.text,
            )

            if e.response.status_code == 401:
                return KikV2SearchResult(
                    decisions=[],
                    total_records=0,
                    page=page,
                    error_code="TAVILY_AUTH_ERROR",
                    error_message=(
                        "Tavily API authentication failed. "
                        "Check TAVILY_API_KEY."
                    ),
                )

            return KikV2SearchResult(
                decisions=[],
                total_records=0,
                page=page,
                error_code="HTTP_ERROR",
                error_message=(
                    f"HTTP {e.response.status_code}: "
                    f"{e.response.text}"
                ),
            )

        except Exception as e:
            logger.exception(
                "KikV2ApiClient: Unexpected Tavily search error"
            )

            return KikV2SearchResult(
                decisions=[],
                total_records=0,
                page=page,
                error_code="UNEXPECTED_ERROR",
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Document retrieval
    # ------------------------------------------------------------------

    async def get_document_markdown(
            self,
            document_id: str,
            page_number: int = 1,
            source_url: Optional[str] = None,
    ) -> "KikV2DocumentMarkdown":
        """
        Retrieve a KİK decision document directly and convert it to Markdown.

        document_id may be:
            - numeric gundemMaddesiId
            - encrypted KararId
            - a complete KİK document URL

        source_url can be supplied directly from the Tavily search result.
        """

        logger.info(
            "KikV2ApiClient: Getting document for ID: %s",
            document_id,
        )

        if not document_id or not document_id.strip():
            return KikV2DocumentMarkdown(
                document_id=document_id,
                kararNo="",
                markdown_content="",
                source_url="",
                error_message="Document ID is required",
            )

        try:
            # ----------------------------------------------------------
            # Resolve document URL
            # ----------------------------------------------------------

            document_url = None

            # Best option: Tavily already returned the actual URL.
            if source_url:
                document_url = source_url

            # The caller may have passed the URL as document_id.
            elif document_id.startswith("http://") or \
                    document_id.startswith("https://"):
                document_url = document_id

            else:
                karar_id = document_id

                # Numeric IDs may need to be encrypted before being
                # inserted into the KİK document URL.
                if document_id.isdigit():
                    try:
                        karar_id = self.encrypt_document_id(document_id)

                        logger.info(
                            "Encrypted numeric KİK ID %s -> %s",
                            document_id,
                            karar_id,
                        )

                    except Exception as e:
                        logger.warning(
                            "Could not encrypt KİK document ID: %s",
                            e,
                        )

                # Current KİK document URL.
                document_url = (
                    "https://ekap.kik.gov.tr/"
                    "EKAP/Vatandas/"
                    f"KurulKararGoster.aspx?KararId={karar_id}"
                )

            logger.info(
                "KikV2ApiClient: Fetching document: %s",
                document_url,
            )

            # ----------------------------------------------------------
            # Fetch document
            # ----------------------------------------------------------

            response = await self.http_client.get(
                document_url,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                    "Referer": self.BASE_URL,
                },
                follow_redirects=True,
            )

            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()

            logger.info(
                "KikV2ApiClient: Retrieved %d bytes, content-type=%s",
                len(response.content),
                content_type,
            )

            # ----------------------------------------------------------
            # Convert to Markdown
            # ----------------------------------------------------------

            if "pdf" in content_type:
                file_extension = ".pdf"
            else:
                file_extension = ".html"

            document_stream = io.BytesIO(response.content)

            # MarkItDown is synchronous.
            result = await asyncio.to_thread(
                self.markitdown.convert_stream,
                document_stream,
                file_extension=file_extension,
            )

            markdown_content = (result.text_content or "").strip()

            total_length = len(markdown_content)

            total_pages = max(
                1,
                math.ceil(total_length / self.DOCUMENT_MARKDOWN_CHUNK_SIZE)
            )

            start_idx = (page_number - 1) * self.DOCUMENT_MARKDOWN_CHUNK_SIZE

            end_idx = start_idx + self.DOCUMENT_MARKDOWN_CHUNK_SIZE

            page_content = markdown_content[start_idx:end_idx]

            return KikV2DocumentMarkdown(
                document_id=document_id,
                kararNo="",
                markdown_content=page_content,
                source_url=str(response.url),
                error_message="",
                page_number=page_number,
                total_pages=total_pages,
            )

        except httpx.HTTPStatusError as e:
            logger.error(f'KikV2ApiClient: HTTP error retrieving document {document_id}: {e}')

            return KikV2DocumentMarkdown(
                document_id=document_id,
                kararNo="",
                markdown_content="",
                source_url=document_url or "",
                error_message=f'HTTP {e.response.status_code}: {e.response.text}',
            )

        except Exception as e:
            logger.exception(f'KikV2ApiClient: Error retrieving document {document_id}')

            return KikV2DocumentMarkdown(
                document_id=document_id,
                kararNo="",
                markdown_content="",
                source_url=document_url or "",
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Encryption
    # ------------------------------------------------------------------

    @staticmethod
    def encrypt_document_id(numeric_id: str) -> str:
        """
        Encrypt a numeric KİK gundemMaddesiId to the 64-character
        hexadecimal KararId.

        AES-256-CBC + PKCS7.
        """

        if not HAS_CRYPTOGRAPHY:
            raise ImportError("cryptography library required for document ID encryption")

        iv = os.urandom(16)

        cipher = Cipher(
            algorithms.AES(KikV3ApiClient.DOCUMENT_ID_ENCRYPTION_KEY),
            modes.CBC(iv),
            backend=default_backend(),
        )

        encryptor = cipher.encryptor()

        plaintext = numeric_id.encode("utf-8")

        block_size = 16
        padding_len = block_size - (len(plaintext) % block_size)

        padded_plaintext = plaintext + bytes([padding_len] * padding_len)

        ciphertext = encryptor.update(padded_plaintext) + encryptor.finalize()

        return iv.hex() + ciphertext.hex()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close_client_session(self):
        """Close HTTP client session."""

        await self.http_client.aclose()

        logger.info("KikV2ApiClient: HTTP client session closed.")
