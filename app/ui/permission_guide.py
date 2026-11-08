"""辅助功能授权引导窗口：解释「为什么文字没自动输入到输入框」并一键修复。

macOS 的安全模型规定：
  · 读取其他应用的焦点控件（判断光标是否在输入框）→ 需要「辅助功能」
  · 向其他应用发送 ⌘V 粘贴 → 同样需要「辅助功能」
未授权时应用只能把识别文字放进剪贴板，且**系统不允许程序代劳打开这个开关**，
必须由用户在系统设置里手动勾选 —— 本窗口把这件事说清楚并直达对应面板。
授权后无需重启应用：下一次听写即可自动输入。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..config import APP_NAME
from ..platform import permissions

_STEPS = (
    "① 点下方「打开辅助功能设置」。\n"
    "② 在右侧列表找到「轻述 Saylo」，打开开关；"
    "若列表里没有，点 ➕ 添加 /Applications/轻述 Saylo.app。\n"
    "③ 回到本窗口点「重新检测」——显示已授权即完成，无需重启。\n"
    "⚠️ 开过开关仍显示未授权？多半是历史安装留下的**旧条目**（同名但签名已变）：\n"
    "把列表里所有「轻述 Saylo」条目选中后按 − 删除，再用 ➕ 重新添加并打开即可。"
)


class PermissionGuideWindow(QWidget):
    """非模态小窗：不抢键盘焦点，用户主动点击才交互。"""

    open_settings_requested = Signal(str)  # 权限 key，由 main 统一调起系统设置

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(f"{APP_NAME} · 让文字自动输入到光标处")
        self.setMinimumWidth(560)

        title = QLabel("识别结果没自动进输入框？原因只有一个：")
        title.setStyleSheet("font-size: 16px; font-weight: 600; background: transparent;")
        self.lb_state = QLabel()
        self.lb_state.setWordWrap(True)

        body = QLabel(
            "macOS 规定：向其他应用的输入框写入文字（模拟 ⌘V 与读取光标位置）"
            "必须授予「辅助功能」权限。未授权时，本应用只能把文字放进剪贴板，"
            "这是系统硬性限制，应用无法代劳打开开关。\n\n" + _STEPS)
        body.setWordWrap(True)

        self.ck_auto = QCheckBox("启动后若仍未授权，自动弹出本窗口提醒")
        self.ck_auto.setChecked(bool(self.settings.get("ax_guide_auto", True)))
        self.ck_auto.toggled.connect(self._save_auto)

        self.btn_open = QPushButton("打开辅助功能设置")
        self.btn_recheck = QPushButton("重新检测")
        self.btn_close = QPushButton("关闭")
        self.btn_open.clicked.connect(
            lambda: self.open_settings_requested.emit("accessibility"))
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
        granted = permissions.accessibility_granted()
        if granted is True:
            self.lb_state.setText("✅ 辅助功能：已授权 —— 下一次听写，文字就会自动输入到光标处。")
        elif granted is False:
            self.lb_state.setText("❌ 辅助功能：未授权 —— 目前识别文字只放入剪贴板（⌘V 手动粘贴）。")
        else:
            self.lb_state.setText("辅助功能状态：本平台无需此权限。")
        self.btn_open.setEnabled(granted is not True)
        self.btn_recheck.setEnabled(granted is not True)

    def maybe_auto_show(self) -> bool:
        """启动自检时调用：未授权且用户允许提醒时弹窗一次。返回是否已弹出。"""
        if permissions.accessibility_granted() is not True and \
                bool(self.settings.get("ax_guide_auto", True)):
            self.refresh()
            self.show()
            return True
        return False

    def _save_auto(self, checked: bool) -> None:
        self.settings.set("ax_guide_auto", bool(checked))
