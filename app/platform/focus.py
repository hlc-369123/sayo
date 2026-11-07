"""判断当前焦点是否落在可编辑输入框（本机辅助功能 API，无网络）。

用途：有输入框焦点 → 识别结果直接粘贴到光标处；
      没有输入框焦点 → 只写剪贴板，不模拟按键（避免误触发应用快捷键）。
返回 None 表示无法可靠判断（权限不足 / 应用不暴露焦点控件 / 非 macOS），
由调用方按设置里的兜底策略处理。

macOS 上微信、Electron 等应用常不提供系统级 AXFocusedUIElement，
此时会从 AXFocusedWindow 的子树里找带 AXFocused 标记的控件作为兜底；
Chromium 系应用（Chrome/Edge/Electron）在无障碍树尚未构建时会先设置
AXManualAccessibility / AXEnhancedUserInterface 请求其开启，再短暂轮询。
"""
from __future__ import annotations

import sys
import time

TEXT_ROLES = {
    "AXTextField", "AXTextArea", "AXSecureTextField", "AXSearchField",
    "AXComboBox", "AXDateField", "AXTextInput", "AXSearchBox",
}
BROWSERISH_ROLES = {"AXWebArea", "AXGroup", "AXScrollArea", "AXUnknown", "AXLayoutArea"}
NON_INPUT_ROLES = {
    "AXWindow", "AXButton", "AXStaticText", "AXCheckBox", "AXRadioButton",
    "AXMenuItem", "AXList", "AXTable", "AXOutline", "AXRow", "AXImage",
    "AXToolbar", "AXTabGroup", "AXSlider", "AXPopUpButton", "AXMenuBar",
    "AXApplication", "AXDockItem", "AXScrollBar", "AXSplitGroup",
}


def _attribute(element, name: str):
    from ApplicationServices import AXUIElementCopyAttributeValue  # type: ignore
    try:
        _err, value = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    # AXFocusedApplication 等属性会带 NoValue 错误码但仍返回有效元素，忽略 err
    return value


def _force_ax_tree(app_element) -> None:
    """请求 Chromium / Electron 应用立即构建无障碍树。

    对不支持这两个属性的应用是普通写失败（错误码 -25205/-25208），无副作用。
    """
    try:
        from ApplicationServices import AXUIElementSetAttributeValue  # type: ignore
        from CoreFoundation import kCFBooleanTrue  # type: ignore
        for name in ("AXManualAccessibility", "AXEnhancedUserInterface"):
            try:
                AXUIElementSetAttributeValue(app_element, name, kCFBooleanTrue)
            except Exception:
                pass
    except Exception:
        pass


def _classify(role: str, element) -> bool | None:
    if role in TEXT_ROLES:
        return True
    if role in NON_INPUT_ROLES:
        return False
    if role in BROWSERISH_ROLES:
        desc = str(_attribute(element, "AXRoleDescription") or "").lower()
        if any(k in desc for k in ("text", "edit", "输入", "编辑", "文本", "field")):
            return True
        if _attribute(element, "AXSelectedText") is not None:
            return True
    return None


def _focused_in_window(app_element) -> bool | None:
    """从焦点窗口的 AX 子树里找被聚焦的控件（限制节点数，避免卡界面）。"""
    window = _attribute(app_element, "AXFocusedWindow")
    if window is None:
        return None
    queue = [(window, 0)]
    visited = 0
    while queue and visited < 400:
        element, depth = queue.pop(0)
        visited += 1
        focused = _attribute(element, "AXFocused")
        if focused is True or str(focused) in ("1", "True"):
            role = str(_attribute(element, "AXRole") or "")
            if role:
                return _classify(role, element)
        if depth < 6:
            children = _attribute(element, "AXChildren") or []
            try:
                for child in children:
                    queue.append((child, depth + 1))
            except TypeError:
                pass
    return None


def text_input_focused() -> bool | None:
    if sys.platform != "darwin":
        return None
    try:
        from ApplicationServices import AXUIElementCreateSystemWide  # type: ignore
        system = AXUIElementCreateSystemWide()
        # Chromium/Electron 应用可能需要数百毫秒才构建出无障碍树；
        # 微信类应用会直接拒绝查询。有界轮询避免长时间阻塞识别收尾。
        deadline = time.monotonic() + 1.2
        attempt = 0
        while True:
            attempt += 1
            focused = _attribute(system, "AXFocusedUIElement")
            if focused is not None:
                role = str(_attribute(focused, "AXRole") or "")
                if role:
                    return _classify(role, focused)

            app = _attribute(system, "AXFocusedApplication")
            if app is not None:
                if attempt == 1:
                    _force_ax_tree(app)
                result = _focused_in_window(app)
                if result is not None:
                    return result

            if time.monotonic() >= deadline or attempt >= 4:
                return None
            time.sleep(0.25)
    except Exception:
        return None


def choose_output(focused: bool | None, *, smart: bool = True,
                  unknown: str = "clipboard") -> tuple[str, str]:
    """把焦点判定翻译成输出动作与浮层提示，便于离线单测。"""
    if not smart or focused is True:
        return "paste", "已写入输入框"
    if focused is False:
        return "clipboard", "光标不在输入框 · 已复制到剪贴板（⌘V 粘贴）"
    if unknown == "paste":
        return "paste", "已写入输入框"
    return "clipboard", "焦点不明确 · 已复制到剪贴板（⌘V 粘贴）"


def describe() -> str:
    state = text_input_focused()
    if state is True:
        return "当前焦点：可编辑输入框"
    if state is False:
        return "当前焦点：非输入框"
    return "当前焦点：无法判断（按兜底策略输出）"
