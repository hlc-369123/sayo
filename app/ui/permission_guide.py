"""权限引导窗口：解释「为什么没声音 / 文字没自动输入」并一键修复。

按平台给出完全不同的说明：

  · macOS：写文字需要「辅助功能」授权，给出三步勾选引导（历史逻辑）。
  · Windows：只需要麦克风授权，且真正需要的是「隐私和安全性 → 麦克风」里的
    两个开关；全局热键与文字注入在 Windows 是默认能力，没有辅助功能这一说。
    旧版在这里硬编码 macOS 文案，Windows 用户会看到「⌘V」「/Applications」
    和「本平台无需此权限」这种自相矛盾的窗口——本实现彻底分开处理。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..config import APP_NAME
from ..platform import permissions
from ..platform.platform_text import (
    IS_WIN, accessibility_hint, combo_text, default_hotkey_hint,
    need_accessibility_prompt, paste_text, permission_intro, permission_steps,
)


class PermissionGuideWindow(QWidget):
    """非模态小窗：不抢键盘焦点，用户主动点击才交互。"""

    open_settings_requested = Signal(str)  # 权限 key，由 main 统一调起系统设置

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(f"{APP_NAME} · " + ("麦克风授权" if IS_WIN else "让文字自动输入到光标处"))
        self.setMinimumWidth(560)

        # 标题与勾选框文案按平台分写：Windows 只谈麦克风，macOS 保持原文不改
        title = QLabel("无法正常使用？先看这里：" if IS_WIN
                       else "识别结果没自动进输入框？原因只有一个：")
        title.setStyleSheet("font-size: 16px; font-weight: 600; background: transparent;")
        self.lb_state = QLabel()
        self.lb_state.setWordWrap(True)

        body = QLabel(permission_intro() + "\n" + permission_steps())
        body.setWordWrap(True)

        self.ck_auto = QCheckBox("启动后若异常，自动弹出本窗口提醒" if IS_WIN
                                 else "启动后若仍未授权，自动弹出本窗口提醒")
        self.ck_auto.setChecked(bool(self.settings.get("ax_guide_auto", True)))
        self.ck_auto.toggled.connect(self._save_auto)

        self.btn_open = QPushButton("打开麦克风设置" if IS_WIN else "打开辅助功能设置")
        self.btn_recheck = QPushButton("重新检测")
        self.btn_close = QPushButton("关闭")
        self.btn_open.clicked.connect(
            lambda: self.open_settings_requested.emit("microphone" if IS_WIN else "accessibility"))
        self.btn_recheck.clicked.connect(self.refresh)
        self.btn_close.clicked.connect(self.close)

        row = QHBoxLayout()
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_recheck)
        row.addStretch(1)
        row.addWidget(self.btn_close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)
        lay.addWidget(title)
        lay.addWidget(self.lb_state)
        lay.addWidget(body)
        lay.addWidget(self.ck_auto)
        lay.addLayout(row)
        self.refresh()

    # ------------------------------------------------------------ 状态
    def refresh(self) -> None:
        if IS_WIN:
            mic = permissions._win_microphone()
            if mic is True:
                self.lb_state.setText(
                    "✅ 麦克风：已允许 —— 可以正常开始听写了。")
            elif mic is False:
                self.lb_state.setText(
                    "❌ 麦克风：已拒绝 —— 请按下面的步骤在 Windows 设置里打开两个开关。")
            else:
                self.lb_state.setText(
                    "麦克风状态：无法自动读取 —— 请打开 Windows 设置确认开关已开启。")
            self.btn_open.setEnabled(mic is not True)
            self.btn_recheck.setEnabled(True)
            # Windows 下快捷键说明也用本机平台文案
            self.lb_state.setToolTip(f"听写快捷键：{default_hotkey_hint()}")
            return

        granted = permissions.accessibility_granted()
        if granted is True:
            self.lb_state.setText("✅ 辅助功能：已授权 —— 下一次听写，文字就会自动输入到光标处。")
        elif granted is False:
            self.lb_state.setText(
                f"❌ 辅助功能：未授权 —— 目前识别文字只放入剪贴板（{paste_text()} 手动粘贴）。")
        else:
            self.lb_state.setText("辅助功能状态：本平台无需此权限。")
        self.btn_open.setEnabled(granted is not True)
        self.btn_recheck.setEnabled(granted is not True)

    def maybe_auto_show(self) -> bool:
        """启动自检时调用：检测到异常且用户允许提醒时弹窗一次。返回是否已弹出。

        Windows 下只有麦克风被显式拒绝才自动弹；macOS 下沿用辅助功能判断。
        """
        if not bool(self.settings.get("ax_guide_auto", True)):
            return False
        if IS_WIN:
            abnormal = permissions._win_microphone() is False
        else:
            abnormal = permissions.accessibility_granted() is not True
        if abnormal:
            self.refresh()
            self.show()
            return True
        return False

    def _save_auto(self, checked: bool) -> None:
        self.settings.set("ax_guide_auto", bool(checked))
