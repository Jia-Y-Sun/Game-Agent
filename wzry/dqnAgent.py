import os

import cv2
import numpy as np
import torch
from torch import optim, nn

from argparses import device, args, globalInfo
from gameplay_policy import GameplayBehaviorPrior
from memory import Transition
from net_actor import NetDQN

class DQNAgent:
    def __init__(self):
        torch.backends.cudnn.enabled = False

        self.action_sizes = [2, 360, 9, 11, 3, 360, 100, 5]
        self.device = device
        self.batch_size = args.batch_size
        self.gamma = args.gamma
        self.epsilon = args.epsilon
        self.epsilon_decay = args.epsilon_decay
        self.epsilon_min = args.epsilon_min
        self.learning_rate = args.learning_rate

        self.steps_done = 0
        self.target_update = args.target_update
        self.behavior_prior = GameplayBehaviorPrior()
        self.last_policy_info = {"source": "init", "macro": None}

        self.policy_net = NetDQN().to(self.device)
        self.target_net = NetDQN().to(self.device)
        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=self.learning_rate)
        self.criterion = nn.SmoothL1Loss()

        if args.model_path and os.path.exists(args.model_path):
            self.policy_net.load_state_dict(torch.load(args.model_path))
            self.target_net.load_state_dict(self.policy_net.state_dict())
            print(f"Model loaded from {args.model_path}")

    def save_model(self, path):
        torch.save(self.policy_net.state_dict(), path)
        print(f"Model saved to {path}")

    def reset_episode(self):
        self.behavior_prior.reset_episode()
        self.last_policy_info = {"source": "episode_reset", "macro": None}

    def select_action(self, state):
        if np.random.rand() <= self.epsilon:
            return self._sample_exploration_action()
        tmp_state_640_640 = self.preprocess_image(state).unsqueeze(0)
        self.policy_net.eval()
        with torch.no_grad():
            q_values = self.policy_net(tmp_state_640_640)
        self.last_policy_info = {"source": "model", "macro": None}
        return self._normalize_action([np.argmax(q.detach().cpu().numpy()) for q in q_values])

    def _sample_exploration_action(self):
        if args.behavior_prior_mode == "smart":
            action, macro = self.behavior_prior.sample()
            self.last_policy_info = {"source": "smart_prior", "macro": macro}
            return self._normalize_action(action)

        if args.behavior_prior_mode == "uniform":
            self.last_policy_info = {"source": "uniform", "macro": None}
            return self._normalize_action([np.random.randint(size) for size in self.action_sizes])

        self.last_policy_info = {"source": "weighted", "macro": None}
        move_action = int(np.random.choice([0, 1], p=[0.25, 0.75]))
        angle = int(np.random.randint(360)) if move_action == 1 else 0

        info_action = int(np.random.choice(
            list(range(9)),
            p=[0.70, 0.02, 0.02, 0.02, 0.02, 0.02, 0.07, 0.07, 0.06],
        ))
        attack_action = int(np.random.choice(
            list(range(11)),
            p=[0.10, 0.35, 0.10, 0.05, 0.02, 0.02, 0.04, 0.06, 0.10, 0.08, 0.08],
        ))

        action_type = 0
        arg1 = 0
        arg2 = 0
        arg3 = 0
        if attack_action >= 7:
            action_type = int(np.random.choice([0, 1, 2], p=[0.70, 0.25, 0.05]))
            arg1 = int(np.random.randint(360))
            arg2 = int(np.random.randint(100))
            arg3 = int(np.random.randint(5))

        return self._normalize_action([
            move_action,
            angle,
            info_action,
            attack_action,
            action_type,
            arg1,
            arg2,
            arg3,
        ])

    def _normalize_action(self, action):
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = [int(v) for v in action]
        if move_action == 0:
            angle = 0
        if attack_action == 0 or attack_action < 7:
            action_type = 0
            arg1 = 0
            arg2 = 0
            arg3 = 0
        return [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]

    def preprocess_image(self, image, target_size=(640, 640)):
        # 调整图像大小
        resized_image = cv2.resize(image, target_size)
        # 转换为张量并调整维度顺序 [height, width, channels] -> [channels, height, width]
        tensor_image = torch.from_numpy(resized_image).float().permute(2, 0, 1) / 255.0
        return tensor_image.to(device)

    def replay(self):
        transitions = globalInfo.random_batch_size_memory_dqn()
        batch = Transition(*zip(*transitions))

        # 将 batch 转换为张量，并移动到设备上
        batch_state = torch.stack([self.preprocess_image(state) for state in batch.state]).to(device)
        batch_action = torch.LongTensor(batch.action).to(self.device)
        batch_reward = torch.FloatTensor(batch.reward).to(self.device)
        batch_next_state = torch.stack([self.preprocess_image(state) for state in batch.next_state]).to(device)
        batch_done = torch.FloatTensor(batch.done).to(self.device)

        # 计算当前状态的 Q 值
        state_action_values = self.policy_net(batch_state)

        # 计算每个动作类别的 Q 值
        move_action_q, angle_q, info_action_q, attack_action_q, action_type_q, arg1_q, arg2_q, arg3_q = state_action_values

        # 选择执行的动作的 Q 值
        state_action_q_values = move_action_q.gather(1, batch_action[:, 0].unsqueeze(1)) + \
                                angle_q.gather(1, batch_action[:, 1].unsqueeze(1)) + \
                                info_action_q.gather(1, batch_action[:, 2].unsqueeze(1)) + \
                                attack_action_q.gather(1, batch_action[:, 3].unsqueeze(1)) + \
                                action_type_q.gather(1, batch_action[:, 4].unsqueeze(1)) + \
                                arg1_q.gather(1, batch_action[:, 5].unsqueeze(1)) + \
                                arg2_q.gather(1, batch_action[:, 6].unsqueeze(1)) + \
                                arg3_q.gather(1, batch_action[:, 7].unsqueeze(1))

        # 计算下一个状态的 Q 值
        next_state_values = torch.zeros(self.batch_size, device=self.device)
        non_final_mask = (batch_done == 0)
        non_final_next_states = batch_next_state[non_final_mask]
        if non_final_next_states.size(0) > 0:
            next_state_action_values = self.target_net(non_final_next_states)
            next_move_action_q, next_angle_q, next_info_action_q, next_attack_action_q, next_action_type_q, next_arg1_q, next_arg2_q, next_arg3_q = next_state_action_values
            next_state_values[non_final_mask] = torch.max(next_move_action_q, 1)[0] + \
                                                torch.max(next_angle_q, 1)[0] + \
                                                torch.max(next_info_action_q, 1)[0] + \
                                                torch.max(next_attack_action_q, 1)[0] + \
                                                torch.max(next_action_type_q, 1)[0] + \
                                                torch.max(next_arg1_q, 1)[0] + \
                                                torch.max(next_arg2_q, 1)[0] + \
                                                torch.max(next_arg3_q, 1)[0]

        # 计算期望的 Q 值
        expected_state_action_values = batch_reward + self.gamma * next_state_values * (1 - batch_done)

        # 计算损失
        loss = self.criterion(state_action_q_values, expected_state_action_values.unsqueeze(1))

        print("loss", loss)

        # 优化模型
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay

        if self.steps_done % self.target_update == 0:
            self.target_net.load_state_dict(self.policy_net.state_dict())

        self.steps_done += 1
