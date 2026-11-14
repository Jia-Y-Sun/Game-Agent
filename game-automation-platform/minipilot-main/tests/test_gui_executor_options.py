from pathlib import Path
import unittest

from mini_pilot.gui import _build_executor_command, _load_unicapture_catalog


class GuiExecutorOptionsTests(unittest.TestCase):
    def test_auto_inference_omits_app_and_scene_arguments(self) -> None:
        command = _build_executor_command(
            {
                "goal": "打开腾讯新闻app，模拟真人操作5分钟",
                "duration": 300,
                "capture_mode": "unicapture",
                "capture_app": "",
                # A stale scene must not leak through when App is automatic.
                "capture_scene": "feed",
            },
            live_command_file=Path("live.jsonl"),
            device_id=None,
        )

        self.assertNotIn("--capture-app", command)
        self.assertNotIn("--capture-scene", command)

    def test_explicit_app_still_forwards_app_and_scene(self) -> None:
        command = _build_executor_command(
            {
                "goal": "打开腾讯新闻app",
                "duration": 300,
                "capture_mode": "unicapture",
                "capture_app": "tencent_news",
                "capture_scene": "feed",
            },
            live_command_file=Path("live.jsonl"),
            device_id="phone-a",
        )

        self.assertIn("--capture-app", command)
        self.assertIn("tencent_news", command)
        self.assertIn("--capture-scene", command)
        self.assertIn("feed", command)

    def test_gui_display_names_come_from_unicapture_config(self) -> None:
        catalog = _load_unicapture_catalog()

        self.assertEqual("腾讯新闻", catalog["tencent_news"]["display"])
        self.assertEqual("豆包", catalog["doubao_ai"]["display"])


if __name__ == "__main__":
    unittest.main()
