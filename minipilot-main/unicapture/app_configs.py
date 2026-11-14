#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
不同领域APP配置模板
包含常见APP的包名、采集场景、QoE指标等配置
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class SceneConfig:
    """场景配置"""
    name: str
    description: str
    duration: int  # 采集时长(秒)
    params: Dict = field(default_factory=dict)


@dataclass
class AppConfig:
    """APP配置"""
    name: str
    app_type: str  # video/game/social/shopping/cloudgame/audio/aigc/news/filetransfer/cloudphone
    package: str
    version: str = ""
    scenes: List[SceneConfig] = field(default_factory=list)
    qoe_metrics: List[str] = field(default_factory=list)
    
    def get_scene(self, scene_name: str) -> Optional[SceneConfig]:
        for scene in self.scenes:
            if scene.name == scene_name:
                return scene
        return None


# ==================== 短视频APP配置 ====================

SHORT_VIDEO_APPS = {
    "douyin": AppConfig(
        name="douyin",
        app_type="video",
        package="com.ss.android.ugc.aweme",
        scenes=[
            SceneConfig(name="feed", description="推荐流浏览", duration=300),
            SceneConfig(name="short_video", description="短视频推荐流浏览", duration=300),
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "kuaishou": AppConfig(
        name="kuaishou",
        app_type="video",
        package="com.smile.gifmaker",
        version="12.x",
        scenes=[
            SceneConfig(
                name="feed",
                description="推荐流浏览(发现页)",
                duration=300,
                params={"tab": "发现", "content_type": "短视频"}
            ),
            SceneConfig(
                name="following",
                description="关注流浏览",
                duration=300,
                params={"tab": "关注", "content_type": "短视频"}
            ),
            SceneConfig(
                name="live",
                description="直播观看",
                duration=600,
                params={"tab": "直播", "live_type": "娱乐直播/电商直播"}
            ),
            SceneConfig(
                name="search",
                description="搜索浏览",
                duration=180,
                params={"feature": "搜索视频/用户/直播"}
            ),
            SceneConfig(
                name="story",
                description="快手故事/长视频",
                duration=600,
                params={"content_type": "长视频/剧集"}
            ),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "xiaohongshu": AppConfig(
        name="xiaohongshu",
        app_type="video",
        package="com.xingin.xhs",
        version="8.x",
        scenes=[
            SceneConfig(
                name="feed",
                description="推荐流浏览(首页)",
                duration=300,
                params={"tab": "推荐", "content_type": "图文/视频混合"}
            ),
            SceneConfig(
                name="video_feed",
                description="视频流浏览",
                duration=300,
                params={"tab": "视频", "content_type": "短视频"}
            ),
            SceneConfig(
                name="live",
                description="直播观看",
                duration=600,
                params={"live_type": "电商直播/娱乐直播"}
            ),
            SceneConfig(
                name="search",
                description="搜索浏览",
                duration=180,
                params={"feature": "搜索笔记/商品"}
            ),
            SceneConfig(
                name="shop",
                description="商城浏览",
                duration=300,
                params={"feature": "浏览商品/购物车"}
            ),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "tomato_novel": AppConfig(
        name="tomato_novel",
        app_type="video",
        package="com.dragon.read",
        scenes=[
            SceneConfig(name="feed", description="推荐流/短故事浏览", duration=300),
            SceneConfig(name="reading", description="小说阅读", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "volcano_video": AppConfig(
        name="volcano_video",
        app_type="video",
        package="com.ss.android.ugc.live",
        scenes=[
            SceneConfig(name="feed", description="短视频推荐流浏览", duration=300),
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "xigua_video": AppConfig(
        name="xigua_video",
        app_type="video",
        package="com.ss.android.article.video",
        scenes=[
            SceneConfig(name="feed", description="短视频/中视频推荐流", duration=300),
            SceneConfig(name="video_play", description="视频播放", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "weishi": AppConfig(
        name="weishi",
        app_type="video",
        package="com.tencent.weishi",
        scenes=[
            SceneConfig(name="feed", description="推荐流浏览", duration=300),
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "haokan_video": AppConfig(
        name="haokan_video",
        app_type="video",
        package="com.baidu.haokan",
        scenes=[
            SceneConfig(name="feed", description="推荐流浏览", duration=300),
            SceneConfig(name="video_play", description="视频播放", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "wechat_channels": AppConfig(
        name="wechat_channels",
        app_type="video",
        package="com.tencent.mm",
        scenes=[
            SceneConfig(name="feed", description="视频号推荐流浏览", duration=300),
            SceneConfig(name="live", description="视频号直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "pipixia": AppConfig(
        name="pipixia",
        app_type="video",
        package="com.sup.android.superb",
        scenes=[
            SceneConfig(name="feed", description="推荐流浏览", duration=300),
            SceneConfig(name="video_play", description="视频播放", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "jinri_toutiao": AppConfig(
        name="jinri_toutiao",
        app_type="video",
        package="com.ss.android.article.news",
        scenes=[
            SceneConfig(name="feed", description="推荐信息流浏览", duration=300),
            SceneConfig(name="video_feed", description="短视频流浏览", duration=300),
            SceneConfig(name="news_read", description="资讯阅读", duration=300),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),
    "com.yixia.videoeditor": AppConfig(
        name="com.yixia.videoeditor",
        app_type="video",
        package="com.yixia.videoeditor",
        scenes=[
            SceneConfig(name="feed", description="Custom scene", duration=300),
        ],
        qoe_metrics=[]
    ),

    "com.meitu.meipaimv": AppConfig(
        name="com.meitu.meipaimv",
        app_type="video",
        package="com.meitu.meipaimv",
        scenes=[
            SceneConfig(name="feed", description="Custom scene", duration=300),
        ],
        qoe_metrics=[]
    ),

}

LIVE_APPS = {
    "huya_live": AppConfig(
        name="huya_live",
        app_type="video",
        package="com.duowan.kiwi",
        scenes=[
            SceneConfig(name="live", description="直播观看", duration=600),
            SceneConfig(name="game_live", description="游戏直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "migulive": AppConfig(
        name="migulive",
        app_type="video",
        package="com.migu.live",
        scenes=[
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "douyu_live": AppConfig(
        name="douyu_live",
        app_type="video",
        package="com.douyu.live",
        scenes=[
            SceneConfig(name="live", description="直播观看", duration=600),
            SceneConfig(name="game_live", description="游戏直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "taobao_live": AppConfig(
        name="taobao_live",
        app_type="video",
        package="com.taobao.taobao",
        scenes=[
            SceneConfig(name="live", description="电商直播观看", duration=600),
            SceneConfig(name="live_shopping", description="直播购物", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "yy_live": AppConfig(
        name="yy_live",
        app_type="video",
        package="com.duowan.mobile",
        scenes=[
            SceneConfig(name="live", description="娱乐直播观看", duration=600),
            SceneConfig(name="voice_live", description="语音直播", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "cc_live": AppConfig(
        name="cc_live",
        app_type="video",
        package="com.netease.cc",
        scenes=[
            SceneConfig(name="live", description="游戏直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "netease_live": AppConfig(
        name="netease_live",
        app_type="video",
        package="com.netease.live",
        scenes=[
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "kugou_live": AppConfig(
        name="kugou_live",
        app_type="video",
        package="com.kugou.android.liver",
        scenes=[
            SceneConfig(name="live", description="音乐直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "bilibili_live": AppConfig(
        name="bilibili_live",
        app_type="video",
        package="tv.danmaku.bili",
        scenes=[
            SceneConfig(name="live", description="直播观看", duration=600),
            SceneConfig(name="game_live", description="游戏直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "now_live": AppConfig(
        name="now_live",
        app_type="video",
        package="com.tencent.now",
        scenes=[
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),
    "com.kugou.fanxing": AppConfig(
        name="com.kugou.fanxing",
        app_type="video",
        package="com.kugou.fanxing",
        scenes=[
            SceneConfig(name="live", description="Custom scene", duration=300),
        ],
        qoe_metrics=[]
    ),

}

VOD_APPS = {
    "tencent_video": AppConfig(
        name="tencent_video",
        app_type="video",
        package="com.tencent.qqlive",
        scenes=[
            SceneConfig(name="movie", description="电影播放", duration=600),
            SceneConfig(name="tv_series", description="电视剧播放", duration=600),
            SceneConfig(name="cartoon", description="动漫播放", duration=600),
            SceneConfig(name="live", description="直播观看", duration=600, params={"live_type": "娱乐直播"}),
        ],
        qoe_metrics=["trust_stall", "trust_resolution", "rtt"]
    ),

    "migu_aikan": AppConfig(
        name="migu_aikan",
        app_type="video",
        package="com.cmcc.mobilevideo",
        scenes=[
            SceneConfig(name="movie", description="电影播放", duration=600),
            SceneConfig(name="tv_series", description="电视剧播放", duration=600),
            SceneConfig(name="live", description="电视直播", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "bilibili": AppConfig(
        name="bilibili",
        app_type="video",
        package="tv.danmaku.bili",
        scenes=[
            SceneConfig(name="video", description="视频播放", duration=600),
            SceneConfig(name="live", description="直播观看", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution", "rtt"]
    ),

    "iqiyi": AppConfig(
        name="iqiyi",
        app_type="video",
        package="com.qiyi.video",
        scenes=[
            SceneConfig(name="movie", description="电影播放", duration=600),
            SceneConfig(name="tv_series", description="电视剧播放", duration=600),
            SceneConfig(name="variety", description="综艺播放", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "baidu_video": AppConfig(
        name="baidu_video",
        app_type="video",
        package="com.baidu.video",
        scenes=[
            SceneConfig(name="video_play", description="视频播放", duration=600),
            SceneConfig(name="feed", description="推荐流浏览", duration=300),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "youku": AppConfig(
        name="youku",
        app_type="video",
        package="com.youku.phone",
        scenes=[
            SceneConfig(name="movie", description="电影播放", duration=600),
            SceneConfig(name="tv_series", description="电视剧播放", duration=600),
            SceneConfig(name="variety", description="综艺播放", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "mango": AppConfig(
        name="mango",
        app_type="video",
        package="com.hunantv.imgo.activity",
        version="9.0.9",
        scenes=[
            SceneConfig(
                name="movie",
                description="电影播放",
                duration=600,
                params={"channel": "电影", "pixel": ["流畅360P", "清晰480P", "高清720P", "超清1080P"]}
            ),
            SceneConfig(
                name="tv_series",
                description="电视剧播放",
                duration=600,
                params={"channel": "电视剧", "pixel": ["流畅360P", "高清720P"]}
            ),
            SceneConfig(
                name="variety",
                description="综艺播放",
                duration=600,
                params={"channel": "综艺", "pixel": ["流畅360P", "高清720P"]}
            ),
            SceneConfig(
                name="short_video",
                description="短视频浏览",
                duration=300,
                params={"channel": "短视频", "pixel": ["流畅360P", "高清720P"]}
            ),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "ximalaya": AppConfig(
        name="ximalaya",
        app_type="audio",
        package="com.ximalaya.ting.android",
        scenes=[
            SceneConfig(name="audio_playback", description="音频播放", duration=600),
            SceneConfig(name="feed", description="推荐流浏览", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "migu_video": AppConfig(
        name="migu_video",
        app_type="video",
        package="com.cmcc.mobilevideo",
        scenes=[
            SceneConfig(name="movie", description="电影播放", duration=600),
            SceneConfig(name="tv_series", description="电视剧播放", duration=600),
            SceneConfig(name="live", description="电视直播", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),

    "letv_video": AppConfig(
        name="letv_video",
        app_type="video",
        package="com.letv.android.client",
        scenes=[
            SceneConfig(name="movie", description="电影播放", duration=600),
            SceneConfig(name="tv_series", description="电视剧播放", duration=600),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),
}

MEETING_APPS = {
    "tencent_meeting": AppConfig(
        name="tencent_meeting",
        app_type="social",
        package="com.tencent.wemeet.app",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
            SceneConfig(name="audio_meeting", description="语音会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "dingtalk": AppConfig(
        name="dingtalk",
        app_type="social",
        package="com.alibaba.android.rimet",
        scenes=[
            SceneConfig(name="video_call", description="视频通话", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "xx_icenter": AppConfig(
        name="xx_icenter",
        app_type="social",
        package="com.zte.softda",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
            SceneConfig(name="voice_meeting", description="语音会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "haoshitong": AppConfig(
        name="haoshitong",
        app_type="social",
        package="com.inpor.fastmeetingcloud",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "meeting_31": AppConfig(
        name="meeting_31",
        app_type="social",
        package="com.huiyi31.qiandao",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "lark": AppConfig(
        name="lark",
        app_type="social",
        package="com.ss.android.lark",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "cloud_video": AppConfig(
        name="cloud_video",
        app_type="social",
        package="com.cmcc.android.ysx",
        scenes=[
            SceneConfig(name="video_meeting", description="云视讯会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "huawei_cloud": AppConfig(
        name="huawei_cloud",
        app_type="social",
        package="com.huawei.cloud",
        scenes=[
            SceneConfig(name="video_meeting", description="华为云会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "quanshi_cloud": AppConfig(
        name="quanshi_cloud",
        app_type="social",
        package="com.gnet.onemeeting",
        scenes=[
            SceneConfig(name="video_meeting", description="全时云会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "teams": AppConfig(
        name="teams",
        app_type="social",
        package="com.microsoft.teams",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),
}

GAME_APPS = {
    "honorofkings": AppConfig(
        name="honorofkings",
        app_type="game",
        package="com.tencent.tmgp.sgame",
        scenes=[
            SceneConfig(
                name="battle",
                description="5v5对战",
                duration=900,
                params={"game_type": "moba", "frame": "高帧率"}
            ),
            SceneConfig(
                name="lobby",
                description="大厅等待",
                duration=300,
                params={"game_type": "moba"}
            ),
            SceneConfig(
                name="loading",
                description="加载界面",
                duration=180,
                params={"game_type": "moba"}
            ),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),

    "pubgm": AppConfig(
        name="pubgm",
        app_type="game",
        package="com.tencent.tmgp.pubgmhd",
        scenes=[
            SceneConfig(name="battle", description="对战", duration=1200, params={"game_type": "fps"}),
            SceneConfig(name="lobby", description="大厅等待", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),

    "genshin": AppConfig(
        name="genshin",
        app_type="game",
        package="com.miHoYo.Yuanshen",
        scenes=[
            SceneConfig(name="gameplay", description="游戏过程", duration=900, params={"game_type": "rpg"}),
            SceneConfig(name="loading", description="加载界面", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),

    "jcc": AppConfig(
        name="jcc",
        app_type="game",
        package="com.tencent.jkchess",
        scenes=[
            SceneConfig(name="battle", description="对战", duration=900),
            SceneConfig(name="lobby", description="大厅等待", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),

    "identity_v": AppConfig(
        name="identity_v",
        app_type="game",
        package="com.netease.idv",
        scenes=[
            SceneConfig(name="battle", description="对战", duration=900),
            SceneConfig(name="lobby", description="大厅等待", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),

    "lolm": AppConfig(
        name="lolm",
        app_type="game",
        package="com.tencent.tmgp.lolm",
        scenes=[
            SceneConfig(name="battle", description="对战", duration=900),
            SceneConfig(name="lobby", description="大厅等待", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),

    "qq_game": AppConfig(
        name="qq_game",
        app_type="game",
        package="com.tencent.game",
        scenes=[
            SceneConfig(name="lobby", description="游戏大厅", duration=300),
            SceneConfig(name="gameplay", description="小游戏游玩", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "fps"]
    ),
}

CLOUDGAME_APPS = {
    "migu_cloudgame": AppConfig(
        name="migu_cloudgame",
        app_type="cloudgame",
        package="com.cmcc.cloudgame",
        scenes=[
            SceneConfig(
                name="cloud_moba",
                description="云MOBA游戏",
                duration=900,
                params={
                    "cloudgame_type": "咪咕快游",
                    "game": "王者荣耀",
                    "dev1_camera": False,
                    "dev1_mic": False
                }
            ),
            SceneConfig(
                name="cloud_fps",
                description="云FPS游戏",
                duration=900,
                params={
                    "cloudgame_type": "咪咕快游",
                    "game": "和平精英",
                }
            ),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "steam_online": AppConfig(
        name="steam_online",
        app_type="cloudgame",
        package="com.valvesoftware.android.steam.community",
        scenes=[
            SceneConfig(name="store", description="Steam商店/社区", duration=300),
            SceneConfig(name="remote_play", description="Steam远程游玩", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "steam_game": AppConfig(
        name="steam_game",
        app_type="cloudgame",
        package="com.valvesoftware.android.steam.community",
        scenes=[
            SceneConfig(name="remote_play", description="Steam游戏串流", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),
}

CALL_APPS = {
    "douyin_call": AppConfig(
        name="douyin_call",
        app_type="social",
        package="com.ss.android.ugc.aweme",
        scenes=[
            SceneConfig(name="video_call", description="抖音视频通话", duration=300),
            SceneConfig(name="voice_call", description="抖音语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "voillo_ultra": AppConfig(
        name="voillo_ultra",
        app_type="social",
        package="com.voillo.ultra",
        scenes=[
            SceneConfig(name="video_call", description="视频通话", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "mobile_yy": AppConfig(
        name="mobile_yy",
        app_type="social",
        package="com.yy.yyvoicetool",
        scenes=[
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "qq_voice": AppConfig(
        name="qq_voice",
        app_type="social",
        package="com.tencent.mobileqq",
        scenes=[
            SceneConfig(name="voice_call", description="QQ语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "skype": AppConfig(
        name="skype",
        app_type="social",
        package="com.skype.raider",
        scenes=[
            SceneConfig(name="video_call", description="视频通话", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "agora_video_call": AppConfig(
        name="agora_video_call",
        app_type="social",
        package="io.agora.vcall",
        scenes=[
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "whatsapp": AppConfig(
        name="whatsapp",
        app_type="social",
        package="com.whatsapp",
        scenes=[
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "zoom": AppConfig(
        name="zoom",
        app_type="social",
        package="us.zoom.videomeetings",
        scenes=[
            SceneConfig(name="video_meeting", description="视频会议", duration=300),
            SceneConfig(name="voice_meeting", description="语音会议", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "teams_call": AppConfig(
        name="teams_call",
        app_type="social",
        package="com.microsoft.teams",
        scenes=[
            SceneConfig(name="video_call", description="视频通话", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),
}

CLOUDPHONE_APPS = {
    "cmcc_cloudphone": AppConfig(
        name="cmcc_cloudphone",
        app_type="cloudphone",
        package="com.chinamobile.cloudphone",
        scenes=[
            SceneConfig(name="cloud_phone", description="云手机使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "cmcc_cloudpc": AppConfig(
        name="cmcc_cloudpc",
        app_type="cloudphone",
        package="com.chinamobile.cloudpc",
        scenes=[
            SceneConfig(name="cloud_pc", description="云电脑使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "redfinger": AppConfig(
        name="redfinger",
        app_type="cloudphone",
        package="com.redfinger.cloudphone",
        scenes=[
            SceneConfig(name="cloud_phone", description="云手机使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "ld_cloudphone": AppConfig(
        name="ld_cloudphone",
        app_type="cloudphone",
        package="com.ld.cloudphone",
        scenes=[
            SceneConfig(name="cloud_phone", description="雷电云手机使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "duoduo_cloudphone": AppConfig(
        name="duoduo_cloudphone",
        app_type="cloudphone",
        package="com.duoduo.cloudphone",
        scenes=[
            SceneConfig(name="cloud_phone", description="多多云手机使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "fengwo_cloudphone": AppConfig(
        name="fengwo_cloudphone",
        app_type="cloudphone",
        package="com.fengwo.cloudphone",
        scenes=[
            SceneConfig(name="cloud_phone", description="蜂窝云手机使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "haima_cloudpc": AppConfig(
        name="haima_cloudpc",
        app_type="cloudphone",
        package="com.haima.cloudpc",
        scenes=[
            SceneConfig(name="cloud_pc", description="海马云电脑使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "vmos": AppConfig(
        name="vmos",
        app_type="cloudphone",
        package="com.vmos.pro",
        scenes=[
            SceneConfig(name="virtual_phone", description="虚拟手机使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "tianyi_cloudpc": AppConfig(
        name="tianyi_cloudpc",
        app_type="cloudphone",
        package="com.ctyun.cloudpc",
        scenes=[
            SceneConfig(name="cloud_pc", description="天翼云电脑使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),

    "netease_cloudpc": AppConfig(
        name="netease_cloudpc",
        app_type="cloudphone",
        package="com.netease.cloudpc",
        scenes=[
            SceneConfig(name="cloud_pc", description="网易云电脑使用", duration=600),
        ],
        qoe_metrics=["trust_stall", "rtt", "trust_resolution"]
    ),
}

NEWS_APPS = {
    "tencent_news": AppConfig(
        name="tencent_news",
        app_type="news",
        package="com.tencent.news",
        scenes=[
            SceneConfig(name="feed", description="资讯流浏览", duration=300),
            SceneConfig(name="news_read", description="新闻阅读", duration=300),
        ],
        qoe_metrics=[]
    ),

    "xiaohongshu_news": AppConfig(
        name="xiaohongshu_news",
        app_type="news",
        package="com.xingin.xhs",
        scenes=[
            SceneConfig(name="feed", description="小红书资讯流", duration=300),
        ],
        qoe_metrics=[]
    ),

    "bytedance": AppConfig(
        name="bytedance",
        app_type="news",
        package="com.ss.android.article.news",
        scenes=[
            SceneConfig(name="feed", description="资讯流浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "web_browser": AppConfig(
        name="web_browser",
        app_type="news",
        package="com.android.chrome",
        scenes=[
            SceneConfig(name="browse", description="网页浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "taobao_browse": AppConfig(
        name="taobao_browse",
        app_type="shopping",
        package="com.taobao.taobao",
        scenes=[
            SceneConfig(name="browse", description="商品浏览", duration=300),
            SceneConfig(name="search", description="搜索商品", duration=180),
        ],
        qoe_metrics=[]
    ),

    "baidu_search": AppConfig(
        name="baidu_search",
        app_type="news",
        package="com.baidu.searchbox",
        scenes=[
            SceneConfig(name="search", description="搜索浏览", duration=300),
            SceneConfig(name="feed", description="信息流浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "pinduoduo": AppConfig(
        name="pinduoduo",
        app_type="shopping",
        package="com.xunmeng.pinduoduo",
        scenes=[
            SceneConfig(name="browse", description="商品浏览", duration=300),
            SceneConfig(name="search", description="搜索商品", duration=180),
        ],
        qoe_metrics=[]
    ),

    "meituan": AppConfig(
        name="meituan",
        app_type="shopping",
        package="com.sankuai.meituan",
        scenes=[
            SceneConfig(name="browse", description="商家/商品浏览", duration=300),
            SceneConfig(name="search", description="搜索", duration=180),
        ],
        qoe_metrics=[]
    ),
}

AIGC_APPS = {
    "deepseek": AppConfig(
        name="deepseek",
        app_type="aigc",
        package="com.deepseek.chat",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "doubao_ai": AppConfig(
        name="doubao_ai",
        app_type="aigc",
        package="com.larus.nova",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "tongyi": AppConfig(
        name="tongyi",
        app_type="aigc",
        package="com.aliyun.tongyi",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "yuanbao": AppConfig(
        name="yuanbao",
        app_type="aigc",
        package="com.tencent.hunyuan.app.chat",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "kimi": AppConfig(
        name="kimi",
        app_type="aigc",
        package="com.moonshot.kimi",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "wenxiaoyan": AppConfig(
        name="wenxiaoyan",
        app_type="aigc",
        package="com.baidu.newapp",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "xinghuo": AppConfig(
        name="xinghuo",
        app_type="aigc",
        package="com.iflytek.spark",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),

    "ai_writing": AppConfig(
        name="ai_writing",
        app_type="aigc",
        package="com.nhpl.aiwnwrite",
        scenes=[
            SceneConfig(name="writing", description="AI写作", duration=300),
        ],
        qoe_metrics=[]
    ),

    "jimeng_ai": AppConfig(
        name="jimeng_ai",
        app_type="aigc",
        package="com.bytedance.dreamina",
        scenes=[
            SceneConfig(name="generate", description="AI生成", duration=300),
        ],
        qoe_metrics=[]
    ),

    "maoxiang": AppConfig(
        name="maoxiang",
        app_type="aigc",
        package="com.parallel.odyssey",
        scenes=[
            SceneConfig(name="chat", description="AI对话", duration=300),
        ],
        qoe_metrics=[]
    ),
}

FILETRANSFER_APPS = {
    "vivo_appstore": AppConfig(
        name="vivo_appstore",
        app_type="filetransfer",
        package="com.bbk.appstore",
        scenes=[
            SceneConfig(name="browse", description="应用商店浏览", duration=300),
            SceneConfig(name="download", description="应用下载", duration=300),
        ],
        qoe_metrics=[]
    ),

    "huawei_appmarket": AppConfig(
        name="huawei_appmarket",
        app_type="filetransfer",
        package="com.huawei.appmarket",
        scenes=[
            SceneConfig(name="browse", description="应用市场浏览", duration=300),
            SceneConfig(name="download", description="应用下载", duration=300),
        ],
        qoe_metrics=[]
    ),

    "aliyun": AppConfig(
        name="aliyun",
        app_type="filetransfer",
        package="com.aliyun.app",
        scenes=[
            SceneConfig(name="console", description="阿里云控制台", duration=300),
        ],
        qoe_metrics=[]
    ),

    "appstore": AppConfig(
        name="appstore",
        app_type="filetransfer",
        package="com.apple.appstore",
        scenes=[
            SceneConfig(name="browse", description="应用商店浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "huawei_cloud_app": AppConfig(
        name="huawei_cloud_app",
        app_type="filetransfer",
        package="com.huawei.cloud",
        scenes=[
            SceneConfig(name="console", description="华为云控制台", duration=300),
        ],
        qoe_metrics=[]
    ),

    "hecaiyun": AppConfig(
        name="hecaiyun",
        app_type="filetransfer",
        package="com.chinamobile.cmccdrive",
        scenes=[
            SceneConfig(name="upload", description="文件上传", duration=300),
            SceneConfig(name="download", description="文件下载", duration=300),
            SceneConfig(name="browse", description="云盘浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "oppo_appstore": AppConfig(
        name="oppo_appstore",
        app_type="filetransfer",
        package="com.oppo.market",
        scenes=[
            SceneConfig(name="browse", description="应用商店浏览", duration=300),
            SceneConfig(name="download", description="应用下载", duration=300),
        ],
        qoe_metrics=[]
    ),

    "icloud": AppConfig(
        name="icloud",
        app_type="filetransfer",
        package="com.apple.icloud",
        scenes=[
            SceneConfig(name="browse", description="iCloud浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "baidu_netdisk": AppConfig(
        name="baidu_netdisk",
        app_type="filetransfer",
        package="com.baidu.netdisk",
        scenes=[
            SceneConfig(name="upload", description="文件上传", duration=300),
            SceneConfig(name="download", description="文件下载", duration=300),
            SceneConfig(name="browse", description="网盘浏览", duration=300),
        ],
        qoe_metrics=[]
    ),

    "xiaomi_appstore": AppConfig(
        name="xiaomi_appstore",
        app_type="filetransfer",
        package="com.xiaomi.market",
        scenes=[
            SceneConfig(name="browse", description="应用商店浏览", duration=300),
            SceneConfig(name="download", description="应用下载", duration=300),
        ],
        qoe_metrics=[]
    ),
}

IM_APPS = {
    "wechat": AppConfig(
        name="wechat",
        app_type="social",
        package="com.tencent.mm",
        scenes=[
            SceneConfig(
                name="text_chat",
                description="文字聊天",
                duration=300,
                params={"feature": "chat", "member_num": 1}
            ),
            SceneConfig(
                name="voice_call",
                description="语音通话",
                duration=300,
                params={"feature": "voice_call", "member_num": 1, "dev1_mic": True}
            ),
            SceneConfig(
                name="video_call",
                description="视频通话",
                duration=300,
                params={
                    "feature": "video_call",
                    "member_num": 1,
                    "dev1_camera": True,
                    "dev1_mic": True
                }
            ),
            SceneConfig(
                name="group_video_call",
                description="群视频通话",
                duration=300,
                params={
                    "feature": "video_call",
                    "member_num": 4,
                    "dev1_camera": True,
                    "dev1_mic": True
                }
            ),
            SceneConfig(
                name="moments",
                description="朋友圈浏览",
                duration=300,
                params={"feature": "moments"}
            ),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "qq": AppConfig(
        name="qq",
        app_type="social",
        package="com.tencent.mobileqq",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "momo": AppConfig(
        name="momo",
        app_type="social",
        package="com.immomo.momo",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "aliwangwang": AppConfig(
        name="aliwangwang",
        app_type="social",
        package="com.alibaba.mobileim",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "wecom": AppConfig(
        name="wecom",
        app_type="social",
        package="com.tencent.wework",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "facebook_messenger": AppConfig(
        name="facebook_messenger",
        app_type="social",
        package="com.facebook.orca",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "kakaotalk": AppConfig(
        name="kakaotalk",
        app_type="social",
        package="com.kakao.talk",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
            SceneConfig(name="video_call", description="视频通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "lianxin": AppConfig(
        name="lianxin",
        app_type="social",
        package="com.zenmen.palmchat",
        scenes=[
            SceneConfig(name="text_chat", description="文字聊天", duration=300),
            SceneConfig(name="voice_call", description="语音通话", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),

    "rcs_message": AppConfig(
        name="rcs_message",
        app_type="social",
        package="com.google.android.apps.messaging",
        scenes=[
            SceneConfig(name="text_chat", description="5G消息文字聊天", duration=300),
        ],
        qoe_metrics=["trust_stall", "rtt"]
    ),
}

SHOPPING_APPS = {
    "taobao": AppConfig(
        name="taobao",
        app_type="shopping",
        package="com.taobao.taobao",
        scenes=[
            SceneConfig(name="browse", description="商品浏览", duration=300),
            SceneConfig(name="search", description="搜索商品", duration=180),
            SceneConfig(name="live", description="直播购物", duration=600),
        ],
        qoe_metrics=["trust_stall"]
    ),

    "jd": AppConfig(
        name="jd",
        app_type="shopping",
        package="com.jingdong.app.mall",
        scenes=[
            SceneConfig(name="browse", description="商品浏览", duration=300),
            SceneConfig(name="search", description="搜索商品", duration=180),
        ],
        qoe_metrics=["trust_stall"]
    ),
}

AUDIO_APPS = {
    "lanren_tingshu": AppConfig(
        name="lanren_tingshu",
        app_type="audio",
        package="bubei.tingshu",
        scenes=[
            SceneConfig(
                name="browse",
                description="首页/推荐内容浏览",
                duration=300,
                params={"feature": "home_feed", "content_type": "audio_book_cards"},
            ),
            SceneConfig(
                name="audio_playback",
                description="有声内容播放/试听",
                duration=300,
                params={"feature": "audio_playback"},
            ),
            SceneConfig(
                name="search",
                description="搜索和浏览有声内容",
                duration=180,
                params={"feature": "search"},
            ),
        ],
        qoe_metrics=["trust_stall", "rtt"],
    ),
}

ALL_APPS = {
    **SHORT_VIDEO_APPS,
    **LIVE_APPS,
    **VOD_APPS,
    **MEETING_APPS,
    **GAME_APPS,
    **CLOUDGAME_APPS,
    **CALL_APPS,
    **CLOUDPHONE_APPS,
    **NEWS_APPS,
    **AIGC_APPS,
    **FILETRANSFER_APPS,
    **IM_APPS,
    **SHOPPING_APPS,
    **AUDIO_APPS,
}


# Friendly names used by the Executor. This is deliberately kept in the
# capturer catalog module so package ids have a single source of truth.
EXECUTOR_APP_PACKAGES: dict[str, str] = {
    # Android system
    "设置": "com.android.settings",
    "系统设置": "com.android.settings",
    "设置应用": "com.android.settings",
    "AndroidSystemSettings": "com.android.settings",
    "Android System Settings": "com.android.settings",
    "Settings": "com.android.settings",
    "浏览器": "com.android.browser",
    "Chrome": "com.android.chrome",
    "Google Chrome": "com.android.chrome",
    "相机": "com.android.camera",
    "短信": "com.android.mms",
    "电话": "com.android.dialer",
    "联系人": "com.android.contacts",
    "时钟": "com.android.deskclock",
    "录音机": "com.android.soundrecorder",
    "文件管理": "com.android.fileexplorer",
    "应用商店": "com.android.vending",
    "Google Play": "com.android.vending",
    # Social & messaging
    "微信": "com.tencent.mm",
    "WeChat": "com.tencent.mm",
    "QQ": "com.tencent.mobileqq",
    "企业微信": "com.tencent.wework",
    "微博": "com.sina.weibo",
    "知乎": "com.zhihu.android",
    "豆瓣": "com.douban.frodo",
    "陌陌": "com.immomo.momo",
    "连信": "com.zenmen.palmchat",
    "阿里旺旺": "com.alibaba.mobileim",
    "Facebook": "com.facebook.katana",
    "Telegram": "org.telegram.messenger",
    "WhatsApp": "com.whatsapp",
    "Twitter": "com.twitter.android",
    "X": "com.twitter.android",
    "Reddit": "com.reddit.frontpage",
    # E-commerce
    "淘宝": "com.taobao.taobao",
    "淘宝闪购": "com.taobao.taobao",
    "淘宝直播": "com.taobao.live",
    "京东": "com.jingdong.app.mall",
    "京东秒送": "com.jingdong.app.mall",
    "拼多多": "com.xunmeng.pinduoduo",
    "得物": "com.shizhuang.duapp",
    "唯品会": "com.achievo.vipshop",
    "闲鱼": "com.taobao.idlefish",
    "Temu": "com.einnovation.temu",
    "temu": "com.einnovation.temu",
    # Payment & finance
    "支付宝": "com.eg.android.AlipayGphone",
    "云闪付": "com.unionpay",
    "招商银行": "cmb.pb",
    "同花顺": "com.hexin.plat.android",
    # Food & local services
    "美团": "com.sankuai.meituan",
    "大众点评": "com.dianping.v1",
    "饿了么": "me.ele",
    "肯德基": "com.yek.android.kfc.activitys",
    "麦当劳": "com.mcdonalds.app",
    # Maps & travel
    "高德地图": "com.autonavi.minimap",
    "百度地图": "com.baidu.BaiduMap",
    "Google Maps": "com.google.android.apps.maps",
    "携程": "ctrip.android.view",
    "去哪儿": "com.Qunar",
    "去哪儿旅行": "com.Qunar",
    "铁路12306": "com.MobileTicket",
    "12306": "com.MobileTicket",
    "滴滴": "com.sdu.didi.psnger",
    "滴滴出行": "com.sdu.didi.psnger",
    "Booking": "com.booking",
    "Booking.com": "com.booking",
    "Expedia": "com.expedia.bookings",
    # Video & entertainment
    "抖音": "com.ss.android.ugc.aweme",
    "鎶栭煶": "com.ss.android.ugc.aweme",
    "快手": "com.smile.gifmaker",
    "快手极速版": "com.kuaishou.nebula",
    "小红书": "com.xingin.xhs",
    "哔哩哔哩": "tv.danmaku.bili",
    "哔哩哔哩直播": "tv.danmaku.bili",
    "bilibili": "tv.danmaku.bili",
    "B站": "tv.danmaku.bili",
    "腾讯视频": "com.tencent.qqlive",
    "腾讯网": "com.tencent.news",
    "爱奇艺": "com.qiyi.video",
    "优酷": "com.youku.phone",
    "优酷视频": "com.youku.phone",
    "芒果TV": "com.hunantv.imgo.activity",
    "乐视视频": "com.letv.android.client",
    "虎牙直播": "com.duowan.kiwi",
    "斗鱼直播": "air.tv.douyu.android",
    "咪咕视频": "com.cmcc.cmvideo",
    "咪咕直播": "com.cmcc.cmvideo",
    "咪咕爱看": "com.wondertek.miguaikan",
    "火山小视频": "com.ss.android.ugc.live",
    "西瓜视频": "com.ss.android.article.video",
    "微视": "com.tencent.weishi",
    "好看视频": "com.baidu.haokan",
    "网易直播": "com.netease.cc",
    "CC直播": "com.netease.cc",
    "酷狗直播": "com.kugou.fanxing.allinone",
    "YY": "com.duowan.mobile",
    "手机YY": "com.duowan.mobile",
    "百度影音": "com.baidu.video",
    "皮皮虾": "com.sup.android.superb",
    "NOW 直播": "com.tencent.now",
    "NOW直播": "com.tencent.now",
    "今日头条": "com.ss.android.article.news",
    "腾讯新闻": "com.tencent.news",
    # Music & audio
    "网易云音乐": "com.netease.cloudmusic",
    "QQ音乐": "com.tencent.qqmusic",
    "汽水音乐": "com.luna.music",
    "喜马拉雅": "com.ximalaya.ting.android",
    "懒人听书": "bubei.tingshu",
    "懒人听书大字版": "bubei.tingshu",
    # Reading & productivity
    "番茄小说": "com.dragon.read",
    "番茄免费小说": "com.dragon.read",
    "七猫免费小说": "com.kmxs.reader",
    "飞书": "com.ss.android.lark",
    "钉钉": "com.alibaba.android.rimet",
    "腾讯会议": "com.tencent.wemeet.app",
    "好视通云会议": "com.bizconf.video",
    "云视讯": "com.cmicc.cloudmeeting",
    "全时云": "com.gnet.calendars",
    "31会议网": "com.eventown.mobile",
    "Zoom": "us.zoom.videomeetings",
    "ZoomS": "us.zoom.videomeetings",
    "Z0OM": "us.zoom.videomeetings",
    "Teams": "com.microsoft.teams",
    "TEAMS": "com.microsoft.teams",
    "QQ邮箱": "com.tencent.androidqqmail",
    "Gmail": "com.google.android.gm",
    "Google Drive": "com.google.android.apps.docs",
    "Google Docs": "com.google.android.apps.docs.editors.docs",
    "Google Calendar": "com.google.android.calendar",
    "Google Keep": "com.google.android.keep",
    # AI & tools
    "豆包": "com.larus.nova",
    "Kimi": "com.moonshot.kimichat",
    "通义": "com.alibaba.tongyi",
    "通义千问": "com.alibaba.tongyi",
    "DeepSeek": "com.deepseek.chat",
    "Deepseek": "com.deepseek.chat",
    "deepseek": "com.deepseek.chat",
    "文小言": "com.baidu.newapp",
    "讯飞星火": "com.iflytek.spark",
    "元宝": "com.tencent.hunyuan.app.chat",
    "腾讯元宝": "com.tencent.hunyuan.app.chat",
    "即梦AI": "com.faceu.jimeng",
    # Health, housing, games
    "keep": "com.gotokeep.keep",
    "美柚": "com.lingan.seeyou",
    "贝壳找房": "com.lianjia.beike",
    "安居客": "com.anjuke.android.app",
    "王者荣耀": "com.tencent.tmgp.sgame",
    "和平精英": "com.tencent.tmgp.pubgmhd",
    "第五人格": "com.netease.dwrg",
    "金铲铲之战": "com.tencent.jkchess",
    "LOLGame": "com.tencent.lolm",
    "英雄联盟手游": "com.tencent.lolm",
    "原神": "com.miHoYo.Yuanshen",
    "星穹铁道": "com.miHoYo.hkrpg",
    "崩坏星穹铁道": "com.miHoYo.hkrpg",
    "恋与深空": "com.papegames.lysk.cn",
    "QQ游戏": "com.tencent.qqgame",
    "Steam": "com.valvesoftware.android.steam.community",
    "Steam游戏": "com.valvesoftware.android.steam.community",
    "Steam在线游戏平台": "com.valvesoftware.android.steam.community",
    # Cloud drive, cloud phone, app stores, vendor tools
    "阿里云盘": "com.alicloud.databox",
    "百度网盘": "com.baidu.netdisk",
    "百度网": "com.baidu.netdisk",
    "和彩云": "com.chinamobile.mcloud",
    "VIVO应用商店": "com.bbk.appstore",
    "vivo应用商店": "com.bbk.appstore",
    "华为应用市场": "com.huawei.appmarket",
    "小米应用商店": "com.xiaomi.market",
    "OPPO应用": "com.heytap.market",
    "OPPO应用商店": "com.heytap.market",
    "华为云": "com.huawei.hicloud",
    "商店": "com.android.vending",
}

CAPTURE_APP_DISPLAY_NAMES: dict[str, str] = {
    "douyin": "抖音",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "tomato_novel": "番茄小说",
    "volcano_video": "火山小视频",
    "xigua_video": "西瓜视频",
    "weishi": "微视",
    "haokan_video": "好看视频",
    "pipixia": "皮皮虾",
    "jinri_toutiao": "今日头条",
    "tencent_news": "腾讯新闻",
    "huya_live": "虎牙直播",
    "migulive": "咪咕直播",
    "douyu_live": "斗鱼直播",
    "taobao_live": "淘宝直播",
    "yy_live": "YY",
    "cc_live": "CC直播",
    "bilibili_live": "哔哩哔哩直播",
    "tencent_video": "腾讯视频",
    "migu_aikan": "咪咕爱看",
    "bilibili": "哔哩哔哩",
    "iqiyi": "爱奇艺",
    "youku": "优酷",
    "mango": "芒果TV",
    "tencent_meeting": "腾讯会议",
    "dingtalk": "钉钉",
    "lark": "飞书",
    "huawei_cloud": "华为云",
    "quanshi_cloud": "全时云",
    "teams": "Teams",
    "honorofkings": "王者荣耀",
    "pubgm": "和平精英",
    "genshin": "原神",
    "identity_v": "第五人格",
    "zoom": "Zoom",
    "skype": "Skype",
    "whatsapp": "WhatsApp",
    "qq_voice": "QQ语音",
    "deepseek": "DeepSeek",
    "doubao_ai": "豆包",
    "tongyi": "通义",
    "yuanbao": "腾讯元宝",
    "kimi": "Kimi",
    "wenxiaoyan": "文小言",
    "xinghuo": "讯飞星火",
    "wechat": "微信",
    "qq": "QQ",
    "wecom": "企业微信",
    "momo": "陌陌",
    "taobao": "淘宝",
    "jd": "京东",
    "pinduoduo": "拼多多",
    "meituan": "美团",
    "baidu_search": "百度",
    "web_browser": "网页浏览",
    "lanren_tingshu": "懒人听书",
    "ximalaya": "喜马拉雅",
}

# Runtime-only registrations are used for custom apps supplied on the CLI.
_RUNTIME_APP_PACKAGES: Dict[str, str] = {}


def _recover_mojibake(text: str) -> Optional[str]:
    """Recover common UTF-8 text decoded as GBK by model providers."""
    try:
        recovered = text.encode("gbk").decode("utf-8")
    except UnicodeError:
        return None
    return recovered if recovered != text else None


def iter_app_package_aliases() -> Dict[str, str]:
    """Return every Executor/capturer name mapped to one package id."""
    mappings = dict(EXECUTOR_APP_PACKAGES)
    for app_key, config in ALL_APPS.items():
        mappings[app_key] = config.package
        mappings.setdefault(config.name, config.package)
    for app_key, display_name in CAPTURE_APP_DISPLAY_NAMES.items():
        config = ALL_APPS.get(app_key)
        if config:
            # Capturer entries are authoritative when an old Executor package
            # mapping disagrees with the catalog.
            mappings[display_name] = config.package
    mappings.update(_RUNTIME_APP_PACKAGES)
    return mappings


def resolve_app_package(value: str) -> Optional[str]:
    """Resolve a natural-language name, catalog key, or package id."""
    text = (value or "").strip()
    if not text:
        return None
    if "." in text and not any(char.isspace() for char in text):
        return text

    mappings = iter_app_package_aliases()
    package = mappings.get(text)
    if package:
        return package
    lowered = text.casefold()
    for alias, candidate in mappings.items():
        if alias.casefold() == lowered:
            return candidate

    recovered = _recover_mojibake(text)
    if recovered and recovered != text:
        return resolve_app_package(recovered)
    return None


def resolve_app_key(value: str) -> Optional[str]:
    """Resolve a name/package to its Unicapture catalog key when supported."""
    text = (value or "").strip()
    if not text:
        return None
    if text in ALL_APPS:
        return text
    lowered = text.casefold()
    for app_key in ALL_APPS:
        if app_key.casefold() == lowered:
            return app_key
    for app_key, display_name in CAPTURE_APP_DISPLAY_NAMES.items():
        if display_name.casefold() == lowered and app_key in ALL_APPS:
            return app_key

    package = resolve_app_package(text)
    if not package:
        return None
    for app_key, config in ALL_APPS.items():
        if config.package == package:
            return app_key
    return None


def infer_app_key_from_text(text: str) -> Optional[str]:
    """Find the longest known natural-language alias contained in a goal."""
    clean_text = (text or "").strip()
    if not clean_text:
        return None
    matches: List[tuple[int, str]] = []
    lowered = clean_text.casefold()
    for alias in iter_app_package_aliases():
        if alias and alias.casefold() in lowered:
            app_key = resolve_app_key(alias)
            if app_key:
                matches.append((len(alias), app_key))
    return max(matches, default=(0, ""))[1] or None


def register_runtime_app(name: str, package: str) -> None:
    """Register a non-persistent custom app alias for the current process."""
    clean_name = (name or "").strip()
    clean_package = (package or "").strip()
    if clean_name and clean_package:
        _RUNTIME_APP_PACKAGES[clean_name] = clean_package


def get_app_config(app_name: str) -> Optional[AppConfig]:
    """获取APP配置，支持内部键、自然语言别名和包名。"""
    app_key = resolve_app_key(app_name)
    return ALL_APPS.get(app_key) if app_key else None


def create_fallback_config(
    app_name: str,
    package: str,
    custom_type: Optional[str] = None,
    custom_scene: Optional[str] = None,
) -> AppConfig:
    """Create a runtime configuration for an app outside the built-in catalog."""
    user_type = (custom_type or "live").strip().lower()
    type_map = {
        "live": ("video", "live", ["trust_stall", "trust_resolution"]),
        "movie": ("video", "movie_play", ["trust_stall", "trust_resolution"]),
        "short_video": ("video", "feed", ["trust_stall", "trust_resolution"]),
        "video": ("video", "feed", ["trust_stall", "trust_resolution"]),
        "game": ("game", "gameplay", ["trust_stall"]),
        "social": ("social", "feed", ["trust_stall"]),
        "shopping": ("shopping", "browse", ["trust_stall"]),
        "audio": ("audio", "playback", ["trust_stall", "rtt"]),
        "news": ("news", "feed", ["trust_stall"]),
    }
    app_type, default_scene, qoe_metrics = type_map.get(
        user_type,
        ("video", "live", ["trust_stall", "trust_resolution"]),
    )
    scene_name = (custom_scene or default_scene).strip() or default_scene
    duration = 600 if scene_name in {"live", "movie_play", "gameplay"} else 300
    return AppConfig(
        name=app_name,
        app_type=app_type,
        package=package,
        scenes=[
            SceneConfig(
                name=scene_name,
                description=f"自定义场景 ({user_type})",
                duration=duration,
            )
        ],
        qoe_metrics=qoe_metrics,
    )


def get_apps_by_type(app_type: str) -> Dict[str, AppConfig]:
    """按类型获取APP列表"""
    return {k: v for k, v in ALL_APPS.items() if v.app_type == app_type}


def list_all_apps() -> List[str]:
    """列出所有支持的APP"""
    return list(ALL_APPS.keys())


def is_qoe_applicable(app_config: AppConfig, scene_name: str) -> bool:
    """
    判断指定APP和场景是否需要启用QoE标注。

    以下场景启用：
      - 视频类（video）全部场景
      - 游戏类（game）全部场景
      - 云游戏类（cloudgame）全部场景
      - 云手机/云电脑类（cloudphone）全部场景
      - 直播类场景（scene 名包含 live，如 taobao live、wechat live 等）
      - 社交类/音频类仅音视频通话场景

    其他类型/场景不启用，最终QoE文件仅保留时间戳，识别内容为空。
    """
    app_type = app_config.app_type

    # 视频、游戏、云游戏、云手机类：全部场景启用
    if app_type in {'video', 'game', 'cloudgame', 'cloudphone'}:
        return True

    # 直播类场景：不论 APP 类型都启用（如购物 APP 的直播、社交 APP 的直播）
    if 'live' in scene_name.lower():
        return True

    # 社交类/音频类：仅音视频通话场景启用
    if app_type in {'social', 'audio'}:
        return scene_name in {'voice_call', 'video_call', 'group_video_call'}

    return False


def print_app_info(app_name: str):
    """打印APP详细信息"""
    app = get_app_config(app_name)
    if not app:
        print(f"未找到应用: {app_name}")
        return
    
    print(f"\n{'='*50}")
    print(f"应用名称: {app.name}")
    print(f"应用类型: {app.app_type}")
    print(f"包名: {app.package}")
    print(f"版本: {app.version or '未知'}")
    print(f"\n支持场景:")
    for scene in app.scenes:
        print(f"  - {scene.name}: {scene.description} ({scene.duration}秒)")
    print(f"\nQoE指标: {', '.join(app.qoe_metrics)}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    # 测试：列出所有APP
    print("支持的APP列表:")
    print("\n[短视频]")
    for name in SHORT_VIDEO_APPS:
        print(f"  - {name}")
    print("\n[直播]")
    for name in LIVE_APPS:
        print(f"  - {name}")
    print("\n[视频点播]")
    for name in VOD_APPS:
        print(f"  - {name}")
    print("\n[会议]")
    for name in MEETING_APPS:
        print(f"  - {name}")
    print("\n[游戏]")
    for name in GAME_APPS:
        print(f"  - {name}")
    print("\n[云游戏]")
    for name in CLOUDGAME_APPS:
        print(f"  - {name}")
    print("\n[音视频通话]")
    for name in CALL_APPS:
        print(f"  - {name}")
    print("\n[云手机/云电脑]")
    for name in CLOUDPHONE_APPS:
        print(f"  - {name}")
    print("\n[资讯浏览]")
    for name in NEWS_APPS:
        print(f"  - {name}")
    print("\n[AIGC]")
    for name in AIGC_APPS:
        print(f"  - {name}")
    print("\n[文件传输/云存储]")
    for name in FILETRANSFER_APPS:
        print(f"  - {name}")
    print("\n[即时通信]")
    for name in IM_APPS:
        print(f"  - {name}")
    print("\n[购物]")
    for name in SHOPPING_APPS:
        print(f"  - {name}")
    print("\n[音频]")
    for name in AUDIO_APPS:
        print(f"  - {name}")
