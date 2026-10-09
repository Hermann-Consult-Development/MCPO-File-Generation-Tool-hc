"""Input-contract regressions: reject bad tool calls before creating/uploading files."""
import asyncio
from contextlib import ExitStack
import copy
import io
import unittest
from unittest.mock import Mock, patch

from test_secure_exports import CASES, HEADERS, tool, validate_artifact


class FileInputContract(unittest.TestCase):
    def assert_rejected_without_output(self, data, expected=None, archive=False):
        names = ["_generate_unique_folder", "_create_excel", "_create_presentation",
                 "_create_word", "_create_pdf", "_create_csv", "_create_raw_file", "http_post", "http_get"]
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(tool, name)) for name in names]
            if archive:
                result = asyncio.run(tool.generate_and_archive([CASES[0], data], mcpo_headers=HEADERS))
            else:
                result = asyncio.run(tool.create_file(data, mcpo_headers=HEADERS))
            self.assertFalse(result.get("success"), result)
            if expected:
                self.assertIn(expected, result["error"]["message"])
            for mock in mocks:
                mock.assert_not_called()

    def test_captured_double_wrappers_fail_before_generation(self):
        excel = {"data": {"format": "xlsx", "filename": "costs.xlsx",
                          "content": [["2024", "2025", "Difference"], [60000, 65100, "=B2-A2"]]},
                 "formats": {"currency_cells": ["A2:C2"]}}
        slides = {"data": {"format": "pptx", "filename": "update.pptx", "content": None,
                           "slides_data": [{"title": "Update", "content": ["No order placed"]}]}}
        for captured in [excel, slides]:
            with self.subTest(captured=captured):
                self.assert_rejected_without_output(captured, "exactly one data wrapper")

    def test_missing_empty_and_wrong_format_types_fail(self):
        for data in [{}, {"content": "hello"}, {"format": "", "content": "hello"},
                     {"format": " ", "content": "hello"}, {"format": 123, "content": "hello"},
                     {"format": "../txt", "content": "hello"}, {"format": "txt", "content": "", "formats": {}}]:
            with self.subTest(data=data):
                self.assert_rejected_without_output(data)

    def test_invalid_payload_types_fail_before_generation(self):
        cases = [
            {"format": "xlsx"}, {"format": "xlsx", "content": "A,B"},
            {"format": "xlsx", "content": [[1, 2], [3]]},
            {"format": "xlsx", "content": [[{"nested": 1}]]},
            {"format": "xlsx", "content": [[float("nan")]]},
            {"format": "csv", "content": []}, {"format": "csv", "content": ["a,b"]},
            {"format": "docx", "content": 42}, {"format": "pdf", "content": {"text": "missing list"}},
            {"format": "pptx", "content": "No slides_data"},
            {"format": "pptx", "slides_data": ["not an object"]},
            {"format": "pptx", "slides_data": [{"title": 42}]},
            {"format": "pptx", "slides_data": [{"content": [42]}]},
            {"format": "json", "content": {"must": "be serialized explicitly"}},
            {"format": "txt", "content": ""},
        ]
        for data in cases:
            with self.subTest(data=data): self.assert_rejected_without_output(data)

    def test_unknown_document_blocks_are_not_silently_omitted(self):
        for fmt, block in [("pdf", {"type": "heading", "text": "Unsupported by current PDF renderer"}),
                           ("docx", {"type": "invented", "text": "Must not disappear"})]:
            with self.subTest(fmt=fmt):
                self.assert_rejected_without_output({"format": fmt, "content": [block]}, "Unsupported")

    def test_docx_list_strings_and_untyped_text_are_preserved(self):
        from docx import Document
        uploaded = []
        def upload(url, **kwargs):
            uploaded.append(kwargs["files"]["file"][1].read())
            return Mock(status_code=200, json=lambda: {"id": "docx-list-strings"})
        content = [
            {"type": "bullet", "items": "Bullet as one string"},
            {"type": "list", "items": "List as one string"},
            {"text": "Legacy untyped text"},
        ]
        with patch.object(tool, "http_post", upload):
            result = asyncio.run(tool.create_file({"format": "docx", "content": content}, mcpo_headers=HEADERS))
        self.assertTrue(result["success"], result)
        self.assertEqual(len(uploaded), 1)
        paragraphs = [p.text for p in Document(io.BytesIO(uploaded[0])).paragraphs]
        for text in ["Bullet as one string", "List as one string", "Legacy untyped text"]:
            self.assertEqual(paragraphs.count(text), 1)

    def test_explicit_null_block_type_fails_before_generation(self):
        for fmt in ["docx", "pdf"]:
            data = {"format": fmt, "content": [{"type": None, "text": "Must not disappear"}]}
            with self.subTest(fmt=fmt):
                self.assert_rejected_without_output(data, "block type must be a string")
                self.assert_rejected_without_output(data, "block type must be a string", archive=True)

    def test_archive_validates_all_files_before_generating_the_first(self):
        self.assert_rejected_without_output({"data": {"format": "xlsx", "content": [[1]]}},
                                            "exactly one data wrapper", archive=True)

    def test_correct_five_formats_still_generate_real_files(self):
        for case in CASES:
            uploaded = []
            def upload(url, **kwargs):
                uploaded.append(kwargs["files"]["file"][1].read())
                return Mock(status_code=200, json=lambda: {"id": "validated-file"})
            with self.subTest(fmt=case["format"]), patch.object(tool, "http_post", upload):
                result = asyncio.run(tool.create_file(copy.deepcopy(case), mcpo_headers=HEADERS))
                self.assertTrue(result["success"], result)
                self.assertEqual(len(uploaded), 1)
                validate_artifact(case["format"], uploaded[0])

    def test_supported_raw_text_outputs_are_preserved(self):
        for fmt, content in [("txt", "plain text"), ("xml", "<root/>"),
                             ("json", '{"value": 1}'), ("py", 'print("hello")'), ("md", "# Heading")]:
            uploaded = []
            def upload(url, **kwargs):
                uploaded.append(kwargs["files"]["file"][1].read().decode("utf-8"))
                return Mock(status_code=200, json=lambda: {"id": "raw-file"})
            with self.subTest(fmt=fmt), patch.object(tool, "http_post", upload):
                result = asyncio.run(tool.create_file({"format": fmt, "content": content}, mcpo_headers=HEADERS))
                self.assertTrue(result["success"], result)
                self.assertEqual(len(uploaded), 1)
                self.assertTrue(uploaded[0].endswith(content))

    def test_exported_mcp_schema_requires_format_and_forbids_inner_wrapper(self):
        tools = asyncio.run(tool.mcp.list_tools())
        exported = next(item for item in tools if item.name == "create_file")
        schema = exported.inputSchema
        self.assertIn("data", schema["required"])
        data_schema = schema["properties"]["data"]
        if "$ref" in data_schema:
            data_schema = schema["$defs"][data_schema["$ref"].rsplit("/", 1)[-1]]
        self.assertIn("format", data_schema["required"])
        self.assertFalse(data_schema["additionalProperties"])
        self.assertNotIn("data", data_schema["properties"])
        self.assertEqual(data_schema["properties"]["format"]["minLength"], 1)
        self.assertIn("exactly one data wrapper", exported.description)

    def test_mcpo_openapi_preserves_required_format(self):
        from fastapi import FastAPI
        from mcpo.utils.main import get_model_fields, get_tool_handler
        exported = next(item for item in asyncio.run(tool.mcp.list_tools()) if item.name == "create_file")
        source = exported.inputSchema
        fields = get_model_fields("create_file_form_model", source["properties"], source["required"], source.get("$defs", {}))
        app = FastAPI()
        app.post("/create_file")(get_tool_handler(Mock(), "create_file", fields, None, {"enabled": False}))
        schema = app.openapi()
        body = schema["paths"]["/create_file"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        def resolve(value):
            return schema["components"]["schemas"][value["$ref"].rsplit("/", 1)[-1]] if "$ref" in value else value
        body = resolve(body)
        self.assertIn("data", body["required"])
        data = resolve(body["properties"]["data"])
        self.assertIn("format", data["required"])
        self.assertEqual(data["properties"]["format"]["type"], "string")
        self.assertNotIn("data", data["properties"])
        from fastapi.testclient import TestClient
        with patch.object(tool, "_generate_unique_folder") as folder, patch.object(tool, "http_post") as post:
            with TestClient(app) as client:
                for fmt in ["xlsx", "pptx"]:
                    response = client.post("/create_file", json={"data": {"data": {"format": fmt}}, "mcpo_headers": HEADERS})
                    self.assertEqual(response.status_code, 422)
                    self.assertTrue(any(error["loc"][-1] == "format" for error in response.json()["detail"]))
            folder.assert_not_called(); post.assert_not_called()

    def test_real_mcp_dispatch_rejects_wrapper_and_accepts_valid_body(self):
        from mcp.server.fastmcp.exceptions import ToolError
        with patch.object(tool, "_generate_unique_folder") as folder, patch.object(tool, "http_post") as post:
            with self.assertRaises(ToolError) as rejected:
                asyncio.run(tool.mcp.call_tool("create_file", {"data": {"data": {"format": "xlsx", "content": [[1]]}},
                                                             "mcpo_headers": HEADERS}))
            self.assertIn("exactly one data wrapper", str(rejected.exception))
            folder.assert_not_called(); post.assert_not_called()
        with patch.object(tool, "http_post", return_value=Mock(status_code=200, json=lambda: {"id": "mcp-valid"})) as post:
            result = asyncio.run(tool.mcp.call_tool("create_file", {"data": CASES[3], "mcpo_headers": HEADERS}))
            self.assertEqual(post.call_count, 1)
            self.assertIn("mcp-valid", str(result))


if __name__ == "__main__":
    unittest.main()
