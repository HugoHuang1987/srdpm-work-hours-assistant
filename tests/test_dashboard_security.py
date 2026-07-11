import json
import unittest
from pathlib import Path

import build_multi_month_dashboard as dashboard


class DashboardSecurityTests(unittest.TestCase):
    def test_inline_json_cannot_close_script_tag(self):
        attack = "</script><img src=x onerror=window.__xss=1>&\u2028\u2029"
        encoded = dashboard.serialize_for_inline_script({"value": attack})

        self.assertNotIn("</script", encoded.lower())
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)
        self.assertNotIn("\u2028", encoded.replace("\\u2028", ""))
        self.assertNotIn("\u2029", encoded.replace("\\u2029", ""))
        self.assertEqual(json.loads(encoded)["value"], attack)

    def test_generated_dashboard_has_no_remote_script_dependency(self):
        dashboard.main()
        html = Path(dashboard.OUTPUT_HTML).read_text(encoding="utf-8")

        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("<script src=", html.lower())


if __name__ == "__main__":
    unittest.main()
