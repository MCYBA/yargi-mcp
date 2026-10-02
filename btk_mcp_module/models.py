# btk_mcp_module/models.py

from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


class BtkSearchRequest(BaseModel):
    """Request model for searching BTK Board decisions."""

    keywords: str = Field("", description="Keywords searched in decision title/content metadata.")
    decision_no: str = Field("", description="BTK decision number, e.g. 2026/DK-THD/91.")
    decision_date: str = Field("", description="Decision date as YYYY-MM-DD.")
    publication_date: str = Field("", description="Publication date as YYYY-MM-DD.")
    relevant_unit: str = Field("", description="Related BTK department name.")
    page: int = Field(1, ge=1, description="Page number for results.")
    pageSize: int = Field(10, ge=1, le=50, description="Results per page.")


class BtkDecisionSummary(BaseModel):
    """Summary of a BTK Board decision from search results."""

    id: str = Field("", description="BTK content ID.")
    title: str = Field("", description="Decision title.")
    slug: str = Field("", description="BTK content slug.")
    decision_no: Optional[str] = Field(None, description="Decision number.")
    decision_date: Optional[str] = Field(None, description="Decision date.")
    publication_date: Optional[str] = Field(None, description="Publication date.")
    relevant_unit: Optional[str] = Field(None, description="Related BTK department.")
    pdf_url: Optional[HttpUrl] = Field(None, description="Direct URL of the decision PDF.")
    original_filename: Optional[str] = Field(None, description="Original PDF filename when available.")


class BtkSearchResult(BaseModel):
    """Response model for BTK Board decision search results."""

    decisions: List[BtkDecisionSummary] = Field(default_factory=list)
    total_results: int = Field(0, description="Total number of matching results.")
    page: int = Field(1, description="Current page.")
    pageSize: int = Field(10, description="Results per page.")
    total_pages: int = Field(0, description="Total result pages.")
    query_url: str = Field("", description="BTK API URL used for the search.")


class BtkDocumentMarkdown(BaseModel):
    """BTK decision PDF converted to paginated Markdown."""

    source_url: HttpUrl = Field(description="Source PDF URL.")
    markdown_chunk: Optional[str] = Field(None, description="A chunk of the Markdown content.")
    current_page: int = Field(1, description="Current Markdown chunk page.")
    total_pages: int = Field(1, description="Total Markdown chunk pages.")
    is_paginated: bool = Field(False, description="True when content spans multiple chunks.")
    error_message: Optional[str] = Field(None, description="Error message, if retrieval failed.")

    class Config:
        json_encoders = {
            HttpUrl: str
        }
