from __future__ import annotations

import importlib
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WorkdayWidgetLegacyBridgeTests(unittest.TestCase):
    def test_legacy_bridge_import_has_no_playwright_server_side_effect(self):
        server_module = "mcp_servers.playwright_server"
        already_loaded = server_module in sys.modules

        module = importlib.import_module("adapters.workday.widgets.legacy_bridge")

        self.assertTrue(callable(module.choose_prompt_option))
        self.assertTrue(callable(module.choose_radio_value))
        self.assertTrue(callable(module.fill_text_value))
        self.assertTrue(callable(module.fill_date_value))
        self.assertTrue(callable(module.upload_file_value))
        if not already_loaded:
            self.assertNotIn(server_module, sys.modules)


if __name__ == "__main__":
    unittest.main()
