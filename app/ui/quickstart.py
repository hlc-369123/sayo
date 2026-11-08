"""首次启动的「怎么用」引导：三步上手、权限一键申请、免麦克风试识别。"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..config import APP_NAME
from ..platform import permissions


class QuickStartWindow(QWidget):
    result_ready = Signal(str)
    perms_refreshed = Signal(object)

    def __init__(self, settings, probe=None):
        super().__init__()
        self.settings = settings
        self.probe = probe
        self.setWindowTitle(f"{APP_NAME} · 怎么用")
        # 不锁死高度：内容（三个步骤卡片 + 按钮）较多，固定高度会裁掉底部按钮
        self.setMinimumSize(560, 540)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)

        title = QLabel("三步开始使用")
        title.setStyleSheet("font-size: 18px; font-weight: 500;")
        lay.addWidget(title)
        intro = QLabel("在任意应用中按快捷键说话，语音在本机变成文字并写入光标处。全程离线，无需账号。")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # 第一步：权限
        box1 = QGroupBox("1 · 本机授权（麦克风 / 辅助功能）")
        v1 = QVBoxLayout(box1)
        self.lb_perm = QLabel()
        self.lb_perm.setWordWrap(True)
        v1.addWidget(self.lb_perm)
        row = QVBoxLayout()
        btn_perm = QPushButton("一键重新申请权限")
        btn_perm.clicked.connect(self._request_permissions)
        row.addWidget(btn_perm)
        v1.addLayout(row)
        lay.addWidget(box1)

        # 第二步：操作
        box2 = QGroupBox("2 · 听写")
        v2 = QVBoxLayout(box2)
        step2 = QLabel(
            "把光标点进任意输入框 → 同时按住 ⌘ + Shift + 空格 三个键（Space 就是键盘"
            "最下方那个空格键）→ 说话 → 再按一次结束。\n"
            "⌘ + Shift + E = 语音编辑选中文字。\n"
            "快捷键是系统级注册，不需要「输入监控」授权，覆盖安装后也不会失效；"
            "应用不会抢走输入框光标，点回输入框不会中断听写。\n"
            "也可以点菜单栏「开始 / 结束听写」手动操作。按了没反应可用菜单栏"
            "「快捷键没反应？诊断 / 修复…」查看是否被其他应用占用了组合键。")
        step2.setWordWrap(True)
        v2.addWidget(step2)
        lay.addWidget(box2)

        # 第三步：结果去向与浮层
        box3 = QGroupBox("3 · 结果去哪了 / 屏幕上看到什么")
        v3 = QVBoxLayout(box3)
        step3 = QLabel(
            "· 光标在输入框里：识别完直接写进该输入框。\n"
            "· 光标不在输入框里：只放进剪贴板，不模拟按键（按 ⌘V 自己粘）。\n"
            "· 微信等应用不暴露焦点控件时，默认也只放剪贴板；可在设置 → 常规 → "
            "「焦点无法判断时」改成自动粘贴。若辅助功能未授权，一律只写剪贴板。\n"
            "· 录音时屏幕顶部中央有两个彼此独立的透明窗口：上面只有一行实时识别文字"
            "（后面说的话会回头修正前面显示的字），下面只有 1~4 条随音量变化的曲线；"
            "切换应用、切换桌面、进入全屏都不会消失，也不会挡住鼠标点击。")
        step3.setWordWrap(True)
        v3.addWidget(step3)
        lay.addWidget(box3)

        self.lb_test = QLabel("")
        self.lb_test.setWordWrap(True)
        lay.addWidget(self.lb_test)
        btn_test = QPushButton("不用麦克风，先用内置语音样本试一次识别")
        btn_test.clicked.connect(self._test)
        lay.addWidget(btn_test)

        btn_close = QPushButton("开始使用")
        btn_close.setDefault(True)
        btn_close.clicked.connect(self._finish)
        lay.addWidget(btn_close)

        self.result_ready.connect(self._on_result)
        self.perms_refreshed.connect(lambda _r: self._refresh())
        self._refresh()

    def _refresh(self, _result=None):
        lines = []
        for st in permissions.check():
            state = "已授权" if st.granted is True else ("未授权" if st.granted is False else "需人工确认")
            lines.append(f"{st.label}：{state}")
        self.lb_perm.setText(" · ".join(lines) +
                             "\n全局快捷键无需输入监控授权；辅助功能仅用于「自动输入到输入框」，"
                             "未授权时文字仍会进入剪贴板。若识别结果没自动进输入框，"
                             "点菜单栏「授权引导 · 文字自动输入」按三步勾选即可。")

    def _request_permissions(self):
        self.lb_perm.setText("正在向系统发起授权弹窗，请留意屏幕上的对话框…")
        permissions.request_all_async(
            force=True, callback=lambda r: self.perms_refreshed.emit(r)
        )

    def _test(self):
        self.lb_test.setText("正在本机离线识别…")
        def work():
            try:
                text = self.probe() if self.probe else "（本窗口未提供测试接口）"
            except Exception as exc:
                text = f"测试失败：{exc}"
            self.result_ready.emit(text)
        threading.Thread(target=work, daemon=True, name="quickstart-probe").start()

    def _on_result(self, text: str):
        self.lb_test.setText("本机识别结果：" + text)

    def _finish(self):
        self.settings.set("first_run_done", True)
        self.close()
