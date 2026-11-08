"""设置窗口：快捷键、识别参数、润色规则、个人词典、权限、离线状态。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
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
    def _form() -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(18, 18, 18, 18)
        f.setSpacing(10)
        return w, f

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
        self.ck_live = QCheckBox("录音时显示两个独立置顶窗口（文字 / 声音曲线）")
        self.ck_live.setChecked(bool(s.get("live_preview", True)))
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
        f.addRow(QLabel("格式示例：cmd+shift+space、cmd+shift+e、ctrl+alt+d"),
                 QLabel("系统级热键，无需输入监控授权；保存后立即重新注册"))
        f.addRow("", self.ck_ptt)
        f.addRow("文字输出方式", self.cb_inject)
        f.addRow("", self.ck_restore)
        f.addRow("", self.ck_offline)
        f.addRow("", self.ck_login)
        f.addRow("", self.ck_smart)
        f.addRow("焦点无法判断时", self.cb_unknown)
        f.addRow("", self.ck_live)
        f.addRow("界面主题", self.cb_theme)
        self.tabs.addTab(w, "常规")

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
        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(0, 16)
        self.sp_threads.setValue(int(s["num_threads"]))
        self.sp_threads.setToolTip("0 = 自动（最多 4 线程）")
        self.ck_vad = QCheckBox("启用 Silero VAD 端点检测（推荐）")
        self.ck_vad.setChecked(bool(s["vad_enabled"]))
        self.sl_threshold = QSlider()
        from PySide6.QtCore import Qt
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
        f.addRow("", self.ck_recheck)
        f.addRow("本机加速状态", self.lb_accel)
        f.addRow("推理线程数", self.sp_threads)
        f.addRow("", self.ck_vad)
        f.addRow("VAD 灵敏度阈值", self.sl_threshold)
        f.addRow("", self.lb_threshold)
        f.addRow("静音判定（秒）", self.sp_silence)
        f.addRow("说完自动结束（秒，0=关闭）", self.sp_autostop)
        f.addRow("", self.ck_agc)
        f.addRow("单次最长录音（秒）", self.sp_max)

        buttons = QHBoxLayout()
        btn_devices = QPushButton("查看麦克风设备")
        btn_devices.clicked.connect(self._show_devices)
        buttons.addWidget(btn_devices)
        buttons.addStretch(1)
        f.addRow("", buttons)
        self.tabs.addTab(w, "识别")

    def _build_polish(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 18, 18, 18)
        box = QGroupBox("本地智能润色（逐项可关，全部在本机执行）")
        gl = QVBoxLayout(box)
        self.post_checks = {}
        opts = self.settings.get("postprocess", {})
        for key, label in POST_LABELS:
            cb = QCheckBox(label)
            cb.setChecked(bool(opts.get(key, True)))
            self.post_checks[key] = cb
            gl.addWidget(cb)
        v.addWidget(box)

        box2 = QGroupBox("语气与模式")
        fl = QFormLayout(box2)
        self.cb_tone = QComboBox()
        self.cb_tone.addItems(["auto", "formal", "casual", "code", "none"])
        self.cb_tone.setCurrentText(self.settings.get("tone_mode", "auto"))
        self.ed_target = QLineEdit(self.settings.get("target_language", "en"))
        fl.addRow("按应用切换语气", self.cb_tone)
        fl.addRow("翻译模式目标语言", self.ed_target)
        v.addWidget(box2)

        box3 = QGroupBox("智能整理与本地大模型（GGUF，完全本机推理）\n"
                         "整理只纠错、通顺、分点，内容全保留；总结在菜单栏「总结最近一次听写」单独触发")
        fl3 = QFormLayout(box3)
        self.ck_organize = QCheckBox(
            "智能整理：修正错别字、理顺语句、自动分点 —— 只去口头禅和改口，"
            "保留全部内容不做摘要删减（输出明显变短会自动重试或回退）")
        self.ck_organize.setChecked(bool(self.settings.get("organize_llm", True)))
        self.lb_llm_status = QLabel()
        self.lb_llm_status.setWordWrap(True)
        self.ck_llm = QCheckBox("启用本地 LLM（深度改写 / 翻译 / 问答 / 一键总结）")
        self.ck_llm.setChecked(bool(self.settings.get("llm_enabled", False)))
        self.ed_llm_path = QLineEdit(self.settings.get("llm_model_path", ""))
        btn_browse = QPushButton("选择模型")
        btn_browse.clicked.connect(self._browse_llm)
        path_row = QHBoxLayout()
        path_row.addWidget(self.ed_llm_path)
        path_row.addWidget(btn_browse)
        pw = QWidget()
        pw.setLayout(path_row)
        fl3.addRow("", self.ck_organize)
        fl3.addRow("整理状态", self.lb_llm_status)
        fl3.addRow("", self.ck_llm)
        fl3.addRow("模型文件", pw)
        v.addWidget(box3)
        v.addStretch(1)
        self.tabs.addTab(w, "润色")
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
        v.addWidget(QLabel("人名、品牌、专业术语：识别时做热词偏置，识别后做同音/近形纠错。"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["标准写法", "常见错写/别名（逗号分隔）", "替换为（可空）"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        v.addWidget(self.table)
        row = QHBoxLayout()
        self.ed_term = QLineEdit()
        self.ed_term.setPlaceholderText("标准写法，如：张伟 / Saylo")
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
        v.addWidget(QLabel("权限状态（仅本机读取，不做任何联网校验）"))
        self.perm_labels = []
        for st in permissions.check():
            state = "已授权" if st.granted is True else ("未授权" if st.granted is False else "需人工确认")
            box = QGroupBox(f"{st.label} · {state}")
            lay = QVBoxLayout(box)
            lay.addWidget(QLabel(PERMISSION_HELP.get(st.key, st.hint)))
            lay.addWidget(QLabel(st.hint))
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
        f.addRow("历史保留天数（0=永久）", self.sp_retention)
        v.addLayout(f)
        v.addWidget(self.ck_save_audio)
        v.addWidget(btn_perm)
        v.addStretch(1)
        self.tabs.addTab(w, "权限与隐私")

    def _build_about(self):
        w, f = self._form()
        engine = ASREngine(models_dir(), self.settings.data)
        ok, msg = engine.check_models()
        self.lb_models = QLabel(("模型完整 ✓  " if ok else "模型缺失 ✗  " + msg) +
                               "\n模型目录：" + str(models_dir()))
        self.lb_models.setWordWrap(True)
        from ..core.llm import LocalTextEngine
        self.lb_llm = QLabel(LocalTextEngine(self.settings.data).status())
        self.lb_llm.setWordWrap(True)
        f.addRow("版本", QLabel(f"{APP_NAME} v{APP_VERSION}"))
        f.addRow("运行方式", QLabel("完全离线 · 无账号 · 无更新检查 · 无遥测"))
        f.addRow("模型状态", self.lb_models)
        f.addRow("推理配置", QLabel(accel.capability_summary(self.settings.data)))
        f.addRow("实时浮层", QLabel("两个独立透明窗口固定在屏幕顶部中央："
                                   "文字（后续语音会修正前面已显示的字）、随音量变化的 1~4 条曲线"))
        f.addRow("GPU 说明", QLabel("GPU 只提供算力；准确率来自 fp32 全精度与二次复核，"
                                   "灵敏度来自 VAD 档位。本机小模型短句实测 CPU 更快，可强制 GPU 对比。"))
        f.addRow("本地 LLM", self.lb_llm)
        f.addRow("数据位置", QLabel(str(self.settings.path.parent)))
        self.tabs.addTab(w, "关于")

    # ---------------------------------------------------------------- 动作
    def _request_permissions(self):
        permissions.request_all_async(force=True)
        QMessageBox.information(
            self, "正在申请权限",
            "已向系统发起授权，请留意屏幕上弹出的对话框并点「允许」。\n"
            "若某项曾被拒绝，系统不会再弹窗，会自动带您打开对应的本机设置面板。",
        )

    def _show_devices(self):
        from ..core.recorder import Recorder
        try:
            devs = Recorder.devices()
            text = "\n".join(f"[{d['index']}] {d['name']} ({d['channels']} ch)" for d in devs) or "未发现输入设备"
        except Exception as exc:
            text = str(exc)
        QMessageBox.information(self, "本机麦克风设备", text)

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
