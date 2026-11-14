import unittest

from mini_pilot.apps import (
    get_capture_app_key,
    get_package,
    infer_capture_app_key,
    register_custom_app,
)
from mini_pilot.capture import infer_unicapture_selection
from unicapture.app_configs import get_app_config, resolve_app_package


class UnifiedAppMappingTests(unittest.TestCase):
    def test_executor_resolves_capturer_key_and_natural_alias(self):
        self.assertEqual(get_package("tencent_news"), "com.tencent.news")
        self.assertEqual(get_package("腾讯新闻"), "com.tencent.news")
        self.assertEqual(resolve_app_package("腾讯新闻"), "com.tencent.news")

    def test_natural_alias_resolves_to_capture_catalog_entry(self):
        self.assertEqual(get_capture_app_key("腾讯新闻"), "tencent_news")
        self.assertEqual(infer_capture_app_key("打开腾讯新闻app浏览五分钟"), "tencent_news")
        config = get_app_config("腾讯新闻")
        self.assertIsNotNone(config)
        self.assertEqual(config.name, "tencent_news")
        self.assertEqual(config.package, "com.tencent.news")

    def test_goal_inference_uses_unified_catalog(self):
        inferred = infer_unicapture_selection("打开腾讯新闻app，模拟真人操作5分钟")
        self.assertEqual(inferred["app"], "tencent_news")
        self.assertEqual(inferred["package"], "com.tencent.news")
        self.assertEqual(inferred["scene"], "feed")
        self.assertEqual(inferred["duration"], 300)

    def test_capturer_package_wins_over_legacy_executor_value(self):
        config = get_app_config("doubao_ai")
        self.assertIsNotNone(config)
        self.assertEqual(config.package, "com.larus.nova")
        self.assertEqual(get_package("豆包"), config.package)
        self.assertEqual(get_capture_app_key("豆包"), "doubao_ai")

        huya = get_app_config("huya_live")
        self.assertIsNotNone(huya)
        self.assertEqual(huya.package, "com.duowan.kiwi")
        self.assertEqual(get_package("虎牙直播"), huya.package)

    def test_direct_package_and_runtime_custom_alias_remain_supported(self):
        self.assertEqual(get_package("com.tencent.news"), "com.tencent.news")
        register_custom_app("测试应用", "com.example.testapp")
        self.assertEqual(get_package("测试应用"), "com.example.testapp")

    def test_jcc_uses_confirmed_android_package(self):
        config = get_app_config("jcc")
        self.assertIsNotNone(config)
        self.assertEqual(config.package, "com.tencent.jkchess")
        self.assertEqual(get_package("金铲铲之战"), "com.tencent.jkchess")
        self.assertEqual(get_capture_app_key("com.tencent.jkchess"), "jcc")


if __name__ == "__main__":
    unittest.main()
