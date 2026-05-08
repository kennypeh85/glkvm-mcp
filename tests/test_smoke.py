"""
Smoke test for glkvm_mcp.py.

Imports the server module without launching the stdio loop and asserts that the
expected MCP tools are registered. Run from the repo root:

    python -m unittest tests.test_smoke

or directly:

    python tests/test_smoke.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SERVER_PATH = os.path.join(REPO_ROOT, "glkvm_mcp.py")

EXPECTED_TOOLS = {
    "kvm_connect",
    "kvm_disconnect",
    "kvm_send_text",
    "kvm_send_keys",
    "kvm_hold_key",
    "kvm_release_all",
    "kvm_mouse_move",
    "kvm_mouse_move_pct",
    "kvm_mouse_click",
    "kvm_mouse_scroll",
    "kvm_screenshot",
    "kvm_screenshot_to_file",
    "kvm_status",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("glkvm_mcp", SERVER_PATH)
    assert spec is not None and spec.loader is not None, "could not load glkvm_mcp.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["glkvm_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


class SmokeTest(unittest.TestCase):
    def test_module_imports(self):
        mod = _load_module()
        self.assertTrue(hasattr(mod, "mcp"), "module should expose `mcp` (FastMCP instance)")

    def test_tools_registered(self):
        mod = _load_module()
        tools = asyncio.run(mod.mcp.list_tools())
        names = {t.name for t in tools}
        missing = EXPECTED_TOOLS - names
        self.assertFalse(
            missing,
            msg=f"Missing tools: {sorted(missing)}. Got: {sorted(names)}",
        )

    def test_kvm_connect_signature(self):
        mod = _load_module()
        tools = asyncio.run(mod.mcp.list_tools())
        connect = next((t for t in tools if t.name == "kvm_connect"), None)
        self.assertIsNotNone(connect, "kvm_connect tool not registered")
        schema = connect.inputSchema
        required = set(schema.get("required", []))
        self.assertEqual(
            required, {"host", "password"},
            msg=f"kvm_connect required args should be host+password, got {required}",
        )
        username_default = schema.get("properties", {}).get("username", {}).get("default")
        self.assertEqual(
            username_default, "admin",
            msg=f"kvm_connect username should default to 'admin', got {username_default!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)