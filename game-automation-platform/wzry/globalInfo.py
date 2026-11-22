import json
import os
import threading

from filelock import FileLock

from memory import ReplayMemory


def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance


@singleton
class GlobalInfo:
    def __init__(self, batch_size=64, buffer_capacity=10000):
        self.batch_size = batch_size
        self._info = {}
        self.ppo_memory = ReplayMemory(buffer_capacity)
        self.td3_memory = ReplayMemory(buffer_capacity)
        self.dqn_memory = ReplayMemory(buffer_capacity)
        self.lock = threading.Lock()

    def set_value(self, key, value):
        self._info[key] = value

    def get_value(self, key):
        return self._info.get(key, None)

    # -------------------------------对局状态-------------------------------------
    def set_game_start(self):
        self.set_value('start_game', True)

    def is_start_game(self):
        start_game = self.get_value('start_game')
        if start_game is None:
            return False
        else:
            return start_game

    def set_game_end(self):
        self.set_value('start_game', False)

    # -------------------------------ppo经验池-------------------------------------
    def store_transition_ppo(self, *args):
        with self.lock:
            self.ppo_memory.push(*args)

    def is_memory_bigger_batch_size_ppo(self):
        with self.lock:
            return len(self.ppo_memory) >= self.batch_size

    def random_batch_size_memory_ppo(self):
        with self.lock:
            return self.ppo_memory.sample(self.batch_size)

    # -------------------------------td3经验池-------------------------------------
    def store_transition_td3(self, *args):
        with self.lock:
            self.td3_memory.push(*args)

    def is_memory_bigger_batch_size_td3(self):
        with self.lock:
            return len(self.td3_memory) >= self.batch_size

    def random_batch_size_memory_td3(self):
        with self.lock:
            return self.td3_memory.sample(self.batch_size)

    # -------------------------------dqn经验池-------------------------------------
    def store_transition_dqn(self, *args):
        """保存一条 DQN 经验。

        兼容两种调用：
        `(state, action, reward, next_state, done)` 和
        `(state, action, reward, next_state, done, human_action, priority)`。
        """
        with self.lock:
            self.dqn_memory.push(*args)

    def is_memory_bigger_batch_size_dqn(self):
        with self.lock:
            return len(self.dqn_memory) >= self.batch_size

    def random_batch_size_memory_dqn(self, human_fraction=0.0, human_boost=1.0):
        """采样一个训练批次。

        `human_fraction > 0` 时，人为示范样本会占固定配额，
        避免人工接管产生的少量高质量经验被随机探索冲淡。
        """
        with self.lock:
            if human_fraction and human_fraction > 0:
                return self.dqn_memory.sample_mixed(
                    self.batch_size,
                    human_fraction=human_fraction,
                    human_boost=human_boost,
                )
            return self.dqn_memory.sample(self.batch_size)

    def dqn_memory_stats(self):
        with self.lock:
            return self.dqn_memory.stats()

    # -------------------------------训练状态-------------------------------------
    def update_data_file(self, new_data):
        lock = FileLock("training_data.json.lock")

        with lock:
            if os.path.exists('training_data.json'):
                with open('training_data.json', 'r') as file:
                    data = json.load(file)
            else:
                data = []

            for new_plot in new_data:
                title_found = False
                for existing_plot in data:
                    if existing_plot['title'] == new_plot['title']:
                        existing_plot['x_data'].extend(new_plot['x_data'])
                        existing_plot['y_data'].extend(new_plot['y_data'])
                        title_found = True
                        break

                if not title_found:
                    data.append(new_plot)

            with open('training_data.json', 'w') as file:
                json.dump(data, file, indent=4)
