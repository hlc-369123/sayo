"""一键总结窗口（完全离线）：对听写文本生成要点列表或短摘要。

数据流全部在本机：源文本可编辑（默认填最近一次听写内容，也可粘贴任意文字），
总结由本地 GGUF 模型在后台线程生成，结果可复制、可直接粘贴到当前光标处
（目标应用里有选中文本时会自动替换选中内容）。
"""
from __future__ import annotations

from concurrent.futures import Executor

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..platform.inject import Clipboard, insert_text
from ..platform import permissions


class SummaryWindow(QWidget):
    filled = Signal(str, str)  # (结果, 来源标记)：后台线程 → GUI 线程

    def __init__(self, generate_fn, executor: Executor | None = None):
        """generate_fn: (text, style) -> (result, source)；executor 保证串行执行。"""
        super().__init__()
        self._generate = generate_fn
        self._executor = executor
        self.filled.connect(self._fill)
        self.setWindowTitle("一键总结 · 完全离线")
        self.resize(720, 560)
        v = QVBoxLayout(self)

        v.addWidget(QLabel(
            "源文本（可编辑：默认是最近一次听写的整理结果，也可粘贴任意文字后重新总结）"))
        self.ed_source = QTextEdit()
        self.ed_source.setPlaceholderText("在此粘贴或保留最近一次听写文本…")
        self.ed_source.setFixedHeight(150)

        result_area = QWidget()
        rv = QVBoxLayout(result_area)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("总结结果（可手动微调后再复制 / 粘贴）"))
        self.ed_result = QTextEdit()
        self.ed_result.setPlaceholderText("点击「生成总结」…")
        rv.addWidget(self.ed_result)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.ed_source)
        split.addWidget(result_area)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        v.addWidget(split, 1)

        row = QHBoxLayout()
        self.cb_style = QComboBox()
        self.cb_style.addItem("要点列表", "points")
        self.cb_style.addItem("短摘要", "brief")
        btn_gen = QPushButton("生成总结")
        btn_gen.setDefault(True)
        btn_gen.clicked.connect(self.regenerate)
        btn_src_clip = QPushButton("用剪贴板文字作源")
        btn_src_clip.clicked.connect(self._use_clipboard)
        row.addWidget(self.cb_style)
        row.addWidget(btn_gen)
        row.addWidget(btn_src_clip)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        self.lb_status = QLabel("就绪 · 由本机模型生成，不出网")
        row2.addWidget(self.lb_status)
        row2.addStretch(1)
        btn_copy = QPushButton("复制")
        btn_copy.clicked.connect(self._copy)
        btn_paste = QPushButton("粘贴到光标 / 替换选中文本")
        btn_paste.clicked.connect(self._paste_out)
        row2.addWidget(btn_copy)
        row2.addWidget(btn_paste)
        v.addLayout(row2)

    # ---------------------------------------------------------------- 数据
    def set_source(self, text: str, auto_run: bool = True) -> None:
        self.ed_source.setPlainText(text or "")
        if auto_run and (text or "").strip():
            self.regenerate()

    def _use_clipboard(self):
        clip = Clipboard.read()
        if clip.strip():
            self.ed_source.setPlainText(clip)
        else:
            self.lb_status.setText("剪贴板为空")

    def regenerate(self):
        src = self.ed_source.toPlainText().strip()
        if not src:
            self.lb_status.setText("源文本为空：先听写一段话，或在上方粘贴文字")
            return
        self.lb_status.setText("本机模型总结中…（长文本约需十几秒）")
        style = self.cb_style.currentData() or "points"
        if self._executor is not None:
            self._executor.submit(self._work, src, style)
        else:
            self._work(src, style)

    def _work(self, src: str, style: str):
        try:
            out, source = self._generate(src, style)
        except Exception as exc:
            out, source = f"总结失败：{exc}", "error"
        # Qt 控件只能由 GUI 线程更新：跨线程信号自动排队到主线程
        self.filled.emit(out, source)

    def _fill(self, out: str, source: str):
        if source == "local-llm" and out:
            self.ed_result.setPlainText(out)
            self.lb_status.setText("完成 · 结果只存在本机")
        elif source == "unavailable":
            self.lb_status.setText("未找到本机 GGUF 模型：见 设置 → 润色 → 整理状态")
        else:
            self.lb_status.setText(out or "总结失败")

    # ---------------------------------------------------------------- 输出
    def _copy(self):
        text = self.ed_result.toPlainText().strip()
        if not text:
            self.lb_status.setText("还没有可复制的结果")
            return
        Clipboard.write(text)
        self.lb_status.setText("已复制到剪贴板")

    def _paste_out(self):
        text = self.ed_result.toPlainText().strip()
        if not text:
            self.lb_status.setText("还没有可粘贴的结果")
            return
        granted = permissions.accessibility_granted()
        if granted is False:
            Clipboard.write(text)
            self.lb_status.setText("辅助功能未授权：已复制，粘贴请按 ⌘V（见菜单栏授权引导）")
            return
        Clipboard.write(text)  # paste 方式本身依赖剪贴板
        try:
            insert_text(text, method="paste", restore_clipboard=False)
            self.lb_status.setText("已发送 ⌘V：目标应用有选中文字时会被替换，否则插入光标处")
        except Exception as exc:
            self.lb_status.setText(f"自动粘贴不可用（{exc}）；内容已复制，可手动 ⌘V")
