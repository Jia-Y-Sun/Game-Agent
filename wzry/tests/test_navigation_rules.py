import unittest
from types import SimpleNamespace

from navigation_rules import activity_close_button, logged_in_start_button, takeover_reason


def node(text, horizontal=300, vertical=100):
    return SimpleNamespace(ocr_text=text, box=[
        [horizontal - 10, vertical - 10], [horizontal + 10, vertical - 10],
        [horizontal + 10, vertical + 10], [horizontal - 10, vertical + 10],
    ])


class NavigationRulesTests(unittest.TestCase):
    def test_activity_words_alone_do_not_authorize_fixed_click(self):
        self.assertIsNone(activity_close_button([node("回归福利")], 1920, 1080))

    def test_only_exact_corner_close_is_allowed(self):
        header = node("回归福利")
        close = node("关闭", 1830, 70)
        self.assertIs(activity_close_button([header, close], 1920, 1080), close)
        self.assertIsNone(activity_close_button([header, node("X特惠", 1830, 70)], 1920, 1080))
        self.assertIsNone(activity_close_button([header, node("关闭", 960, 70)], 1920, 1080))

    def test_login_start_requires_existing_session_context(self):
        start = node("开始游戏", 960, 850)
        self.assertIsNone(logged_in_start_button([start]))
        self.assertIs(logged_in_start_button([start, node("换区"), node("退出")]), start)

    def test_health_footer_is_not_real_verification_form(self):
        footer = node("请您确保已经通过实名认证，为了您的健康请合理安排游戏时间并享受健康生活", 960, 1040)
        self.assertIsNone(takeover_reason([footer], 1080))
        self.assertEqual(takeover_reason([node("实名认证", 960, 500)], 1080), "verification_or_login")
        self.assertEqual(takeover_reason([footer, node("请输入验证码")], 1080), "verification_or_login")

    def test_login_selection_is_a_takeover(self):
        self.assertIsNotNone(takeover_reason([node("QQ登录")], 1080))
