"""Validate file-tool requests before creating any output or contacting Open WebUI."""
from typing import Any
import math

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator

from .file_security import FilePolicyError


def _grid(value, *, rectangular=False, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError("content must be a list of non-empty rows")
    width = None
    for row in value:
        if not isinstance(row, list) or not row:
            raise ValueError("content must be a list of non-empty rows")
        if rectangular and width is not None and len(row) != width:
            raise ValueError("XLSX content must be a rectangular grid")
        width = len(row)
        for cell in row:
            if cell is not None and not isinstance(cell, (str, int, float, bool)):
                raise ValueError("table cells must be strings, numbers, booleans or null")
            if isinstance(cell, float) and not math.isfinite(cell):
                raise ValueError("table numbers must be finite")


def _document(content, fmt):
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise ValueError(f"{fmt.upper()} content must be text or a list of text/document blocks")
    text_types = {"title", "subtitle", "paragraph"}
    if fmt == "docx":
        text_types |= {"heading", "subheading", "bold", "numbered"}
    for item in content:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            raise ValueError("document blocks must be strings or objects")
        kind = item.get("type")
        if kind is not None and not isinstance(kind, str):
            raise ValueError("document block type must be a string")
        if kind in text_types or (fmt == "docx" and kind is None and "text" in item):
            if not isinstance(item.get("text"), str):
                raise ValueError("text blocks require a text string")
        elif kind == "list" or (fmt == "docx" and kind == "bullet"):
            items = item.get("items", [item.get("text")] if fmt == "docx" else None)
            if not isinstance(items, list) or any(not isinstance(x, str) for x in items):
                raise ValueError("list blocks require an items list of strings")
        elif kind == "table":
            if fmt == "pdf" and item.get("headers") and item.get("rows"):
                _grid(item["rows"])
                _grid([item["headers"]])
            else:
                _grid(item.get("data"))
        elif kind in {"image", "image_query"}:
            # Existing secure-export policy rejects these before any file write.
            if not isinstance(item.get("query"), str):
                raise ValueError("image blocks require a query string")
        else:
            raise ValueError(f"Unsupported {fmt.upper()} document block type; use a supported block or plain text")


class FileInput(BaseModel):
    """The value of the tool's single data field; never wrap another data object inside it."""
    model_config = ConfigDict(extra="forbid", strict=True)

    format: StrictStr = Field(
        min_length=1, pattern=r"^[A-Za-z0-9]+$",
        description="Required extension: pdf, docx, xlsx, csv, pptx, or a raw text extension such as txt, xml, json or py.",
    )
    filename: StrictStr | None = None
    title: StrictStr | None = None
    content: StrictStr | list[Any] | None = Field(
        default=None,
        description="Required except for pptx. XLSX/CSV: rows of scalar cells. PDF/DOCX: text or supported document blocks. Other formats: a non-empty string.",
    )
    slides_data: list[dict[str, Any]] | None = Field(
        default=None,
        description="Required for pptx: slide objects with a text title and content (text or a list of text/items).",
    )

    @model_validator(mode="before")
    @classmethod
    def single_wrapper(cls, value):
        if isinstance(value, dict) and "data" in value:
            raise ValueError("Use exactly one data wrapper: put format and content or slides_data directly inside data")
        return value

    @model_validator(mode="after")
    def format_payload(self):
        fmt = self.format.lower()
        if fmt == "pptx":
            if self.slides_data is None:
                raise ValueError("PPTX requires slides_data")
            if self.content is not None:
                raise ValueError("PPTX uses slides_data, not content")
            for slide in self.slides_data:
                if "title" in slide and not isinstance(slide["title"], str):
                    raise ValueError("slide title must be a string")
                content = slide.get("content", [])
                if not isinstance(content, (str, list)):
                    raise ValueError("slide content must be text or a list of text/items")
                if isinstance(content, list) and any(not isinstance(x, (str, dict)) for x in content):
                    raise ValueError("slide content items must be strings or objects")
        else:
            if self.slides_data is not None:
                raise ValueError("slides_data is only supported for PPTX")
            if self.content is None:
                raise ValueError("This format requires content directly inside data")
            if fmt in {"xlsx", "csv"}:
                _grid(self.content, rectangular=fmt == "xlsx", allow_empty=fmt == "xlsx")
            elif fmt in {"pdf", "docx"}:
                _document(self.content, fmt)
            elif not isinstance(self.content, str) or not self.content:
                raise ValueError("Raw text formats require a non-empty content string")
        return self


def validate_file_input(data):
    try:
        value = data if isinstance(data, FileInput) else FileInput.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        raise FilePolicyError(f"Invalid file data{(' at ' + location) if location else ''}: {message}") from None
    return value.model_dump(exclude_none=True)
