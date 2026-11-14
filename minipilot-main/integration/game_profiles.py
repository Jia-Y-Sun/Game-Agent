"""Game-specific defaults for the shared probe orchestrator."""

GAME_PROFILES = {
    "jcc": {
        "name": "金铲铲之战",
        "capture_app": "jcc",
        "package": "com.tencent.jkchess",
        "activity": "com.tencent.gcloud.msdk.core.policy.ZGamePolicyActivity",
        "first_goal": "打开金铲铲之战，关闭普通公告和活动弹窗，进入大厅后选择标准排位并开始匹配，接受对局，进入对局后立即停止。遇到登录验证、实名、支付或账号安全页面时停止并请求人工接管。不要充值、绑定账号或发送消息。",
        "postgame_goal": "当前处于金铲铲之战结算页面。返回大厅，重新选择标准排位并开始匹配，接受对局，进入下一局后立即停止。遇到登录验证、实名、支付或账号安全页面时停止并请求人工接管。不要充值、绑定账号或发送消息。",
        "recovery_goal": "当前金铲铲之战页面未知。关闭普通公告和活动弹窗，恢复到大厅，选择标准排位并开始匹配，接受对局，进入对局后立即停止。遇到登录验证、实名、支付或账号安全页面时停止并请求人工接管。不要充值、绑定账号或发送消息。",
    },
    "wzry": {
        "name": "王者荣耀",
        "capture_app": "honorofkings",
        "package": "com.tencent.tmgp.sgame",
        "activity": ".SGameActivity",
        "first_goal": "进入王者荣耀大厅，选择对战、单人模式、英雄练习场、单人练习场，完成英雄确认后进入对局，进入对局后立即停止。",
        "postgame_goal": "当前处于王者荣耀结算页面。点击继续返回大厅，然后进入对战、单人模式、英雄练习场、单人练习场，进入下一局后立即停止。",
        "recovery_goal": "当前王者荣耀页面未知。关闭普通活动和公告，恢复到大厅，然后进入训练营单人练习场，进入对局后立即停止。遇到登录、验证码、实名或支付页面时停止并请求人工接管。",
    },
}
