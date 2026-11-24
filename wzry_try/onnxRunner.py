"""ONNX 检测模型封装。

原版直接 `ort.InferenceSession(path, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])`，
有两个隐患：

1. CUDA 会话默认会按需扩张显存 arena，两个 117MB 的检测模型叠加上策略网络，
   在 8GB 显卡上很容易把显存挤爆；
2. 没有设置 `arena_extend_strategy=kSameAsRequested` 时，arena 碎片会持续增长。

这里加了：

* `--onnx_gpu_mem_limit_mb`：给每个 CUDA 会话一个显存上限；
* `--onnx_intra_op_threads`：限制 CPU 线程数，避免和采集线程抢核；
* `--onnx_provider auto|cpu|cuda`：可以整体把检测模型赶到 CPU 上，把显存全留给训练；
* 模型路径通过 `asset_paths` 解析，可用 `--asset_root` 把权重放到项目外的目录。
"""

import cv2
import numpy as np
import onnxruntime as ort

from argparses import args
from asset_paths import resolve_asset_path


def _wanted_providers():
    requested = str(getattr(args, "onnx_provider", "auto") or "auto").lower()
    available = set(ort.get_available_providers())
    cuda_ok = "CUDAExecutionProvider" in available

    if requested == "cpu":
        return ["CPUExecutionProvider"]
    if requested == "cuda":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"] if cuda_ok else ["CPUExecutionProvider"]
    if cuda_ok:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def build_session(model_path, gpu_mem_limit_mb=None):
    options = ort.SessionOptions()
    try:
        options.intra_op_num_threads = max(1, int(getattr(args, "onnx_intra_op_threads", 2) or 2))
        options.inter_op_num_threads = 1
        options.enable_mem_pattern = True
    except (AttributeError, TypeError):
        pass

    providers = _wanted_providers()
    provider_options = []
    if providers and providers[0] == "CUDAExecutionProvider":
        limit_mb = gpu_mem_limit_mb
        if limit_mb is None:
            limit_mb = int(getattr(args, "onnx_gpu_mem_limit_mb", 512) or 512)
        limit_mb = max(128, int(limit_mb))
        provider_options = [
            {
                "device_id": 0,
                "arena_extend_strategy": "kSameAsRequested",
                "gpu_mem_limit": limit_mb * 1024 * 1024,
                "cudnn_conv_algo_search": "HEURISTIC",
                "do_copy_in_default_stream": 1,
            },
            {},
        ]

    resolved = resolve_asset_path(model_path, description="ONNX 模型")
    return ort.InferenceSession(resolved, sess_options=options, providers=providers, provider_options=provider_options or None)


class OnnxRunner:
    def __init__(self, model_path, input_width=640, input_height=640, confidence_thres=0.5, iou_thres=0.4, classes=None,
                 lazy=False, gpu_mem_limit_mb=None):
        """
        :param model_path: ONNX 模型文件的路径
        :param input_width: 模型输入的宽度
        :param input_height: 模型输入的高度
        :param confidence_thres: 过滤检测结果的置信度阈值
        :param iou_thres: 非极大值抑制的 IOU 阈值
        :param classes: 类别名称列表
        :param lazy: True 时延迟到第一次推理才真正创建会话，避免白占显存
        """
        self.img_height = 0
        self.img_width = 0
        self.model_path = model_path
        self.input_width = input_width
        self.input_height = input_height
        self.confidence_thres = confidence_thres
        self.iou_thres = iou_thres
        self.classes = list(classes or [])
        self.gpu_mem_limit_mb = gpu_mem_limit_mb
        self.session = None
        self._input_name = None
        self.last_error = ""
        if not lazy:
            self._ensure_session()

    # -- 会话管理 -----------------------------------------------------------
    def _ensure_session(self):
        if self.session is not None:
            return True
        try:
            self.session = build_session(self.model_path, self.gpu_mem_limit_mb)
        except Exception as exc:  # onnxruntime 的异常类型不统一
            self.last_error = str(exc)
            print(f"[onnx] 会话创建失败 ({self.model_path}): {exc}")
            self.session = None
            return False
        try:
            self._input_name = self.session.get_inputs()[0].name
        except (IndexError, AttributeError):
            self._input_name = None
        self._warmup()
        return True

    def _warmup(self):
        try:
            dummy = np.zeros((1, 3, self.input_height, self.input_width), dtype=np.float32)
            self.session.run(None, {self._input_name: dummy})
        except Exception:
            pass

    @property
    def providers(self):
        if self.session is None:
            return []
        try:
            return list(self.session.get_providers())
        except AttributeError:
            return []

    def release(self):
        """显式释放会话，把显存让给训练。"""
        self.session = None
        self._input_name = None

    # -- 推理 ---------------------------------------------------------------
    def preprocess(self, image):
        """
        预处理输入图像

        :param image: OpenCV 读取的图像
        :return: 预处理后的张量
        """
        self.img_height, self.img_width = image.shape[:2]
        image_resized = cv2.resize(image, (self.input_width, self.input_height))
        image_rgb = cv2.cvtColor(image_resized, cv2.COLOR_BGR2RGB)
        image_normalized = image_rgb.astype(np.float32) / 255.0
        image_transposed = np.transpose(image_normalized, (2, 0, 1))  # CHW
        return np.expand_dims(image_transposed, axis=0)  # NCHW

    def postprocess(self, outputs):
        """
        后处理模型输出

        :param outputs: 模型输出
        :return: 过滤后的检测框
        """
        outputs = np.squeeze(outputs[0])
        rows = outputs.shape[0]
        detections = []

        x_factor = self.img_width / self.input_width
        y_factor = self.img_height / self.input_height

        for i in range(rows):
            classes_scores = round(float(outputs[i][4]), 4)
            if classes_scores < self.confidence_thres:
                continue

            class_id = int(outputs[i][5])
            x1, y1, x2, y2 = outputs[i][0], outputs[i][1], outputs[i][2], outputs[i][3]
            detections.append({
                "class_id": class_id,
                "class_name": self.classes[class_id] if class_id < len(self.classes) else str(class_id),
                "score": classes_scores,
                "box": [
                    int(x1 * x_factor),
                    int(y1 * y_factor),
                    int(x2 * x_factor),
                    int(y2 * y_factor),
                ],
            })
        return detections

    def run(self, image):
        if not self._ensure_session():
            return []
        input_data = self.preprocess(image)
        outputs = self.session.run(None, {self._input_name: input_data})
        return self.postprocess(outputs)

    def get_max_label(self, image):
        """运行推理并返回置信度最高的类别名。"""
        detections = self.run(image)

        max_score = 0.0
        result = None
        for det in detections:
            score = float(det["score"])
            if score > max_score:
                max_score = score
                result = det["class_name"]
        return result

    def draw_detections(self, image, detections):
        for det in detections:
            left, top, right, bottom = det["box"]
            cv2.rectangle(image, (left, top), (right, bottom), (0, 255, 0), 2)
            label = f'{det["class_name"]}: {det["score"]:.2f}'
            cv2.putText(image, label, (left, top - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        return image
