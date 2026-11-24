# 参与贡献

感谢你有兴趣改进这个项目。提交前请先读完本文。

## 1. 环境

```bat
conda create -n wzry_ai python=3.10 -y
conda activate wzry_ai
pip install -r requirements.txt
```

显存/结构相关的改动，建议同时跑一遍 `python bench_vram.py --iters 12`，把前后数字写进 PR 描述。

## 2. 提交前必做

```bat
:: 语法检查（不需要设备；compileall 在 cmd / PowerShell 下都能用）
python -m compileall -q .

:: 离线自检：不连手机、不需要显卡，应 100 项全过
python test_assist_pipeline.py --device cpu

:: 文档链接与格式检查
python tools/check_docs.py
```

## 3. 代码约定

- Python 3.10，不引入新的重依赖；新增依赖请同步 `requirements.txt` 与 `NOTICE.md`。
- 所有可调行为都要有命令行参数，默认值在 `argparses.py` 里集中声明，并在 `docs/` 里说明。
- 涉及设备交互的代码要能**在没有设备时优雅降级**（打印原因、不要抛异常退出）。
- 训练相关的改动要保证 `--episode_end_enabled true`、`--td_target_clip > 0` 时不会让 Q 值发散。
- 注释与文档用中文，变量名用英文。

## 4. 文档约定

- 只保留一套：`README.md` 是入口，`docs/` 下按主题分（quickstart / human-assist / architecture / vram / troubleshooting）。
- 不要在多个文档里重复同一段说明，用链接指过去。
- 上游历史文档只放 `docs/legacy/`，不要加新内容。
- 改完跑 `python tools/check_docs.py`，确保相对链接都能打开。

## 5. 提交信息

```text
<类型>: <一句话说明>

类型：feat / fix / docs / perf / refactor / test / chore
```

例如：`fix: 触摸还原跟随 --move_joystick_center_*`

## 6. 报告问题

请附上：

1. 运行命令（完整命令行）；
2. 设备/模拟器型号与 `adb shell wm size` 结果；
3. 关键日志（`[net]` / `[startup]` / `step=` 前几行 / 报错栈）；
4. `python test_assist_pipeline.py --device cpu` 的结果。

## 7. 不要提交

`models/*.onnx`、`scrcpy-win64-v2.0/`、`*.pt`、`*.jsonl`、`human_data/`、`__pycache__/` 已在
`.gitignore` 中排除，请勿强行加入。
