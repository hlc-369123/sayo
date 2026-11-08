"""设置窗口：快捷键、识别参数、润色规则、个人词典、权限、离线状态。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import (
    APP_NAME, APP_VERSION, DEFAULT_SETTINGS, PERMISSION_HELP, models_dir, sensitivity_preset,
)
from ..core import accel
from ..core.asr import ASREngine
from ..platform import permissions
from .themes import DEFAULT_THEME, get_theme, theme_items, ui_stylesheet

POST_LABELS = [
    ("remove_fillers", "去除填充词（嗯/呃/um/you know）"),
    ("remove_repetitions", "去除重复与口吃"),
    ("self_correction", "识别中途改口，只保留最终意图"),
    ("auto_format", "口述列表 / 步骤自动排版"),
    ("punctuation", "口述标点还原与标点规范化"),
    ("normalize_spacing", "中英混排空格规范化"),
    ("personal_dictionary", "个人词典纠错与热词偏置"),
]


class SettingsWindow(QWidget):
    saved = Signal(dict)
    theme_previewed = Signal(str)  # 下拉框切换时实时预览主题（浮层 + 本窗口）
    overlay_style_previewed = Signal(str)  # 浮层样式切换时实时预览（classic/pill）

    def __init__(self, settings, storage, pipeline=None):
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.pipeline = pipeline
        self.setWindowTitle(f"{APP_NAME} · 设置")
        self.setMinimumWidth(620)
        self.resize(660, 560)
        self.setStyleSheet(ui_stylesheet(get_theme(settings.get("theme", DEFAULT_THEME))))
        self.tabs = QTabWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self._build_general()
        self._build_asr()
        self._build_polish()
        self._build_dictionary()
        self._build_privacy()
        self._build_about()

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("关闭")
        btn_save = QPushButton("保存并应用")
        btn_save.setDefault(True)
        btn_cancel.clicked.connect(self.close)
        btn_save.clicked.connect(self._save)
        row.addWidget(btn_cancel)
        row.addWidget(btn_save)
        lay.addLayout(row)

    # ---------------------------------------------------------------- 通用
    @staticmethod
    def _tune_form(f: QFormLayout) -> None:
        """统一表单对齐：标签与控件垂直居中，行距一致。

        默认 QFormLayout 的标签是 AlignTop，遇到比标签高的控件（下拉框、
        换行文本）时文字会明显偏上，看起来"没在两行中间"。
        """
        # 标签左对齐：与卡片标题、复选框统一左边缘，不再挤向右边的控件列
        f.setLabelAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        # 表单整体靠上（不要垂直居中，否则页面顶部会出现一大片空白）；
        # 单行的垂直居中只由 setLabelAlignment 负责。
        f.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        f.setVerticalSpacing(12)
        f.setHorizontalSpacing(14)

    @classmethod
    def _form(cls) -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(18, 18, 18, 18)
        f.setSpacing(10)
        cls._tune_form(f)
        return w, f

    @staticmethod
    def _scrollable(content: QWidget) -> QWidget:
        """内容页放进滚动区：窗口拉小也不会把下面的设置直接裁掉。

        个人词典页有自带滚动的表格，不套这层，避免双层滚动条。
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("tabScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        return page

    @staticmethod
    def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
        """卡片：标题作为卡片内独立一行，垂直居中、与内容严格左对齐。

        替代 QGroupBox::title（浮在边框线上、贴左上角，视觉上明显偏上偏左）。
        """
        frame = QFrame()
        frame.setObjectName("card")
        v = QVBoxLayout(frame)
        v.setContentsMargins(16, 13, 16, 14)
        v.setSpacing(9)
        head = QLabel(title)
        head.setObjectName("cardTitle")
        head.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        head.setMinimumHeight(20)
        v.addWidget(head)
        return frame, v

    def _build_general(self):
        w, f = self._form()
        s = self.settings.data
        self.ed_hotkey = QLineEdit(s["hotkey_dictate"])
        self.ed_hotkey_ask = QLineEdit(s["hotkey_ask"])
        self.ck_ptt = QCheckBox("按住快捷键说话，松开即结束")
        self.ck_ptt.setChecked(bool(s["push_to_talk"]))
        self.cb_inject = QComboBox()
        self.cb_inject.addItems(["paste", "clipboard_only", "type"])
        self.cb_inject.setCurrentText(s["inject_method"])
        self.ck_restore = QCheckBox("粘贴后恢复原剪贴板内容")
        self.ck_restore.setChecked(bool(s["restore_clipboard"]))
        self.ck_offline = QCheckBox("强制离线锁（禁止一切 IP 连接）")
        self.ck_offline.setChecked(bool(s["offline_lock"]))
        self.ck_offline.setEnabled(False)  # 安全项，安装后不可关闭
        self.ck_login = QCheckBox("开机自动启动")
        self.ck_login.setChecked(bool(s["launch_at_login"]))
        self.ck_smart = QCheckBox("智能输出：有输入框才粘贴，没有输入框只放进剪贴板")
        self.ck_smart.setChecked(bool(s.get("smart_output", True)))
        self.ck_live = QCheckBox("经典样式：录音时显示文字 / 声音曲线两窗")
        self.ck_live.setChecked(bool(s.get("live_preview", True)))
        self.ck_live.setToolTip(
            "只影响「经典（两窗）」样式。胶囊样式中间始终显示实时识别文字，不受此项影响")
        self.cb_unknown = QComboBox()
        self.cb_unknown.addItems(["clipboard", "paste"])
        self.cb_unknown.setCurrentText(s.get("smart_output_when_unknown", "clipboard"))
        self.cb_unknown.setToolTip(
            "微信 / Electron 等应用可能不暴露焦点控件。clipboard=保守只复制；paste=按老方式自动粘贴"
        )
        self.cb_theme = QComboBox()
        for key, name in theme_items():
            self.cb_theme.addItem(name, key)
        cur = get_theme(s.get("theme", DEFAULT_THEME))
        idx = self.cb_theme.findData(cur.key)
        self.cb_theme.setCurrentIndex(max(0, idx))
        self.cb_theme.currentIndexChanged.connect(self._preview_theme)
        f.addRow("听写快捷键", self.ed_hotkey)
        f.addRow("Ask / 编辑快捷键", self.ed_hotkey_ask)
        hint_left = QLabel("格式示例：cmd+shift+space、cmd+shift+e、ctrl+alt+d")
        hint_right = QLabel("系统级热键，无需输入监控授权；保存后立即重新注册")
        hint_left.setWordWrap(True)
        hint_right.setWordWrap(True)
        hint_left.setAlignment(Qt.AlignVCenter)
        hint_right.setAlignment(Qt.AlignVCenter)
        f.addRow(hint_left, hint_right)
        f.addRow(self.ck_ptt)
        f.addRow("文字输出方式", self.cb_inject)
        f.addRow(self.ck_restore)
        f.addRow(self.ck_offline)
        f.addRow(self.ck_login)
        f.addRow(self.ck_smart)
        f.addRow("焦点无法判断时", self.cb_unknown)
        f.addRow(self.ck_live)
        f.addRow("界面主题", self.cb_theme)
        # 浮层样式：下拉用 data 存机器值（classic/pill），显示文案与值分离。
        # 旧 bug：_save() 曾用 currentText() 把中文文案存进配置，启动时
        # RecordingOverlay 拿到非 "pill" 值回退成经典两窗——界面显示胶囊、
        # 实际跑旧样式。这里同时兼容历史脏值。
        self.cb_overlay_style = QComboBox()
        self.cb_overlay_style.addItem("经典（文字 / 曲线两窗）", "classic")
        self.cb_overlay_style.addItem("胶囊（单条椭圆）", "pill")
        cur_style = str(s.get("overlay_style", "pill"))
        if "胶囊" in cur_style or cur_style == "pill":
            idx = 1
        elif "经典" in cur_style or cur_style == "classic":
            idx = 0
        else:
            idx = 1  # 未知历史值按默认胶囊处理
        self.cb_overlay_style.setCurrentIndex(idx)
        self.cb_overlay_style.setToolTip(
            "胶囊：左 × / logo / 文字 / 分隔 / 麦克风 / 波形 / 右 ✓ 都在一个椭圆框内，"
            "× 取消（丢弃），✓ 确认（提交）。切换立即生效，无需保存")
        self.cb_overlay_style.currentIndexChanged.connect(self._apply_overlay_style)
        f.addRow("浮层样式", self.cb_overlay_style)
        self.ck_sound = QCheckBox("交互声音：开始 / 结束听写的提示音")
        self.ck_sound.setChecked(bool(s.get("interaction_sound", True)))
        f.addRow(self.ck_sound)
        self.ck_mute_others = QCheckBox("语音输入时静音其他活动音频")
        self.ck_mute_others.setChecked(bool(s.get("mute_others_during_input", False)))
        self.ck_mute_others.setToolTip(
            "开始听写时临时静音系统输出，结束后自动恢复原状态")
        f.addRow(self.ck_mute_others)
        self.tabs.addTab(self._scrollable(w), "常规")

    def _apply_overlay_style(self):
        """浮层样式切换立即生效并落盘（不依赖点「保存并应用」）。"""
        val = self.cb_overlay_style.currentData() or "pill"
        try:
            self.settings.set("overlay_style", val)
        except Exception:
            pass
        self.overlay_style_previewed.emit(val)

    def _preview_theme(self):
        key = self.cb_theme.currentData() or DEFAULT_THEME
        self.setStyleSheet(ui_stylesheet(get_theme(key)))
        self.theme_previewed.emit(key)

    def _build_asr(self):
        w, f = self._form()
        s = self.settings.data
        self.cb_model = QComboBox()
        self.cb_model.addItems(["sensevoice", "whisper"])
        self.cb_model.setCurrentText(s["model"])
        self.cb_lang = QComboBox()
        self.cb_lang.addItems(["auto", "zh", "en", "yue", "ja", "ko"])
        self.cb_lang.setCurrentText(s["language"])
        self.cb_accel = QComboBox()
        self.cb_accel.addItems(["auto", "cpu", "gpu"])
        self.cb_accel.setCurrentText(s.get("accelerator", "auto"))
        self.cb_accel.setToolTip("auto = 按本机实测智能调度；gpu = 强制 Core ML / CUDA，失败自动回退 CPU")
        self.cb_precision = QComboBox()
        self.cb_precision.addItems(["fp32", "int8", "auto"])
        self.cb_precision.setCurrentText(s.get("precision", "fp32"))
        self.cb_precision.setToolTip("fp32 全精度：轻声、长句、噪声下更稳；int8 量化：体积更小、冷启动更快")
        self.cb_sensitivity = QComboBox()
        self.cb_sensitivity.addItems(["standard", "high", "max"])
        self.cb_sensitivity.setCurrentText(s.get("sensitivity", "high"))
        self._initial_sensitivity = self.cb_sensitivity.currentText()
        self.ck_recheck = QCheckBox("低电平片段二次复核（用额外算力换取轻声不丢字）")
        self.ck_recheck.setChecked(bool(s.get("gpu_recheck", True)))
        self.lb_accel = QLabel("\n".join(accel.status_lines()))
        self.lb_accel.setWordWrap(True)
        self.lb_accel.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # 绝不能用 MinimumExpanding：窗口拉高时它会吃掉几百像素空白，
        # 把「本机加速状态」的标签和值拉开半屏（与关于页同一个 bug）。
        # Preferred 让行高只由文字内容决定。
        self.lb_accel.setMinimumWidth(400)
        self.lb_accel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(0, 16)
        self.sp_threads.setValue(int(s["num_threads"]))
        self.sp_threads.setToolTip("0 = 自动（最多 4 线程）")
        self.ck_vad = QCheckBox("启用 Silero VAD 端点检测（推荐）")
        self.ck_vad.setChecked(bool(s["vad_enabled"]))
        self.sl_threshold = QSlider()
        self.sl_threshold.setOrientation(Qt.Horizontal)
        self.sl_threshold.setRange(10, 80)
        self.sl_threshold.setValue(int(float(s["vad_threshold"]) * 100))
        self.lb_threshold = QLabel(f"{s['vad_threshold']:.2f}（越小越灵敏）")
        self.sl_threshold.valueChanged.connect(lambda v: self.lb_threshold.setText(f"{v/100:.2f}（越小越灵敏）"))
        self.sp_silence = QDoubleSpinBox()
        self.sp_silence.setRange(0.1, 3.0)
        self.sp_silence.setSingleStep(0.05)
        self.sp_silence.setValue(float(s["vad_min_silence"]))
        self.sp_autostop = QDoubleSpinBox()
        self.sp_autostop.setRange(0, 10)
        self.sp_autostop.setSingleStep(0.2)
        self.sp_autostop.setValue(float(s["auto_stop_silence_s"]))
        self.ck_agc = QCheckBox("低音量自动增益（提升内置麦克风灵敏度）")
        self.ck_agc.setChecked(bool(s["agc_enabled"]))
        self.sp_max = QSpinBox()
        self.sp_max.setRange(10, 1800)
        self.sp_max.setValue(int(s["max_record_seconds"]))
        f.addRow("识别模型", self.cb_model)
        f.addRow("识别语言", self.cb_lang)
        f.addRow("推理后端（GPU）", self.cb_accel)
        f.addRow("权重精度", self.cb_precision)
        f.addRow("灵敏度档位", self.cb_sensitivity)
        f.addRow(self.ck_recheck)
        f.addRow("本机加速状态", self.lb_accel)
        f.addRow("推理线程数", self.sp_threads)
        f.addRow(self.ck_vad)
        # VAD 阈值：滑块与数值并排，避免数值掉到下一行
        vad_row = QWidget()
        vad_h = QHBoxLayout(vad_row)
        vad_h.setContentsMargins(0, 0, 0, 0)
        vad_h.addWidget(self.sl_threshold)
        vad_h.addWidget(self.lb_threshold)
        vad_h.addStretch(1)
        f.addRow("VAD 灵敏度阈值", vad_row)
        f.addRow("静音判定（秒）", self.sp_silence)
        f.addRow("说完自动结束（秒，0=关闭）", self.sp_autostop)
        f.addRow(self.ck_agc)
        f.addRow("单次最长录音（秒）", self.sp_max)

        # 输入麦克风：即选即用（写入配置，下次听写生效）；失败自动回退系统默认
        self.cb_input_device = QComboBox()
        self.cb_input_device.addItem("跟随系统默认（自动检测）", None)
        self._reload_input_devices()
        self.cb_input_device.currentIndexChanged.connect(self._apply_input_device)
        btn_refresh_dev = QPushButton("刷新")
        btn_refresh_dev.setToolTip("重新扫描本机输入设备（插入/拔出麦克风后点此）")
        btn_refresh_dev.clicked.connect(self._reload_input_devices)
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.cb_input_device, 1)
        mic_row.addWidget(btn_refresh_dev)
        mic_widget = QWidget()
        mic_widget.setLayout(mic_row)
        f.addRow("输入麦克风", mic_widget)

        buttons = QHBoxLayout()
        btn_devices = QPushButton("选择输入麦克风…")
        btn_devices.clicked.connect(self._show_devices)
        buttons.addWidget(btn_devices)
        buttons.addStretch(1)
        f.addRow("", buttons)
        self.tabs.addTab(self._scrollable(w), "识别")

    def _reload_input_devices(self):
        """重填输入设备下拉：第一项固定「跟随系统默认」，其余为真实输入设备。"""
        try:
            cur = self.cb_input_device.currentData()
        except Exception:
            cur = self.settings.get("input_device")
        self.cb_input_device.blockSignals(True)
        self.cb_input_device.clear()
        self.cb_input_device.addItem("跟随系统默认（自动检测）", None)
        try:
            from ..core.recorder import Recorder
            devs = Recorder.devices()
        except Exception:
            devs = []
        for d in devs:
            self.cb_input_device.addItem(
                f"{d['name']}（{d['channels']} 声道）", int(d["index"]))
        idx = 0
        for i in range(self.cb_input_device.count()):
            if self.cb_input_device.itemData(i) == cur:
                idx = i
                break
        self.cb_input_device.setCurrentIndex(idx)
        self.cb_input_device.blockSignals(False)

    def _apply_input_device(self):
        """选择输入麦克风立即落盘（下次开始听写生效）。"""
        try:
            self.settings.set("input_device", self.cb_input_device.currentData())
        except Exception:
            pass

    def _build_polish(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 18, 18, 18)
        box, gl = self._card("本地智能润色（逐项可关，全部在本机执行）")
        self.post_checks = {}
        opts = self.settings.get("postprocess", {})
        for key, label in POST_LABELS:
            cb = QCheckBox(label)
            cb.setChecked(bool(opts.get(key, True)))
            self.post_checks[key] = cb
            gl.addWidget(cb)
        v.addWidget(box)

        box2, v2 = self._card("语气与模式")
        fl = QFormLayout()
        fl.setContentsMargins(0, 0, 0, 0)
        self._tune_form(fl)
        v2.addLayout(fl)
        self.cb_tone = QComboBox()
        self.cb_tone.addItems(["auto", "formal", "casual", "code", "none"])
        self.cb_tone.setCurrentText(self.settings.get("tone_mode", "auto"))
        self.ed_target = QLineEdit(self.settings.get("target_language", "en"))
        fl.addRow("按应用切换语气", self.cb_tone)
        fl.addRow("翻译模式目标语言", self.ed_target)
        v.addWidget(box2)

        box3, bl = self._card("智能整理与本地大模型（GGUF，完全本机推理）")
        # 全部用 QVBoxLayout 全宽纵排：QFormLayout 对「自动换行的多行标签」行高
        # 计算不准，会出现「字叠字」；复选框长文案单行也会被右缘裁掉。
        bl.setSpacing(8)
        intro = QLabel("整理只纠错、通顺、分点，内容全保留；总结在菜单栏「总结最近一次听写」单独触发")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignVCenter)
        intro.setStyleSheet("color:#8A94A8;")
        bl.addWidget(intro)
        # 复选框文案只留标题，长说明拆成独立换行标签
        self.ck_organize = QCheckBox("智能整理：修正错别字、理顺语句、自动分点")
        self.ck_organize.setChecked(bool(self.settings.get("organize_llm", True)))
        bl.addWidget(self.ck_organize)
        organize_hint = QLabel(
            "只去口头禅和改口，保留全部内容不做摘要删减；输出明显变短会自动重试或回退。")
        organize_hint.setWordWrap(True)
        organize_hint.setAlignment(Qt.AlignVCenter)
        organize_hint.setStyleSheet("color:#8A94A8; padding-left:26px;")
        bl.addWidget(organize_hint)
        status_title = QLabel("整理状态")
        status_title.setAlignment(Qt.AlignVCenter)
        status_title.setStyleSheet("color:#8A94A8;")
        bl.addWidget(status_title)
        self.lb_llm_status = QLabel()
        self.lb_llm_status.setWordWrap(True)
        self.lb_llm_status.setAlignment(Qt.AlignVCenter)
        bl.addWidget(self.lb_llm_status)
        self.ck_llm = QCheckBox("启用本地 LLM（深度改写 / 翻译 / 问答 / 一键总结）")
        self.ck_llm.setChecked(bool(self.settings.get("llm_enabled", False)))
        bl.addWidget(self.ck_llm)
        model_row = QHBoxLayout()
        model_label = QLabel("模型文件")
        model_label.setAlignment(Qt.AlignVCenter)
        model_row.addWidget(model_label)
        self.ed_llm_path = QLineEdit(self.settings.get("llm_model_path", ""))
        btn_browse = QPushButton("选择模型")
        btn_browse.clicked.connect(self._browse_llm)
        model_row.addWidget(self.ed_llm_path, 1)
        model_row.addWidget(btn_browse)
        bl.addLayout(model_row)
        v.addWidget(box3)
        v.addStretch(1)
        self.tabs.addTab(self._scrollable(w), "润色")
        self._refresh_llm_status()

    def _refresh_llm_status(self) -> None:
        """显示整理模型的真实就绪状态与放置路径提示。"""
        try:
            from ..core.llm import LocalTextEngine
            engine = (self.pipeline.llm if self.pipeline is not None
                      else LocalTextEngine(self.settings.data))
            self.lb_llm_status.setText(engine.status())
        except Exception as exc:  # 状态展示永不阻塞设置窗口
            self.lb_llm_status.setText(f"状态获取失败：{exc}")

    def _build_dictionary(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 18, 18, 18)
        dict_intro = QLabel("人名、品牌、专业术语：识别时做热词偏置，识别后做同音/近形纠错。")
        dict_intro.setWordWrap(True)
        dict_intro.setAlignment(Qt.AlignVCenter)
        v.addWidget(dict_intro)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["标准写法", "常见错写/别名（逗号分隔）", "替换为（可空）"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        v.addWidget(self.table)
        row = QHBoxLayout()
        self.ed_term = QLineEdit()
        self.ed_term.setPlaceholderText("标准写法，如：张伟 / sayo")
        self.ed_alias = QLineEdit()
        self.ed_alias.setPlaceholderText("常见误识别，如：张卫,章伟")
        btn_add = QPushButton("添加 / 更新")
        btn_add.clicked.connect(self._add_term)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._del_term)
        row.addWidget(self.ed_term, 2)
        row.addWidget(self.ed_alias, 2)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        v.addLayout(row)
        self._reload_terms()
        self.tabs.addTab(w, "个人词典")

    def _build_privacy(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 18, 18, 18)
        perm_intro = QLabel("权限状态（仅本机读取，不做任何联网校验）")
        perm_intro.setAlignment(Qt.AlignVCenter)
        v.addWidget(perm_intro)
        self.perm_labels = []
        for st in permissions.check():
            state = "已授权" if st.granted is True else ("未授权" if st.granted is False else "需人工确认")
            box, lay = self._card(f"{st.label} · {state}")
            for txt in (PERMISSION_HELP.get(st.key, st.hint), st.hint):
                lb = QLabel(txt)
                lb.setWordWrap(True)
                lb.setAlignment(Qt.AlignVCenter)
                lay.addWidget(lb)
            btn = QPushButton("打开系统设置")
            key = st.key
            btn.clicked.connect(lambda _, k=key: permissions.open_settings(k))
            lay.addWidget(btn)
            v.addWidget(box)
        self.sp_retention = QSpinBox()
        self.sp_retention.setRange(0, 3650)
        self.sp_retention.setValue(int(self.settings.get("history_retention_days", 365)))
        self.ck_save_audio = QCheckBox("在本机留存录音文件（默认关闭，仅保存在用户数据目录）")
        self.ck_save_audio.setChecked(bool(self.settings.get("save_audio", False)))
        btn_perm = QPushButton("一键重新申请系统权限（麦克风 / 辅助功能 / 输入监控）")
        btn_perm.clicked.connect(self._request_permissions)
        f = QFormLayout()
        self._tune_form(f)
        f.addRow("历史保留天数（0=永久）", self.sp_retention)
        v.addLayout(f)
        v.addWidget(self.ck_save_audio)
        v.addWidget(btn_perm)
        v.addStretch(1)
        self.tabs.addTab(self._scrollable(w), "权限与隐私")

    def _build_about(self):
        """关于页：滚动区 + 手工行布局。

        旧实现把换行 QLabel 放进 QFormLayout，还给了 MinimumExpanding 纵向
        策略：窗口一拉高，这三行就把几百像素的空白全吃掉，标签与值相隔
        半屏；其余多行说明被压到单行高度，字叠字、底部被裁。手工行 + 滚动
        区让每一行高度完全由内容决定，任何窗口尺寸下都不重叠、不裁切。
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("aboutScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        rows = QVBoxLayout(body)
        rows.setContentsMargins(20, 18, 20, 18)
        rows.setSpacing(16)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        def value_label(text: str) -> QLabel:
            lb = QLabel(text)
            lb.setWordWrap(True)
            lb.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            lb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            return lb

        def add_row(label: str, value) -> QLabel:
            row = QHBoxLayout()
            row.setSpacing(14)
            key = QLabel(label)
            key.setObjectName("aboutKey")
            key.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            key.setMinimumWidth(96)
            key.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            val = value if isinstance(value, QWidget) else value_label(str(value))
            row.addWidget(key)
            row.addWidget(val, 1)
            rows.addLayout(row)
            return val

        engine = ASREngine(models_dir(), self.settings.data)
        ok, msg = engine.check_models()
        from ..core.llm import LocalTextEngine

        add_row("版本", f"{APP_NAME} v{APP_VERSION}")
        add_row("运行方式", "完全离线 · 无账号 · 无更新检查 · 无遥测")
        self.lb_models = add_row("模型状态", "模型完整 ✓" if ok else "模型缺失 ✗  " + msg)
        self.lb_model_dir = add_row("模型目录", str(models_dir()))
        add_row("推理配置", accel.capability_summary(self.settings.data))
        add_row("实时浮层",
                "单条胶囊浮层：左 × 取消、中间实时识别文字、右侧随音量变化的曲线、"
                "右 ✓ 确认。可在桌面拖动、可拉伸宽度，位置与宽度重启后保持。")
        add_row("GPU 说明",
                "GPU 只提供算力；准确率来自 fp32 全精度与二次复核，灵敏度来自 VAD 档位。"
                "本机小模型短句实测 CPU 更快，可强制 GPU 对比。")
        self.lb_llm = add_row("本地 LLM", LocalTextEngine(self.settings.data).status())
        add_row("数据位置", str(self.settings.path.parent))
        rows.addStretch(1)
        self.tabs.addTab(page, "关于")

    # ---------------------------------------------------------------- 动作
    def _request_permissions(self):
        permissions.request_all_async(force=True)
        QMessageBox.information(
            self, "正在申请权限",
            "已向系统发起授权，请留意屏幕上弹出的对话框并点「允许」。\n"
            "若某项曾被拒绝，系统不会再弹窗，会自动带您打开对应的本机设置面板。",
        )

    def _show_devices(self):
        """选择输入麦克风对话框：列表可点选，确定即应用并同步识别页下拉。"""
        from ..core.recorder import Recorder
        try:
            devs = Recorder.devices()
        except Exception as exc:
            devs = None
            err = str(exc)
        dlg = QDialog(self)
        dlg.setWindowTitle("选择输入麦克风")
        dlg.setMinimumWidth(480)
        dlg.setWindowModality(Qt.ApplicationModal)
        v = QVBoxLayout(dlg)
        tip = QLabel("选中用于听写的输入设备后点「确定」；选「跟随系统默认」则自动使用系统当前输入。"
                     "指定设备不可用时会自动回退系统默认。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        lst = None
        if devs is None:
            v.addWidget(QLabel(f"读取设备失败：{err}"))
        elif not devs:
            v.addWidget(QLabel("未发现输入设备"))
        else:
            lst = QListWidget()
            first = QListWidgetItem("跟随系统默认（自动检测）")
            first.setData(Qt.UserRole, None)
            lst.addItem(first)
            for d in devs:
                it = QListWidgetItem(f"{d['name']}（{d['channels']} 声道）")
                it.setData(Qt.UserRole, int(d["index"]))
                lst.addItem(it)
            cur_dev = self.settings.get("input_device")
            row = 0
            for i in range(lst.count()):
                if lst.item(i).data(Qt.UserRole) == cur_dev:
                    row = i
                    break
            lst.setCurrentRow(row)
            lst.itemDoubleClicked.connect(lambda _it: dlg.accept())
            v.addWidget(lst)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() == QDialog.Accepted and lst is not None:
            sel = lst.currentItem()
            val = sel.data(Qt.UserRole) if sel else None
            try:
                self.settings.set("input_device", val)
            except Exception:
                pass
            # 同步识别页「输入麦克风」下拉的选中项（不触发重复落盘）
            for i in range(self.cb_input_device.count()):
                if self.cb_input_device.itemData(i) == val:
                    self.cb_input_device.blockSignals(True)
                    self.cb_input_device.setCurrentIndex(i)
                    self.cb_input_device.blockSignals(False)
                    break

    def _browse_llm(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择本地 GGUF 模型", "", "GGUF (*.gguf)")
        if p:
            self.ed_llm_path.setText(p)

    def _reload_terms(self):
        rows = self.storage.terms(enabled_only=False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r["term"]))
            self.table.setItem(i, 1, QTableWidgetItem(r.get("aliases", "")))
            self.table.setItem(i, 2, QTableWidgetItem(r.get("replacement", "")))

    def _add_term(self):
        term = self.ed_term.text().strip()
        if not term:
            return
        self.storage.add_term(term, self.ed_alias.text().split(","), "")
        self.ed_term.clear()
        self.ed_alias.clear()
        self._reload_terms()

    def _del_term(self):
        r = self.table.currentRow()
        if r < 0:
            return
        item = self.table.item(r, 0)
        if item:
            self.storage.remove_term(item.text())
            self._reload_terms()

    def _save(self):
        # 切换灵敏度档位时同步套用整组 VAD/增益参数；用户仍可在下方滑杆微调
        level = self.cb_sensitivity.currentText()
        threshold = self.sl_threshold.value() / 100
        silence = self.sp_silence.value()
        if level != self._initial_sensitivity:
            preset = sensitivity_preset(level)
            threshold = preset["vad_threshold"]
            silence = preset["vad_min_silence"]
            self.sl_threshold.setValue(int(threshold * 100))
            self.sp_silence.setValue(silence)
            self._initial_sensitivity = level
        self.settings.update({
            "hotkey_dictate": self.ed_hotkey.text().strip() or DEFAULT_SETTINGS["hotkey_dictate"],
            "hotkey_ask": self.ed_hotkey_ask.text().strip() or DEFAULT_SETTINGS["hotkey_ask"],
            "push_to_talk": self.ck_ptt.isChecked(),
            "inject_method": self.cb_inject.currentText(),
            "restore_clipboard": self.ck_restore.isChecked(),
            "launch_at_login": self.ck_login.isChecked(),
            "smart_output": self.ck_smart.isChecked(),
            "smart_output_when_unknown": self.cb_unknown.currentText(),
            "live_preview": self.ck_live.isChecked(),
            "overlay_style": self.cb_overlay_style.currentData() or "pill",
            "interaction_sound": self.ck_sound.isChecked(),
            "mute_others_during_input": self.ck_mute_others.isChecked(),
            "input_device": self.cb_input_device.currentData(),
            "theme": self.cb_theme.currentData() or DEFAULT_THEME,
            "model": self.cb_model.currentText(),
            "language": self.cb_lang.currentText(),
            "accelerator": self.cb_accel.currentText(),
            "precision": self.cb_precision.currentText(),
            "sensitivity": level,
            "gpu_recheck": self.ck_recheck.isChecked(),
            "num_threads": self.sp_threads.value(),
            "vad_enabled": self.ck_vad.isChecked(),
            "vad_threshold": threshold,
            "vad_min_silence": silence,
            "vad_min_speech": sensitivity_preset(level)["vad_min_speech"],
            "agc_target_peak": sensitivity_preset(level)["agc_target_peak"],
            "auto_stop_silence_s": self.sp_autostop.value(),
            "agc_enabled": self.ck_agc.isChecked(),
            "max_record_seconds": self.sp_max.value(),
            "postprocess": {k: cb.isChecked() for k, cb in self.post_checks.items()},
            "tone_mode": self.cb_tone.currentText(),
            "target_language": self.ed_target.text().strip() or "en",
            "llm_enabled": self.ck_llm.isChecked(),
            "llm_model_path": self.ed_llm_path.text().strip(),
            "organize_llm": self.ck_organize.isChecked(),
            "history_retention_days": self.sp_retention.value(),
            "save_audio": self.ck_save_audio.isChecked(),
        })
        self.saved.emit(self.settings.data)
        QMessageBox.information(
            self, "已保存",
            "设置已写入本机配置。推理后端 / 权重精度会立即重新加载模型；快捷键已立即重新注册，无需重启。",
        )
