from typing import List
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Customer detail extraction schemas
# ---------------------------------------------------------------------------

class ExtractionResult(BaseModel):
    name: str | None = Field(default=None)
    master_account_number: str | None = Field(default=None)
    sub_account_number: str | None = Field(default=None)
    fi_num: str | None = Field(default=None)
    bank_name: str | None = Field(default=None)  


class ExtractionMeta(BaseModel):
    input_characters: int
    llm_called: bool
    source: str


class ExtractResponse(BaseModel):
    success: bool
    message: str
    data: ExtractionResult
    meta: ExtractionMeta


# ---------------------------------------------------------------------------
# Shared LLM communication schemas
# ---------------------------------------------------------------------------

class LLMRequestPayload(BaseModel):
    prompt: str
    model: str | None = None


class LLMRawResponse(BaseModel):
    content: str


class ErrorResponse(BaseModel):
    success: bool = False
    message: str