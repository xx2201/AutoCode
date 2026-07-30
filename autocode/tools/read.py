"""Unified, workspace-bound reading for text, images, PDFs, and notebooks."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
from pathlib import Path

from .base import ConcurrencySpec, Tool, ToolResult
from .file_state import DEFAULT_FILE_READ_TRACKER

_IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
_MAX_TEXT_CHARS = 50_000
_MAX_SINGLE_LINE_CHARS = 20_000
_DEFAULT_LINE_LIMIT = 2_000
_MAX_PDF_PAGES = 20
_IMAGE_MAX_DIMENSION = 2_000
_IMAGE_TARGET_BYTES = 5_000_000
_IMAGE_FORMAT_MEDIA_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class ReadTool(Tool):
    name = "read"
    description = (
        "Read a workspace file. Text is returned with line numbers; images are sent for visual "
        "inspection; PDFs support page ranges; Jupyter notebooks are rendered by cell. Partial "
        "text reads do not satisfy the read-before-edit requirement."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file"},
            "offset": {
                "type": "integer",
                "description": "First text line to return (1-based). Default 1.",
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum text lines to return. Default 2000.",
                "minimum": 1,
            },
            "pages": {
                "type": "string",
                "description": "PDF pages, for example '1-5' or '3'. At most 20 pages.",
            },
            "detail": {
                "type": "string",
                "enum": ["auto", "low", "high"],
                "description": "Vision detail level for images. Default auto.",
            },
        },
        "required": ["file_path"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.resources(
            reads={self.file_resource(str(arguments["file_path"]))},
            reason="file reads may share a path but must not overlap a write",
        )

    def execute(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = _DEFAULT_LINE_LIMIT,
        pages: str | None = None,
        detail: str = "auto",
    ) -> str | ToolResult:
        try:
            path = self._resolve_file(file_path)
            suffix = path.suffix.lower()
            media_type = mimetypes.guess_type(path.name)[0] or ""
            if media_type in _IMAGE_TYPES:
                return self._read_image(path, detail)
            if suffix == ".pdf":
                return self._read_pdf(path, pages)
            if suffix == ".ipynb":
                return self._read_notebook(path)
            return self._read_text(path, offset, limit)
        except Exception as exc:
            return f"Error: {exc}"

    def _resolve_file(self, file_path: str) -> Path:
        fs = getattr(self, "_fs", None)
        path = fs.resolve_path(file_path) if fs else Path(file_path).expanduser().resolve()
        if fs:
            fs.ensure_within_workspace(path)
        if not path.exists():
            raise FileNotFoundError(f"{file_path} not found")
        if not path.is_file():
            raise IsADirectoryError(f"{file_path} is a directory, not a file")
        return path

    def _read_text(self, path: Path, offset: int, limit: int) -> str:
        if offset < 1 or limit < 1:
            return "Error: offset and limit must be positive integers"

        selected: list[str] = []
        full_lines: list[str] = []
        total = 0
        partial = offset != 1
        char_count = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for total, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if offset == 1 and not partial:
                    full_lines.append(raw_line)
                if total < offset:
                    continue
                if len(selected) >= limit:
                    partial = True
                    continue
                if len(line) > _MAX_SINGLE_LINE_CHARS:
                    line = line[:_MAX_SINGLE_LINE_CHARS] + "… [line truncated]"
                    partial = True
                rendered = f"{total}\t{line}"
                if selected and char_count + len(rendered) > _MAX_TEXT_CHARS:
                    partial = True
                    continue
                selected.append(rendered)
                char_count += len(rendered) + 1

        if not partial and offset == 1 and len(selected) == total:
            content = "".join(full_lines)
            self._tracker().record(path, content)

        if total == 0:
            self._tracker().record(path, "")
            return "(empty file)"
        result = "\n".join(selected)
        if partial:
            end = offset + len(selected) - 1
            result += (
                f"\n\nPARTIAL view: {total} lines total, showing {offset}-{max(offset, end)}. "
                "Read the complete file before edit_file or write_file."
            )
        return result

    def _read_image(self, path: Path, detail: str) -> ToolResult:
        from PIL import Image

        original = path.read_bytes()
        with Image.open(path) as source:
            source_format = str(source.format or "").upper()
            media_type = _IMAGE_FORMAT_MEDIA_TYPES.get(source_format)
            width, height = source.size
            if (
                media_type is not None
                and max(width, height) <= _IMAGE_MAX_DIMENSION
                and len(original) <= _IMAGE_TARGET_BYTES
            ):
                payload = original
            else:
                image = source.convert("RGB")
                image.thumbnail((_IMAGE_MAX_DIMENSION, _IMAGE_MAX_DIMENSION))
                width, height = image.size
                output = io.BytesIO()
                quality = 90
                image.save(output, format="JPEG", quality=quality, optimize=True)
                while output.tell() > _IMAGE_TARGET_BYTES and quality > 45:
                    quality -= 10
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=quality, optimize=True)
                payload = output.getvalue()
                media_type = "image/jpeg"
        encoded = base64.b64encode(payload).decode("ascii")
        relative = self._display_path(path)
        return ToolResult(
            text=(
                f"Loaded image for visual inspection: {relative} "
                f"({width}x{height}, {len(payload)} bytes)"
            ),
            model_content=[{
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}", "detail": detail},
            }],
        )

    def _read_pdf(self, path: Path, pages: str | None) -> str:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            return "Error: encrypted PDFs are not supported"
        total = len(reader.pages)
        requested = self._parse_pages(pages, total)
        if pages is None:
            requested = list(range(min(total, 10 if total > 10 else total)))
        chunks = []
        empty_pages = []
        for index in requested:
            text = reader.pages[index].extract_text() or ""
            if not text.strip():
                empty_pages.append(index + 1)
            chunks.append(f"--- Page {index + 1} of {total} ---\n{text.strip() or '[No extractable text]'}")
        result = "\n\n".join(chunks)
        if total > len(requested):
            result += (
                f"\n\nPARTIAL view: PDF has {total} pages; showing "
                f"{requested[0] + 1}-{requested[-1] + 1}. Use pages to read another range."
            )
        if empty_pages:
            result += (
                "\n\nWarning: no extractable text on page(s) "
                + ", ".join(map(str, empty_pages))
                + "; scanned pages may require OCR."
            )
        if len(requested) == total:
            self._tracker().record(path, path.read_bytes())
        return result or "(empty PDF)"

    def _read_notebook(self, path: Path) -> str:
        raw = path.read_text(encoding="utf-8")
        notebook = json.loads(raw)
        sections = []
        for index, cell in enumerate(notebook.get("cells", []), start=1):
            source = "".join(cell.get("source", []))
            section = [f"--- Cell {index} [{cell.get('cell_type', 'unknown')}] ---", source]
            for output in cell.get("outputs", []):
                text = output.get("text")
                if text is None:
                    text = output.get("data", {}).get("text/plain", [])
                if text:
                    section.append("[output]\n" + "".join(text))
            sections.append("\n".join(section))
        rendered = "\n\n".join(sections)
        if len(rendered) > _MAX_TEXT_CHARS:
            return (
                rendered[:_MAX_TEXT_CHARS]
                + f"\n\nPARTIAL view: notebook output exceeded {_MAX_TEXT_CHARS} characters."
            )
        self._tracker().record(path, raw)
        return rendered or "(empty notebook)"

    @staticmethod
    def _parse_pages(spec: str | None, total: int) -> list[int]:
        if not spec:
            return []
        if "-" in spec:
            start_text, end_text = spec.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(spec)
        if start < 1 or end < start or end > total:
            raise ValueError(f"pages must be within 1-{total}")
        if end - start + 1 > _MAX_PDF_PAGES:
            raise ValueError(f"read at most {_MAX_PDF_PAGES} PDF pages at once")
        return list(range(start - 1, end))

    def _tracker(self):
        return getattr(self, "_file_read_tracker", DEFAULT_FILE_READ_TRACKER)

    def _display_path(self, path: Path) -> str:
        fs = getattr(self, "_fs", None)
        return path.relative_to(fs.workspace_root).as_posix() if fs else str(path)
