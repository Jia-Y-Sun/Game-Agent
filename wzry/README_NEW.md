<p align="center">
    <a href="https://github.com/myBoris/wzry_ai">
        <img src="https://socialify.git.ci/myBoris/wzry_ai/image?description=1&font=Rokkitt&language=1&name=1&owner=1&theme=Auto" alt="wzry_ai"/>    
    </a>
</p>

<p align="center">
    <a href="https://github.com/myBoris/wzry_ai/stargazers">
        <img src="https://img.shields.io/github/stars/myBoris/wzry_ai?style=flat-square&label=STARS&color=%23dfb317" alt="stars">
    </a>
    <a href="https://github.com/myBoris/wzry_ai/network/members">
        <img src="https://img.shields.io/github/forks/myBoris/wzry_ai?style=flat-square&label=FORKS&color=%2397ca00" alt="forks">
    </a>
    <a href="https://github.com/myBoris/wzry_ai/issues">
        <img src="https://img.shields.io/github/issues/myBoris/wzry_ai?style=flat-square&label=ISSUES&color=%23007ec6" alt="issues">
    </a>
    <a href="https://github.com/myBoris/wzry_ai/pulls">
        <img src="https://img.shields.io/github/issues-pr/myBoris/wzry_ai?style=flat-square&label=PULLS&color=%23fe7d37" alt="pulls">
    </a>
</p>

---

> **声明**: 本项目的目的是为了学习人工智能，严禁外挂

---

## 📖 目录

- [项目简介](#项目简介)
- [核心文件详解](#核心文件详解)
- [环境配置](#环境配置)
- [实验步骤](#实验步骤)
- [参数配置说明](#参数配置说明)
- [常见问题](#常见问题)

---

## 项目简介

这是一个基于**深度强化学习（DQN）**的人工智能项目，用于学习如何玩王者荣耀游戏。项目通过屏幕截图获取游戏状态，使用神经网络决策动作，并通过ADB工具在手机/模拟器上执行操作。

### 技术栈
- **深度学习框架**: PyTorch
- **强化学习算法**: DQN (Deep Q-Network)
- **图像识别**: ONNX Runtime, OpenCV
- **设备控制**: ADB (Android Debug Bridge), scrcpy
- **OCR识别**: PPOCR-ONNX

---

## 核心文件详解

### 🎯 主程序文件

#### `train.py` - 训练主程序
**作用**: 项目的核心入口，负责启动训练流程

**主要功能**:
1. **数据收集线程** (`data_collector`):
   - 持续截取游戏屏幕画面
   - 使用ONNX模型检测对局是否开始
   - 对局开始后，通过DQN Agent选择动作
   - 收集经验数据（状态、动作、奖励、下一状态）

2. **训练线程** (`train_agent`):
   - 从经验池中随机采样进行训练
   - 定期保存模型到 `src/wzry_ai.pt`

**运行方式**:
```bash
python train.py
```

---

#### `dqnAgent.py` - DQN智能体
**作用**: 实现DQN算法的核心逻辑

**主要组件**:
- **策略网络** (`policy_net`): 用于选择动作
- **目标网络** (`target_net`): 用于计算目标Q值
- **经验回放**: 从经验池中学习

**核心方法**:
- `select_action(state)`: 根据当前状态选择动作（ε-贪婪策略）
- `replay()`: 从经验池采样并训练网络
- `save_model(path)`: 保存训练好的模型

**动作空间** (8维):
1. **移动动作** (2维): 0=不移动, 1=移动
2. **移动角度** (360维): 0-359度
3. **信息动作** (9维): 购买装备、发起进攻、撤退等
4. **攻击动作** (11维): 攻击、攻击小兵、技能等
5. **动作类型** (3维): 点击、滑动、长按
6. **参数1** (360维): 技能释放角度
7. **参数2** (100维): 技能释放距离
8. **参数3** (5维): 长按时间

---

#### `wzry_env.py` - 游戏环境
**作用**: 封装游戏环境，提供强化学习标准接口

**核心方法**:
- `step(action)`: 执行动作，返回 `(next_state, reward, done, info)`
  - 执行移动、信息、攻击操作
  - 获取下一帧截图
  - 计算奖励和游戏状态

**符合OpenAI Gym接口标准**，便于后续扩展其他强化学习算法。

---

### 🔧 工具类文件

#### `android_tool.py` - Android设备控制工具
**作用**: 通过ADB控制手机/模拟器执行游戏操作

**主要功能**:
1. **设备管理**:
   - `get_device_resolution()`: 获取设备分辨率
   - `show_scrcpy()`: 启动scrcpy投屏

2. **动作执行**:
   - `action_move(params)`: 控制移动摇杆
   - `action_attack(params)`: 执行攻击/技能操作
   - `action_info(params)`: 执行信息操作（购买装备等）

3. **图像获取**:
   - `screenshot_window()`: 截取scrcpy窗口画面
   - `take_screenshot()`: 通过ADB截图

**坐标计算**:
- 使用百分比坐标系统，适配不同分辨率
- `calculate_startpoint()`: 计算操作起始点
- `calculate_endpoint()`: 根据角度和半径计算终点

---

#### `getReword.py` - 奖励计算工具
**作用**: 根据游戏画面计算奖励值

**奖励机制**:
1. **攻击奖励**: 检测血条颜色，计算攻击效果（+1 ~ +10）
2. **死亡惩罚**: 检测死亡界面（-5）
3. **胜利奖励**: OCR识别"胜利"文字（+10000）
4. **失败惩罚**: OCR识别"失败"文字（-10000）
5. **无效动作**: 不合理操作（-1）

**检测方法**:
- **颜色检测**: HSV颜色空间检测血条
- **ONNX检测**: 使用 `death.onnx` 检测死亡状态
- **OCR识别**: 识别胜负文字

---

#### `globalInfo.py` - 全局状态管理
**作用**: 单例模式管理全局状态和经验池

**主要功能**:
1. **游戏状态管理**:
   - `set_game_start()`: 标记对局开始
   - `is_start_game()`: 查询对局状态
   - `set_game_end()`: 标记对局结束

2. **经验池管理**:
   - `store_transition_dqn()`: 存储经验
   - `is_memory_bigger_batch_size_dqn()`: 检查经验池大小
   - `random_batch_size_memory_dqn()`: 随机采样

**支持多种算法**: DQN、PPO、TD3（预留接口）

---

#### `argparses.py` - 参数配置
**作用**: 集中管理所有配置参数

**关键配置**:
```python
# 设备配置
--iphone_id          # 设备ID（真机/模拟器）
--real_iphone        # 是否真机（True/False）
--window_title       # scrcpy窗口标题

# 训练参数
--batch_size         # 批次大小（默认64）
--learning_rate      # 学习率（默认0.001）
--gamma              # 折扣因子（默认0.99）
--epsilon            # 探索率（默认1.0）
--epsilon_decay      # 探索率衰减（默认0.995）
--epsilon_min        # 最小探索率（默认0.01）
--memory_size        # 经验池大小（默认10000）

# 模型配置
--model_path         # 预训练模型路径
--num_episodes       # 训练轮数
--target_update      # 目标网络更新频率
```

**操作映射配置**:
- `move_actions_detail`: 移动摇杆位置和半径
- `info_actions_detail`: 信息按钮位置（购买装备、信号等）
- `attack_actions_detail`: 攻击按钮位置（技能、攻击、回城等）

---

### 🧠 模型文件

#### `net_actor.py` - 神经网络模型
**作用**: 定义DQN网络结构

**网络架构**:
```
输入: 3x640x640 RGB图像
  ↓
Conv2d(3→64, kernel=8, stride=4) + ReLU
  ↓
Conv2d(64→128, kernel=4, stride=2) + ReLU
  ↓
Flatten
  ↓
Linear(→256) + ReLU
  ↓
8个输出头:
  - fc_move: 2维（移动选择）
  - fc_angle: 360维（移动角度）
  - fc_info: 9维（信息操作）
  - fc_attack: 11维（攻击操作）
  - fc_action_type: 3维（动作类型）
  - fc_arg1: 360维（参数1）
  - fc_arg2: 100维（参数2）
  - fc_arg3: 5维（参数3）
```

**特点**:
- 使用卷积层提取图像特征
- 多头输出，每个动作维度独立预测
- Xavier初始化权重

---

#### `memory.py` - 经验回放缓冲区
**作用**: 存储和采样训练数据

**核心类**:
- `Transition`: 命名元组，存储 `(state, action, reward, next_state, done)`
- `ReplayMemory`: 循环缓冲区，支持随机采样

**优势**:
- 打破数据相关性
- 提高样本利用率
- 支持off-policy学习

---

#### `onnxRunner.py` - ONNX模型推理器
**作用**: 运行ONNX格式的检测模型

**主要方法**:
- `run(image)`: 执行推理
- `get_max_label(image)`: 获取置信度最高的类别
- `draw_detections()`: 在图像上绘制检测结果

**使用模型**:
- `models/start.onnx`: 检测对局是否开始
- `models/death.onnx`: 检测角色是否死亡

---

#### `showposition.py` - 坐标查看工具
**作用**: GUI工具，用于获取屏幕坐标百分比

**功能**:
1. 连接设备并截图
2. 点击图片获取坐标
3. 显示百分比坐标 `(x_percent, y_percent)`

**使用场景**: 调整 `argparses.py` 中的按钮位置配置

**运行方式**:
```bash
python showposition.py
```

---

### 📁 目录结构

```
wzry_ai-main/
├── train.py              # 训练主程序
├── dqnAgent.py           # DQN智能体
├── wzry_env.py           # 游戏环境
├── android_tool.py       # 设备控制工具
├── getReword.py          # 奖励计算
├── globalInfo.py         # 全局状态管理
├── argparses.py          # 参数配置
├── net_actor.py          # 神经网络模型
├── memory.py             # 经验回放
├── onnxRunner.py         # ONNX推理器
├── showposition.py       # 坐标查看工具
├── requirements.txt      # Python依赖
├── README.md             # 项目说明
│
├── models/               # ONNX模型目录
│   ├── start.onnx        # 对局开始检测模型
│   └── death.onnx        # 死亡检测模型
│
├── src/                  # 训练输出目录
│   └── wzry_ai.pt        # 训练生成的模型
│
├── doc/                  # 文档目录
│   ├── 说明文档.md       # 详细安装教程
│   └── requirements.txt  # 依赖文件
│
└── scrcpy-win64-v2.0/    # scrcpy工具目录
    ├── adb.exe           # ADB工具
    └── scrcpy.exe        # 投屏工具
```

---

## 环境配置

### 1. 安装Anaconda
下载地址: https://www.anaconda.com/download

### 2. 创建Python环境
```bash
conda create --name wzry_ai python=3.10
conda activate wzry_ai
```

### 3. 安装依赖包
```bash
# 安装基础依赖
pip install -r requirements.txt

# 安装PyTorch (CUDA 11.8)
pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

# 安装ONNX Runtime GPU
# CUDA 11
pip install onnxruntime-gpu

# CUDA 12
pip install onnxruntime-gpu --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
```

### 4. 解决zlibwapi.dll问题
如果运行时提示缺少 `zlibwapi.dll`:
```
复制: C:\Program Files\NVIDIA Corporation\Nsight Systems 2022.4.2\host-windows-x64\zlib.dll
到: C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v11.8\bin\zlibwapi.dll
```

---

## 实验步骤

### 准备工作

#### 1. 下载ONNX模型
从以下地址下载模型文件，放入 `models/` 目录:
- 项目网站: https://stack-traceable.top/archives/wzry-ai-model
- QQ群: 687853827(已满), 369509470, 566501058

#### 2. 连接设备

**真机连接**:
1. 开启手机开发者模式和USB调试
2. 用USB连接电脑
3. 运行 `scrcpy-win64-v2.0/adb devices` 获取设备ID
4. 修改 `argparses.py`:
```python
parser.add_argument('--iphone_id', type=str, default='你的设备ID', help="iphone_id")
parser.add_argument('--real_iphone', type=bool, default=True, help="real_iphone")
```

**模拟器连接** (推荐MuMu模拟器):
1. 安装MuMu模拟器，设置分辨率为 2400x1080
2. 开启ROOT权限
3. 运行命令:
```bash
cd scrcpy-win64-v2.0
adb kill-server
adb start-server
adb connect 127.0.0.1:7555
adb devices
```
4. 修改 `argparses.py`:
```python
parser.add_argument('--iphone_id', type=str, default='127.0.0.1:7555', help="iphone_id")
parser.add_argument('--real_iphone', type=bool, default=False, help="real_iphone")
```

#### 3. 调整按键映射（可选）
如果按键位置不准确:
1. 运行 `python showposition.py`
2. 截取游戏画面
3. 点击需要调整的按钮位置
4. 将显示的百分比坐标填入 `argparses.py` 对应位置

---

### 开始训练

#### 步骤1: 启动投屏
```bash
python train.py
```
程序会自动启动scrcpy投屏窗口

#### 步骤2: 开始游戏
1. 在手机/模拟器上打开王者荣耀
2. 选择英雄并进入对局
3. AI会自动检测对局开始并接管控制

#### 步骤3: 观察训练
- 控制台会输出训练日志
- 每隔 `num_episodes` 轮保存一次模型
- 模型保存在 `src/wzry_ai.pt`

#### 步骤4: 加载预训练模型继续训练
```python
# 修改 argparses.py
parser.add_argument('--model_path', type=str, default="src/wzry_ai.pt", help="Path to the model to load")
```

---

### 实验建议

#### 新手实验
1. **观察阶段**: 先运行程序观察AI行为，不进行训练
2. **小规模训练**: 设置较小的 `batch_size` 和 `num_episodes`
3. **逐步调优**: 根据训练效果调整奖励函数和网络结构

#### 进阶实验
1. **超参数调优**:
   - 调整学习率、探索率、折扣因子
   - 尝试不同的网络结构
   
2. **奖励函数优化**:
   - 修改 `getReword.py` 中的奖励计算逻辑
   - 添加更多奖励信号（击杀、助攻、金币等）

3. **算法扩展**:
   - 尝试Double DQN、Dueling DQN
   - 实现PPO、A3C等算法（框架已预留接口）

---

## 参数配置说明

### 关键参数详解

| 参数 | 默认值 | 说明 | 调优建议 |
|------|--------|------|----------|
| `batch_size` | 64 | 训练批次大小 | 根据GPU内存调整，越大越稳定 |
| `learning_rate` | 0.001 | 学习率 | 训练不稳定时可降低至0.0001 |
| `gamma` | 0.99 | 折扣因子 | 接近1重视长期奖励，接近0重视即时奖励 |
| `epsilon` | 1.0 | 初始探索率 | 建议从1.0开始，让AI充分探索 |
| `epsilon_decay` | 0.995 | 探索率衰减 | 控制探索到利用的过渡速度 |
| `epsilon_min` | 0.01 | 最小探索率 | 保留一定探索能力 |
| `memory_size` | 10000 | 经验池大小 | 越大越稳定，但占用更多内存 |
| `target_update` | 10 | 目标网络更新频率 | 定期同步策略网络到目标网络 |

### 操作映射配置

**移动摇杆**:
```python
move_actions_detail = {
    1: {'action_name': '移动', 'position': (0.164, 0.798), 'radius': 200}
}
```
- `position`: 摇杆中心位置（屏幕百分比）
- `radius`: 滑动半径（像素）

**信息按钮**:
```python
info_actions_detail = {
    1: {'action_name': '购买装备1', 'position': (0.133, 0.4)},
    2: {'action_name': '购买装备2', 'position': (0.133, 0.51)},
    3: {'action_name': '发起进攻', 'position': (0.926, 0.14)},
    # ...
}
```

**攻击按钮**:
```python
attack_actions_detail = {
    1: {'action_name': '攻击', 'position': (0.85, 0.85)},
    7: {'action_name': '召唤师技能', 'position': (0.64, 0.9), 'radius': 50},
    8: {'action_name': '1技能', 'position': (0.71, 0.874), 'radius': 100},
    # ...
}
```

---

## 常见问题

### 1. 环境安装问题

**问题**: pip安装速度慢
**解决**: 使用国内镜像源
```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**问题**: CUDA版本不匹配
**解决**: 检查CUDA版本
```bash
nvidia-smi  # 查看CUDA版本
```
选择对应的PyTorch和ONNX Runtime版本

---

### 2. 设备连接问题

**问题**: `adb devices` 找不到设备
**解决**:
```bash
adb kill-server
adb start-server
adb devices
```

**问题**: 模拟器连接失败
**解决**:
- 确认模拟器已开启USB调试
- 确认端口号正确（MuMu默认7555，夜神62001，雷电5555）
- 尝试重启模拟器

---

### 3. 训练问题

**问题**: 程序无法检测对局开始
**解决**:
- 确认 `models/start.onnx` 存在
- 检查游戏画面是否清晰
- 调整ONNX模型的置信度阈值

**问题**: AI操作不准确
**解决**:
- 使用 `showposition.py` 重新校准按键位置
- 确认设备分辨率与配置一致
- 检查scrcpy窗口标题是否正确

**问题**: 训练loss不下降
**解决**:
- 降低学习率
- 增大batch_size
- 检查奖励函数是否合理
- 确认经验池数据质量

---

### 4. 性能优化

**提高帧率**:
- 降低截图分辨率
- 使用GPU加速
- 优化ONNX模型

**提高训练速度**:
- 增大batch_size
- 使用多线程数据收集
- 减少不必要的奖励计算

---

## 联系方式

- **项目地址**: https://github.com/myBoris/wzry_ai
- **博客**: https://stack-traceable.top/
- **QQ群1**: 687853827 (已满)
- **QQ群2**: 369509470
- **QQ群3**: 566501058
- **视频教程**: https://www.bilibili.com/video/BV1ZXYuePEUG/

---

## 许可声明

本项目仅用于人工智能学习和研究，**严禁用于任何商业用途或游戏作弊行为**。使用本项目所产生的一切后果由使用者自行承担。

---

**开源不易，共同努力！** ⭐ 如果这个项目对你有帮助，欢迎Star支持！
