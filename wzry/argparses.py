# config.py
import argparse

import torch

from globalInfo import GlobalInfo

# 移动坐标和滑动半径
# Coordinates are screen ratios in landscape mode. The combat controls were
# aligned with autowzry's 960x540 templates: record_pos x = 0.5 + rx,
# record_pos y = 0.5 + ry * 960 / 540.
move_actions_detail = {
    1: {'action_name': '移动', 'position': (0.173, 0.784), 'radius': 200}
}

# 点击坐标
#  购买装备1， 购买装备2，发起进攻，开始撤退，请求集合，升级1技能，升级2技能，升级3技能，升级4技能
info_actions_detail = {
    1: {'action_name': '购买装备1', 'position': (0.731, 0.145), 'radius': 0},
    2: {'action_name': '购买装备2', 'position': (0.731, 0.253), 'radius': 0},
    3: {'action_name': '发起进攻', 'position': (0.926, 0.14), 'radius': 0},
    4: {'action_name': '开始撤退', 'position': (0.926, 0.22), 'radius': 0},
    5: {'action_name': '请求集合', 'position': (0.926, 0.31), 'radius': 0},
    6: {'action_name': '升级1技能', 'position': (0.668, 0.772), 'radius': 0},
    7: {'action_name': '升级2技能', 'position': (0.717, 0.59), 'radius': 0},
    8: {'action_name': '升级3技能', 'position':  (0.788, 0.467), 'radius': 0}
}

# 无操作, 攻击，攻击小兵，攻击塔，回城，恢复，装备技能, 1技能，2技能，3技能,
attack_actions_detail = {
    1: {'action_name': '攻击', 'position': (0.862, 0.856), 'radius': 0},
    2: {'action_name': '攻击小兵', 'position':  (0.667, 0.904), 'radius': 0},
    3: {'action_name': '攻击塔', 'position':  (0.732, 0.706), 'radius':0},
    4: {'action_name': '回城', 'position':  (0.507, 0.896), 'radius': 0},
    5: {'action_name': '恢复', 'position': (0.534, 0.898), 'radius': 0},
    6: {'action_name': '装备技能', 'position':  (0.666, 0.402), 'radius': 50},
    7: {'action_name': '召唤师技能', 'position': (0.600, 0.891), 'radius': 50},
    8: {'action_name': '1技能', 'position':  (0.698, 0.875), 'radius': 10},
    9: {'action_name': '2技能', 'position': (0.763, 0.697), 'radius': 10},
    10: {'action_name': '3技能', 'position': (0.868, 0.584), 'radius': 10}
}


def str2bool(value):
    if isinstance(value, bool):
        return value
    value = value.lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--iphone_id', type=str, default='10AD880X7Q001RX', help="iphone_id")
    parser.add_argument('--real_iphone', type=bool, default=True, help="real_iphone")
    parser.add_argument('--window_title', type=str, default='wzry_ai', help="window_title")
    parser.add_argument('--device_id', type=str, default='cuda:0', help="device_id")
    parser.add_argument('--memory_size', type=int, default=10000, help="Replay memory size")
    parser.add_argument('--batch_size', type=int, default=64, help="Batch size for training")
    parser.add_argument('--learning_rate', type=float, default=0.001, help="Learning rate")
    parser.add_argument('--gamma', type=float, default=0.99, help="Discount factor")
    parser.add_argument('--epsilon', type=float, default=1.0, help="Initial exploration rate")
    parser.add_argument('--epsilon_decay', type=float, default=0.995, help="Exploration rate decay")
    parser.add_argument('--epsilon_min', type=float, default=0.01, help="Minimum exploration rate")
    parser.add_argument('--model_path', type=str, default="src/wzry_ai.pt", help="Path to the model to load")
    parser.add_argument('--num_episodes', type=int, default=10, help="Number of episodes to collect data")
    parser.add_argument('--target_update', type=int, default=10, help="Number of episodes to collect data")
    parser.add_argument('--observation_delay_sec', type=float, default=0.08,
                        help="Delay after each action bundle before taking the next observation")
    parser.add_argument('--info_action_cooldown_steps', type=int, default=6,
                        help="Minimum number of steps between low-frequency info actions")
    parser.add_argument('--move_swipe_duration_ms', type=int, default=120,
                        help="Duration of movement swipes")
    parser.add_argument('--continuous_move_enabled', type=str2bool, default=True,
                        help="Use one long joystick drag for each burst instead of repeated short swipes")
    parser.add_argument('--move_control_mode', type=str, default='persistent_motionevent',
                        choices=[
                            'persistent_motionevent',
                            'persistent_minitouch',
                            'persistent_swipe',
                            'center_swipe',
                            'motionevent',
                            'swipe',
                            'repeat_swipe',
                        ],
                        help="Movement execution backend: persistent_motionevent keeps one joystick contact alive across steps")
    parser.add_argument('--continuous_move_extra_ms', type=int, default=120,
                        help="Extra hold time added to movement swipes inside one action burst")
    parser.add_argument('--move_joystick_radius', type=int, default=260,
                        help="Joystick drag radius used for movement execution; overrides action table radius")
    parser.add_argument('--move_joystick_center_enabled', type=str2bool, default=True,
                        help="Override the joystick center with calibrated screen ratios")
    parser.add_argument('--move_joystick_center_x', type=float, default=0.173,
                        help="Screen-relative joystick center x; aligned with autowzry combat templates")
    parser.add_argument('--move_joystick_center_y', type=float, default=0.784,
                        help="Screen-relative joystick center y; aligned with autowzry combat templates")
    parser.add_argument('--move_motionevent_settle_ms', type=int, default=25,
                        help="Delay between joystick DOWN and MOVE when using motionevent movement")
    parser.add_argument('--persistent_motionevent_use_center_swipe', type=str2bool, default=False,
                        help="Use real center-to-endpoint adb swipe for persistent_motionevent; more reliable on phones where input motionevent is accepted but does not move the in-game joystick")
    parser.add_argument('--persistent_center_swipe_hold_ms', type=int, default=10000,
                        help="Duration of one background center-to-edge joystick hold used by persistent_motionevent center-swipe fallback")
    parser.add_argument('--persistent_center_swipe_refresh_ms', type=int, default=4300,
                        help="Deprecated compatibility option; active center-to-edge holds are now kept until the adb swipe finishes")
    parser.add_argument('--move_debug', type=str2bool, default=False,
                        help="Print joystick execution coordinates and backend")
    parser.add_argument('--persistent_move_hold_ms', type=int, default=12000,
                        help="Duration of each background joystick hold in persistent_swipe mode")
    parser.add_argument('--persistent_move_refresh_ms', type=int, default=11000,
                        help="Refresh persistent joystick hold when this much time has elapsed")
    parser.add_argument('--persistent_move_angle_tolerance', type=int, default=18,
                        help="Angle tolerance used to reuse an existing persistent movement hold")
    parser.add_argument('--persistent_move_press_endpoint', type=str2bool, default=True,
                        help="In persistent_swipe mode, long-press the joystick endpoint instead of slowly swiping from center")
    parser.add_argument('--skill_swipe_duration_ms', type=int, default=120,
                        help="Duration of directional skill swipes")
    parser.add_argument('--long_press_unit_ms', type=int, default=150,
                        help="Base duration used by long-press actions")
    parser.add_argument('--action_burst_enabled', type=str2bool, default=True,
                        help="Expand one policy decision into a short burst of repeated controls")
    parser.add_argument('--action_burst_duration_ms', type=int, default=520,
                        help="Duration of one repeated-control burst")
    parser.add_argument('--move_repeat_interval_ms', type=int, default=150,
                        help="Interval between repeated movement swipes inside one burst")
    parser.add_argument('--attack_repeat_interval_ms', type=int, default=170,
                        help="Interval between repeated attack taps inside one burst")
    parser.add_argument('--basic_attack_repeat_interval_ms', type=int, default=100,
                        help="Start-to-start interval for repeated basic-attack taps during combat bursts")
    parser.add_argument('--skill_followup_attack_delay_ms', type=int, default=120,
                        help="Delay before basic-attack autofire after a skill")
    parser.add_argument('--autofire_basic_attack', type=str2bool, default=True,
                        help="Keep tapping basic attack during movement and after skill casts")
    parser.add_argument('--combat_assist_enabled', type=str2bool, default=True,
                        help="Autowzry-style low-level combat fallback: keep interacting when policy output is weak")
    parser.add_argument('--combat_assist_basic_attack_enabled', type=str2bool, default=True,
                        help="Tap basic attack periodically when no attack action is scheduled")
    parser.add_argument('--combat_assist_attack_interval_ms', type=int, default=260,
                        help="Minimum interval for fallback basic-attack taps")
    parser.add_argument('--combat_assist_skill_enabled', type=str2bool, default=True,
                        help="Cast fallback skills on a low-frequency cycle")
    parser.add_argument('--combat_assist_skill_interval_ms', type=int, default=2400,
                        help="Minimum interval between fallback skill casts")
    parser.add_argument('--combat_assist_skill_actions', type=str, default='8,9,10',
                        help="Comma-separated attack action ids used by fallback skill cycle")
    parser.add_argument('--combat_assist_directional_skills', type=str2bool, default=True,
                        help="Aim fallback skills along the current movement/target angle instead of tapping only")
    parser.add_argument('--combat_assist_skill_distance', type=int, default=55,
                        help="Swipe distance for fallback directional skills")
    parser.add_argument('--combat_assist_default_skill_angle', type=int, default=315,
                        help="Fallback skill aim angle when the action has no movement angle")
    parser.add_argument('--combat_assist_recover_enabled', type=str2bool, default=True,
                        help="Tap recover periodically during combat")
    parser.add_argument('--combat_assist_recover_interval_ms', type=int, default=16000,
                        help="Minimum interval between fallback recover taps")
    parser.add_argument('--combat_assist_buy_enabled', type=str2bool, default=True,
                        help="Tap quick-buy buttons periodically when info actions are not suppressed")
    parser.add_argument('--combat_assist_buy_interval_ms', type=int, default=9000,
                        help="Minimum interval between fallback quick-buy taps")
    parser.add_argument('--combat_assist_idle_move_enabled', type=str2bool, default=False,
                        help="Fill a no-move action with a light patrol move; off by default to avoid walking under tower")
    parser.add_argument('--combat_assist_idle_move_angle', type=int, default=315,
                        help="Movement angle used when fallback idle movement is enabled")
    parser.add_argument('--allow_info_attack_combo', type=str2bool, default=True,
                        help="Allow buy/upgrade/info actions to be executed in the same step as attack")
    parser.add_argument('--force_attack_while_moving', type=str2bool, default=True,
                        help="Fill missing attack branch with basic attack when the policy moves")
    parser.add_argument('--target_tracking_enabled', type=str2bool, default=True,
                        help="Use simple visual target tracking to bias movement and skills toward enemies")
    parser.add_argument('--target_tracking_min_area', type=int, default=40,
                        help="Minimum contour area for a target candidate")
    parser.add_argument('--target_tracking_max_area', type=int, default=6000,
                        help="Maximum contour area for a target candidate")
    parser.add_argument('--target_tracking_center_x', type=float, default=0.50,
                        help="Screen-relative center x used to compute pursuit angle")
    parser.add_argument('--target_tracking_center_y', type=float, default=0.56,
                        help="Screen-relative center y used to compute pursuit angle")
    parser.add_argument('--target_tracking_attack_distance', type=float, default=0.34,
                        help="Screen-relative distance under which basic attack is forced")
    parser.add_argument('--target_tracking_skill_distance', type=float, default=0.46,
                        help="Screen-relative distance under which directional skills are aimed")
    parser.add_argument('--target_tracking_smooth_factor', type=float, default=0.55,
                        help="Smoothing factor for target center tracking")
    parser.add_argument('--target_tracking_debug', type=str2bool, default=False,
                        help="Print target tracking diagnostics")
    parser.add_argument('--minimap_vision_enabled', type=str2bool, default=True,
                        help="Use minimap and attack-avatar cues for lane arrival and laning decisions")
    parser.add_argument('--minimap_roi_left', type=float, default=0.045,
                        help="Left bound of the minimap ROI")
    parser.add_argument('--minimap_roi_top', type=float, default=0.0,
                        help="Top bound of the minimap ROI")
    parser.add_argument('--minimap_roi_right', type=float, default=0.205,
                        help="Right bound of the minimap ROI")
    parser.add_argument('--minimap_roi_bottom', type=float, default=0.31,
                        help="Bottom bound of the minimap ROI")
    parser.add_argument('--minimap_lane_tower_auto_enabled', type=str2bool, default=False,
                        help="Try to snap the lane target to a blue/cyan tower candidate on the minimap")
    parser.add_argument('--minimap_self_lane_min_y', type=float, default=0.78,
                        help="Ignore self-marker candidates above this minimap-relative y when walking to the right lane")
    parser.add_argument('--minimap_self_min_area', type=float, default=25.0,
                        help="Minimum contour area for the green self marker on the minimap")
    parser.add_argument('--minimap_self_max_step_delta', type=float, default=0.35,
                        help="Reject minimap self candidates that jump too far from the previous self position in one frame")
    parser.add_argument('--minimap_self_prefer_min_x', type=float, default=0.18,
                        help="Prefer bottom-lane self candidates to the right of this x when available, avoiding the base icon")
    parser.add_argument('--minimap_self_prefer_min_y', type=float, default=0.78,
                        help="Prefer self candidates in this lower minimap band for right-lane routing")
    parser.add_argument('--minimap_self_max_x', type=float, default=0.84,
                        help="Ignore far-right minimap self candidates that are usually fixed edge icons before first-tower arrival")
    parser.add_argument('--minimap_self_base_icon_max_x', type=float, default=0.18,
                        help="Treat lower-left green candidates left of this x as the fixed base icon after opening starts")
    parser.add_argument('--minimap_self_base_icon_min_y', type=float, default=0.75,
                        help="Treat lower-left green candidates below this y as the fixed base icon after opening starts")
    parser.add_argument('--minimap_self_base_ignore_after_steps', type=int, default=8,
                        help="Opening minimap frames after which the fixed base icon is ignored for self tracking")
    parser.add_argument('--minimap_self_prediction_enabled', type=str2bool, default=True,
                        help="Predict self position along the lane when the green self rim is temporarily occluded")
    parser.add_argument('--minimap_self_predict_step_distance', type=float, default=0.0085,
                        help="Minimap-ratio distance advanced toward the lane tower per frame while self is occluded")
    parser.add_argument('--minimap_self_predict_max_missing_steps', type=int, default=180,
                        help="Maximum consecutive frames to use predicted self position")
    parser.add_argument('--minimap_lane_tower_min_x', type=float, default=0.55,
                        help="Ignore auto tower candidates left of this minimap-relative x for the right-lane first tower")
    parser.add_argument('--minimap_lane_tower_min_y', type=float, default=0.75,
                        help="Ignore auto tower candidates above this minimap-relative y for the right-lane first tower")
    parser.add_argument('--minimap_lane_tower_x', type=float, default=0.71,
                        help="Fallback minimap-relative x for the right-lane first blue tower")
    parser.add_argument('--minimap_lane_tower_y', type=float, default=0.93,
                        help="Fallback minimap-relative y for the right-lane first blue tower")
    parser.add_argument('--minimap_lane_arrival_enabled', type=str2bool, default=True,
                        help="Treat being close to the configured minimap tower as lane arrival")
    parser.add_argument('--minimap_lane_arrival_distance', type=float, default=0.16,
                        help="Minimap diagonal-relative distance threshold for reaching the lane tower")
    parser.add_argument('--minimap_lane_arrival_confirm_steps', type=int, default=1,
                        help="Consecutive minimap-arrival frames required before stopping opening movement")
    parser.add_argument('--minimap_lane_arrival_min_opening_steps', type=int, default=45,
                        help="Minimum opening steps before minimap tower arrival may stop forced right movement")
    parser.add_argument('--minimap_lane_move_y_weight', type=float, default=0.0,
                        help="Vertical component weight when converting minimap tower direction to joystick angle; 0.0 means right/left only, which is more reliable for opening lane movement")
    parser.add_argument('--minimap_require_lane_arrival_before_laning', type=str2bool, default=True,
                        help="When the minimap can see self, keep moving to the lane tower before entering laning")
    parser.add_argument('--minimap_reopen_false_visual_arrival', type=str2bool, default=True,
                        help="Resume opening movement if older visual cues marked arrival but the minimap says the tower is not reached")
    parser.add_argument('--minimap_reopen_confirm_steps', type=int, default=5,
                        help="Consecutive far-from-tower minimap frames required before reopening after arrival")
    parser.add_argument('--minimap_opening_steer_enabled', type=str2bool, default=True,
                        help="During opening, steer toward the minimap lane tower when self position is visible")
    parser.add_argument('--minimap_opening_steer_after_steps', type=int, default=0,
                        help="Earliest opening step where minimap steering can override the scripted route angle")
    parser.add_argument('--minimap_enemy_seek_enabled', type=str2bool, default=True,
                        help="Use minimap enemy direction for short repositioning when no target is attackable")
    parser.add_argument('--minimap_enemy_seek_interval_steps', type=int, default=5,
                        help="Minimum no-wave steps between short minimap enemy seek moves")
    parser.add_argument('--minimap_enemy_seek_min_distance', type=float, default=0.08,
                        help="Minimum minimap distance before seeking an enemy")
    parser.add_argument('--minimap_enemy_seek_max_distance', type=float, default=0.55,
                        help="Maximum minimap distance used for safe enemy seeking")
    parser.add_argument('--minimap_attack_avatar_enabled', type=str2bool, default=True,
                        help="Detect the red enemy avatar above skills as a basic-attack range cue")
    parser.add_argument('--minimap_attack_roi_left', type=float, default=0.55,
                        help="Left bound of the attackable-avatar ROI")
    parser.add_argument('--minimap_attack_roi_top', type=float, default=0.28,
                        help="Top bound of the attackable-avatar ROI")
    parser.add_argument('--minimap_attack_roi_right', type=float, default=0.82,
                        help="Right bound of the attackable-avatar ROI")
    parser.add_argument('--minimap_attack_roi_bottom', type=float, default=0.70,
                        help="Bottom bound of the attackable-avatar ROI")
    parser.add_argument('--minimap_attack_avatar_min_area', type=int, default=700,
                        help="Minimum red circular area for the attackable-avatar cue")
    parser.add_argument('--minimap_dark_death_enabled', type=str2bool, default=True,
                        help="Use global dark-screen cue as a death/wait-for-respawn signal")
    parser.add_argument('--minimap_dark_death_mean_v_threshold', type=float, default=52.0,
                        help="Mean HSV V threshold for dark death-screen detection")
    parser.add_argument('--minimap_dark_death_fraction_threshold', type=float, default=0.72,
                        help="Dark-pixel fraction threshold for death-screen detection")
    parser.add_argument('--minimap_debug', type=str2bool, default=False,
                        help="Print minimap vision diagnostics")
    parser.add_argument('--lane_macro_enabled', type=str2bool, default=True,
                        help="Use scripted early laning macro before relying on the learned policy")
    parser.add_argument('--lane_macro_control_steps', type=int, default=220,
                        help="Number of post-opening laning steps controlled by the laning macro")
    parser.add_argument('--lane_profile_enabled', type=str2bool, default=True,
                        help="Load learned early-lane macro profile from JSON when available")
    parser.add_argument('--lane_profile_path', type=str, default='src/lane_profile.json',
                        help="JSON profile generated by build_lane_profile.py")
    parser.add_argument('--lane_profile_replay_steps', type=int, default=60,
                        help="Number of early steps that replay action schedule from the lane profile")
    parser.add_argument('--lane_opening_route_angles', type=str, default='195,205,190,175',
                        help="Comma-separated movement angles used to leave base and reach lane")
    parser.add_argument('--lane_opening_route_steps', type=str, default='8,14,12,8',
                        help="Comma-separated step counts for each opening route angle")
    parser.add_argument('--lane_opening_route_source', type=str, default='auto',
                        choices=['auto', 'autowzry_mid', 'profile', 'manual'],
                        help="Opening route source: auto prefers a trusted lane_profile.json and falls back to autowzry_mid")
    parser.add_argument('--lane_opening_force_right_until_tower', type=str2bool, default=True,
                        help="Before the minimap confirms arrival at the right-lane tower, always hold the joystick to the right")
    parser.add_argument('--lane_opening_right_angle', type=int, default=7,
                        help="Joystick angle used by the hard right-lane opening rule")
    parser.add_argument('--lane_opening_force_right_hard_max_steps', type=int, default=260,
                        help="Hard cap for forced-right opening steps when minimap arrival never confirms")
    parser.add_argument('--lane_opening_profile_min_move_ratio', type=float, default=0.65,
                        help="Minimum opening move ratio required before auto trusts lane_profile.json")
    parser.add_argument('--lane_opening_autowzry_angles', type=str, default='300,310,320',
                        help="Autowzry-inspired opening angles, converted from repeated up-right joystick swipes")
    parser.add_argument('--lane_opening_autowzry_steps', type=str, default='34,28,18',
                        help="Step counts for the autowzry-inspired opening route")
    parser.add_argument('--lane_opening_force_stop_at_base', type=str2bool, default=True,
                        help="Stop opening movement as soon as the minimum opening route is complete")
    parser.add_argument('--lane_opening_ignore_visual_before_base', type=str2bool, default=True,
                        help="Ignore visual lane-arrival/danger cues before the base opening route is complete")
    parser.add_argument('--lane_opening_stop_on_contact', type=str2bool, default=False,
                        help="During opening, stop early once minions or an enemy are visually confirmed")
    parser.add_argument('--lane_opening_contact_detect_after_steps', type=int, default=12,
                        help="Earliest opening step where minion/enemy contact is allowed to stop movement")
    parser.add_argument('--lane_opening_contact_pressure_threshold', type=float, default=0.16,
                        help="Center visual pressure threshold treated as minion-wave contact during opening")
    parser.add_argument('--lane_opening_contact_confirm_steps', type=int, default=2,
                        help="Consecutive contact frames required before stopping opening movement")
    parser.add_argument('--lane_opening_angle_jitter', type=int, default=7,
                        help="Random angle jitter applied to opening route movement")
    parser.add_argument('--lane_opening_move_only_jitter', type=int, default=0,
                        help="Angle jitter used during pure opening movement")
    parser.add_argument('--lane_opening_attack_start_step', type=int, default=8,
                        help="Step after which opening route starts basic attacking")
    parser.add_argument('--lane_opening_move_only_steps', type=int, default=28,
                        help="Opening steps that must be pure movement with no attack/info actions")
    parser.add_argument('--lane_opening_min_move_steps', type=int, default=150,
                        help="Minimum pure-movement opening steps even when the learned lane profile is shorter")
    parser.add_argument('--lane_opening_extend_last_angle', type=str2bool, default=True,
                        help="Keep moving with the last route angle after the profile route ends until lane arrival")
    parser.add_argument('--lane_opening_disable_profile_attacks', type=str2bool, default=True,
                        help="Ignore replayed attack schedule during pure opening movement")
    parser.add_argument('--lane_opening_disable_info_actions', type=str2bool, default=True,
                        help="Ignore buy/upgrade/info actions during pure opening movement")
    parser.add_argument('--lane_opening_skill_start_step', type=int, default=16,
                        help="Step after which opening route may cast skills")
    parser.add_argument('--lane_opening_second_buy_step', type=int, default=24,
                        help="Second early equipment purchase attempt step")
    parser.add_argument('--lane_laning_patrol_angles', type=str, default='175,190,205,190',
                        help="Movement angles used when clearing wave without a visible target")
    parser.add_argument('--lane_laning_skill_interval_steps', type=int, default=7,
                        help="Interval for skill casts during early laning macro")
    parser.add_argument('--lane_laning_last_hit_interval_steps', type=int, default=4,
                        help="Interval for last-hit button during early laning macro")
    parser.add_argument('--lane_laning_push_tower_interval_steps', type=int, default=13,
                        help="Interval for push-tower button during early laning macro")
    parser.add_argument('--lane_laning_pressure_threshold', type=float, default=0.14,
                        help="Visual pressure threshold that indicates minion/wave activity near lane center")
    parser.add_argument('--lane_laning_hold_pressure_threshold', type=float, default=0.34,
                        help="Pressure threshold where laning macro stops moving and focuses on clearing/last hitting")
    parser.add_argument('--lane_laning_low_pressure_threshold', type=float, default=0.05,
                        help="Pressure threshold below which laning macro patrols to find wave or target")
    parser.add_argument('--lane_laning_find_wave_move_enabled', type=str2bool, default=False,
                        help="Allow movement while no wave/target is visible; disabled by default to avoid walking under tower")
    parser.add_argument('--lane_laning_standstill_clear_enabled', type=str2bool, default=True,
                        help="Stop the joystick while clearing a visible wave to avoid drifting past lane")
    parser.add_argument('--lane_laning_kite_enabled', type=str2bool, default=True,
                        help="Use backward movement when target is very close or own HP is low")
    parser.add_argument('--lane_laning_kite_distance', type=float, default=0.18,
                        help="Target distance ratio under which the laning macro kites backward")
    parser.add_argument('--lane_laning_trade_distance', type=float, default=0.34,
                        help="Target distance ratio under which the laning macro trades in place or with light movement")
    parser.add_argument('--lane_laning_debug', type=str2bool, default=False,
                        help="Print laning macro visual-state diagnostics")
    parser.add_argument('--lane_arrival_detection_enabled', type=str2bool, default=True,
                        help="Detect arrival at lane using visual pressure and visible target cues")
    parser.add_argument('--lane_arrival_detect_after_steps', type=int, default=55,
                        help="Earliest macro step where lane-arrival visual detection is trusted")
    parser.add_argument('--lane_arrival_extend_steps', type=int, default=20,
                        help="Extra opening movement steps allowed while lane arrival has not been detected")
    parser.add_argument('--lane_arrival_pressure_enabled', type=str2bool, default=True,
                        help="Allow center visual pressure to end opening movement; off by default because it can fire inside base")
    parser.add_argument('--lane_arrival_pressure_threshold', type=float, default=0.40,
                        help="Center visual pressure threshold used as a lane-arrival cue")
    parser.add_argument('--lane_arrival_confirm_steps', type=int, default=3,
                        help="Consecutive visual-arrival frames required before ending opening movement")
    parser.add_argument('--lane_arrival_target_enabled', type=str2bool, default=False,
                        help="Treat a visible target after the detection delay as a lane-arrival cue")
    parser.add_argument('--lane_opening_abort_on_danger', type=str2bool, default=True,
                        help="Abort opening movement early when strong combat/tower-danger cues appear")
    parser.add_argument('--lane_opening_danger_after_steps', type=int, default=45,
                        help="Earliest opening step where danger cues are allowed to abort pure movement")
    parser.add_argument('--lane_opening_danger_pressure_threshold', type=float, default=0.72,
                        help="Visual pressure threshold that is treated as danger during opening movement")
    parser.add_argument('--lane_opening_danger_target_distance', type=float, default=0.14,
                        help="Visible target distance ratio that is treated as danger during opening movement")
    parser.add_argument('--lane_opening_danger_low_hp_enabled', type=str2bool, default=False,
                        help="Allow low-HP visual estimate to abort opening movement; disabled because HP ROI can be noisy early")
    parser.add_argument('--lane_opening_danger_confirm_steps', type=int, default=2,
                        help="Consecutive danger frames required before aborting opening movement")
    parser.add_argument('--lane_arrival_stop_steps', type=int, default=1,
                        help="Number of all-zero steps used to release the persistent joystick after arrival")
    parser.add_argument('--lane_arrival_debug', type=str2bool, default=False,
                        help="Print lane-arrival detection diagnostics")
    parser.add_argument('--phase_controller_enabled', type=str2bool, default=True,
                        help="Use phase-based gameplay controller after opening lane macro")
    parser.add_argument('--phase_debug', type=str2bool, default=False,
                        help="Print phase controller diagnostics")
    parser.add_argument('--phase_low_hp_threshold', type=float, default=0.32,
                        help="Estimated HP ratio under which the phase controller retreats")
    parser.add_argument('--phase_low_hp_confirm_steps', type=int, default=3,
                        help="Consecutive low-HP visual frames required before phase retreat")
    parser.add_argument('--phase_self_hp_roi_left', type=float, default=0.30,
                        help="Left bound of the self-HP search ROI")
    parser.add_argument('--phase_self_hp_roi_right', type=float, default=0.72,
                        help="Right bound of the self-HP search ROI")
    parser.add_argument('--phase_self_hp_roi_top', type=float, default=0.28,
                        help="Top bound of the self-HP search ROI")
    parser.add_argument('--phase_self_hp_roi_bottom', type=float, default=0.68,
                        help="Bottom bound of the self-HP search ROI")
    parser.add_argument('--phase_self_hp_expected_width_ratio', type=float, default=0.085,
                        help="Expected full self HP-bar width relative to the screen width")
    parser.add_argument('--phase_trade_distance', type=float, default=0.32,
                        help="Target distance ratio under which the controller enters trade phase")
    parser.add_argument('--phase_retreat_angle', type=int, default=225,
                        help="Fallback retreat movement angle")
    parser.add_argument('--phase_retreat_steps', type=int, default=8,
                        help="Number of steps to keep retreating after low HP is detected")
    parser.add_argument('--phase_recover_steps', type=int, default=8,
                        help="Number of safe recovery steps after death/recover events")
    parser.add_argument('--phase_trade_skill_interval_steps', type=int, default=5,
                        help="Skill interval while trading with visible close targets")
    parser.add_argument('--phase_chase_skill_interval_steps', type=int, default=6,
                        help="Skill interval while chasing visible far targets")
    parser.add_argument('--phase_push_start_step', type=int, default=100,
                        help="Earliest step where push-tower phase can be considered")
    parser.add_argument('--phase_push_tower_interval_steps', type=int, default=12,
                        help="Interval for tower-pressure actions after laning starts")
    parser.add_argument('--phase_last_hit_interval_steps', type=int, default=4,
                        help="Interval for last-hit actions in laning phase")
    parser.add_argument('--phase_clear_skill_interval_steps', type=int, default=7,
                        help="Interval for lane-clear skills when visual pressure is high")
    parser.add_argument('--phase_clear_wave_pressure', type=float, default=0.16,
                        help="Center pressure score threshold for lane-clear skill use")
    parser.add_argument('--phase_laning_patrol_angles', type=str, default='175,190,205,190',
                        help="Patrol angles used by the phase controller during laning")
    parser.add_argument('--rookie_laner_enabled', type=str2bool, default=True,
                        help="Use a conservative intent layer that behaves like a low-skill laning player")
    parser.add_argument('--rookie_laner_wave_pressure', type=float, default=0.14,
                        help="Center pressure threshold treated as visible minion wave for rookie laning")
    parser.add_argument('--rookie_laner_push_pressure', type=float, default=0.34,
                        help="Pressure required before safe tower-push attempts")
    parser.add_argument('--rookie_laner_trade_distance', type=float, default=0.30,
                        help="Target distance ratio where rookie laner trades in place")
    parser.add_argument('--rookie_laner_poke_distance', type=float, default=0.46,
                        help="Target distance ratio where rookie laner pokes without chasing")
    parser.add_argument('--rookie_laner_chase_enabled', type=str2bool, default=False,
                        help="Allow chasing far visible targets; off by default to avoid tower dives")
    parser.add_argument('--rookie_laner_poke_move_enabled', type=str2bool, default=False,
                        help="Move toward a poke target; off by default for conservative laning")
    parser.add_argument('--rookie_laner_no_wave_patrol_enabled', type=str2bool, default=False,
                        help="Patrol lightly when no wave or target is visible")
    parser.add_argument('--rookie_laner_no_wave_patrol_interval_steps', type=int, default=10,
                        help="Interval for no-wave patrol moves when rookie patrol is enabled")
    parser.add_argument('--rookie_laner_hold_attack_interval_steps', type=int, default=3,
                        help="Interval for basic attacks while holding lane")
    parser.add_argument('--finish_check_interval_steps', type=int, default=30,
                        help="How often to run expensive OCR-based terminal checks")
    parser.add_argument('--finish_check_start_step', type=int, default=120,
                        help="Skip expensive OCR-based terminal checks before this episode step")
    parser.add_argument('--death_check_interval_steps', type=int, default=5,
                        help="How often to run expensive death ONNX checks")
    parser.add_argument('--action_preview_enabled', type=str2bool, default=True,
                        help="Print selected action immediately before env.step for early-step debugging")
    parser.add_argument('--action_preview_steps', type=int, default=30,
                        help="Number of early episode steps for immediate action preview printing")
    parser.add_argument('--step_timing_debug', type=str2bool, default=True,
                        help="Record and print split timing diagnostics for action selection and env.step")
    parser.add_argument('--traffic_probe_mode', type=str, default='adb_proc_net_dev',
                        choices=['disabled', 'adb_proc_net_dev'],
                        help="Traffic probe backend")
    parser.add_argument('--traffic_probe_interfaces', type=str, default='',
                        help="Comma-separated network interfaces to include; empty means auto-select non-loopback")
    parser.add_argument('--traffic_probe_timeout_sec', type=float, default=1.0,
                        help="Timeout used by the traffic probe backend")
    parser.add_argument('--traffic_bytes_target', type=int, default=12000,
                        help="Bytes per step that saturate the traffic reward")
    parser.add_argument('--traffic_packets_target', type=int, default=24,
                        help="Packets per step that saturate the traffic reward")
    parser.add_argument('--traffic_reward_weight', type=float, default=0.0,
                        help="Reward weight for observed traffic activation")
    parser.add_argument('--traffic_inactive_penalty', type=float, default=0.0,
                        help="Penalty when an active action produces no observed traffic")
    parser.add_argument('--latency_target_ms', type=float, default=350.0,
                        help="Step latency target used as a response-quality proxy")
    parser.add_argument('--latency_reward_weight', type=float, default=0.0,
                        help="Reward weight for response latency quality")
    parser.add_argument('--step_metrics_path', type=str, default='src/step_metrics.jsonl',
                        help="JSONL file used to append per-step probe metrics")
    parser.add_argument('--behavior_prior_mode', type=str, default='smart',
                        choices=['smart', 'weighted', 'uniform'],
                        help="Exploration behavior prior used before the learned model becomes reliable")
    parser.add_argument('--macro_buy_interval_steps', type=int, default=45,
                        help="Step interval for automatic equipment purchase attempts in smart prior")
    parser.add_argument('--macro_upgrade_interval_steps', type=int, default=18,
                        help="Step interval for skill upgrade attempts in smart prior")
    parser.add_argument('--macro_direction_hold_steps', type=int, default=14,
                        help="Minimum steps to keep a movement direction before changing lane pressure")
    parser.add_argument('--human_data_dir', type=str, default='human_data',
                        help="Directory used to store human demonstration data")
    parser.add_argument('--human_episode_id', type=str, default='',
                        help="Episode id used by human_data_collector.py; empty means timestamp")
    parser.add_argument('--human_collect_fps', type=float, default=5.0,
                        help="Frame capture rate used by human_data_collector.py")
    parser.add_argument('--human_collect_seconds', type=float, default=0.0,
                        help="Collection duration in seconds; 0 means run until Ctrl+C")
    parser.add_argument('--human_action_only', type=str2bool, default=False,
                        help="Collect touch/action samples without taking screenshots; useful for lane profiles")
    parser.add_argument('--human_save_frames', type=str2bool, default=True,
                        help="Save captured frames to disk; disable for lightweight action-profile collection")
    parser.add_argument('--human_frame_max_width', type=int, default=0,
                        help="Resize saved frames to this max width; 0 keeps original size")
    parser.add_argument('--human_jpeg_quality', type=int, default=85,
                        help="JPEG quality for saved human demonstration frames")
    parser.add_argument('--human_collect_traffic', type=str2bool, default=True,
                        help="Collect per-sample traffic metrics during human data collection")
    parser.add_argument('--human_capture_source', type=str, default='scrcpy_window',
                        choices=['scrcpy_window', 'adb_screencap'],
                        help="Frame source used by human_data_collector.py")
    parser.add_argument('--adb_screenshot_method', type=str, default='remote_file',
                        choices=['remote_file', 'exec_out', 'shell', 'auto'],
                        help="ADB screenshot method used when capture source is adb_screencap")
    parser.add_argument('--adb_screenshot_timeout_sec', type=float, default=12.0,
                        help="Timeout for each ADB screenshot command")
    parser.add_argument('--touch_device', type=str, default='',
                        help="Optional /dev/input/eventX path for getevent; empty means listen to all events")
    parser.add_argument('--touch_label_window_ms', type=int, default=180,
                        help="Future time window used to map touches to the captured frame")
    parser.add_argument('--touch_hit_radius_ratio', type=float, default=0.055,
                        help="Default button hit radius relative to the shorter screen side")
    parser.add_argument('--touch_move_deadzone_ratio', type=float, default=0.025,
                        help="Joystick dead zone relative to the shorter screen side")
    parser.add_argument('--touch_raw_width', type=int, default=0,
                        help="Raw getevent X range; 0 means use device screen width")
    parser.add_argument('--touch_raw_height', type=int, default=0,
                        help="Raw getevent Y range; 0 means use device screen height")
    parser.add_argument('--touch_swap_xy', action='store_true',
                        help="Swap raw touch X/Y before mapping to game coordinates")
    parser.add_argument('--touch_invert_x', action='store_true',
                        help="Invert raw touch X before mapping to game coordinates")
    parser.add_argument('--touch_invert_y', action='store_true',
                        help="Invert raw touch Y before mapping to game coordinates")
    parser.add_argument('--bc_samples_path', type=str, default='',
                        help="JSONL samples file used by train_behavior_clone.py")
    parser.add_argument('--bc_epochs', type=int, default=5,
                        help="Behavior cloning training epochs")
    parser.add_argument('--bc_batch_size', type=int, default=16,
                        help="Behavior cloning batch size")
    parser.add_argument('--bc_learning_rate', type=float, default=0.0003,
                        help="Behavior cloning learning rate")
    parser.add_argument('--bc_model_out', type=str, default='src/wzry_ai_bc.pt',
                        help="Output model path for behavior cloning")
    parser.add_argument('--bc_num_workers', type=int, default=0,
                        help="DataLoader workers for behavior cloning; keep 0 on Windows unless needed")
    parser.add_argument('--relabel_episode_dir', type=str, default='',
                        help="Episode directory used by relabel_human_actions.py; empty means latest")
    parser.add_argument('--relabel_output_path', type=str, default='',
                        help="Output JSONL path for relabeled samples; empty means samples_relabel.jsonl")
    parser.add_argument('--relabel_in_place', action='store_true',
                        help="Overwrite samples.jsonl with relabeled actions")
    parser.add_argument('--relabel_preview_limit', type=int, default=20,
                        help="Number of most common relabeled actions to print")

    return parser.parse_args()


# 解析参数并存储在全局变量中
args = get_args()

device = torch.device(args.device_id if torch.cuda.is_available() else 'cpu')

# 全局状态
globalInfo = GlobalInfo(batch_size=args.batch_size, buffer_capacity=args.memory_size)
