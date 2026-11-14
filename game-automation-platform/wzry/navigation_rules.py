"""Pure OCR decisions; callers retain responsibility for device input."""


def center(node):
    points = node.box
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def takeover_reason(results, height):
    for node in results or []:
        text = str(node.ocr_text).strip()
        markers = ("请登录", "请先登录", "QQ登录", "微信登录", "扫码登录", "验证码",
                   "人脸识别", "账号安全", "支付密码", "实名认证", "实名验证")
        if not any(marker in text for marker in markers):
            continue
        footer = ("健康" in text or "合理安排" in text) and len(text) > 25
        if footer and center(node)[1] >= height * 0.88 and not any(
            marker in text for marker in ("验证码", "人脸识别", "支付密码", "请登录")
        ):
            continue
        return "verification_or_login"
    return None


def logged_in_start_button(results):
    texts = [str(node.ocr_text).strip() for node in results or []]
    if not any("换区" in text for text in texts) or "退出" not in texts:
        return None
    return next((node for node in results if str(node.ocr_text).strip() == "开始游戏"), None)


def activity_close_button(results, width, height):
    header = [node for node in results or [] if center(node)[1] < height * 0.28]
    words = ("公告", "活动", "回归", "福利", "签到", "更新")
    if sum(any(word in str(node.ocr_text) for node in header) for word in words) < 2:
        return None
    for node in header:
        text = str(node.ocr_text).strip()
        horizontal, vertical = center(node)
        if text in {"关闭", "跳过", "取消", "稍后", "X", "×", "返回"} and (
            horizontal <= width * 0.15 or horizontal >= width * 0.85
        ) and vertical <= height * 0.2:
            return node
    return None
