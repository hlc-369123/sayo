"""跨平台 UI 文案与权限说明（本机生成，不联网）。

把 macOS / Windows 的用户可见文案集中到这里，避免把「⌘」「/Applications」
「辅助功能」这类 Mac 专属字样硬编码进界面，在 Windows 上显得格格不入。

Windows 与 macOS 的权限模型差异（重要）：

  macOS：
    · 麦克风     —— TCC 授权，首次录音系统弹窗
    · 辅助功能   —— 读焦点控件 / 注入 ⌘V 都需要，用户手动勾选
    · 输入监控   —— 仅回退到 CGEventTap 键盘监听时才需要

  Windows：
    · 麦克风     —— 隐私设置里的开关（注册表 ConsentStore），可为桌面应用单独授权
    · 辅助功能   —— 无此概念。全局热键用系统级键盘钩子、文本注入用 SendInput /
                    剪贴板，均属普通用户态能力，不需要也不能在系统设置里授权
    · 输入监控   —— 无此概念（pynput 用低层键盘钩子，无需 UAC 或系统授权）

因此 Windows 下权限引导只谈麦克风，其余权限项不再呈现给用户。
"""
from __future__ import annotations

import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# ---- 快捷键文案 -----------------------------------------------------------
MOD_KEY = "⌘" if IS_MAC else ("Win" if IS_WIN else "Super")
ALT_KEY = "⌥ Option" if IS_MAC else "Alt"

# 组合键的可读文案（用于菜单、提示、引导）
def combo_text(combo: str) -> str:
    """把配置串 'cmd+shift+space' 转成当前平台可读文案。

    macOS: ⌘ + Shift + 空格
    Windows: Ctrl + Shift + 空格
    """
    if not combo:
        return ""
    parts = []
    for p in str(combo).split("+"):
        p = p.strip().lower()
        if p in ("cmd", "command", "meta", "super", "win"):
            parts.append(MOD_KEY)  # Windows 上显示 Win，绝不显示 Mac 的 ⌘
        elif p in ("ctrl", "control"):
            parts.append("Ctrl")
        elif p in ("alt", "option", "opt"):
            parts.append(ALT_KEY)
        elif p == "shift":
            parts.append("Shift")
        elif p == "space":
            parts.append("空格")
        elif p == "esc":
            parts.append("Esc")
        elif p == "enter":
            parts.append("回车")
        else:
            parts.append(p.upper() if len(p) == 1 else p)
    return " + ".join(parts)


def paste_text() -> str:
    """手动粘贴的快捷键文案。"""
    return "⌘V" if IS_MAC else "Ctrl+V"


def default_hotkey_hint() -> str:
    """设置窗口/引导里展示的默认快捷键示例。"""
    # macOS 用带空格的原始写法，与历史提示文案逐字一致
    return "⌘ + Shift + 空格" if IS_MAC else "Alt"


# ---- 权限文案 -------------------------------------------------------------
def permission_intro() -> str:
    """权限引导窗口的正文说明。"""
    if IS_WIN:
        return (
            "Windows 下本应用只需要一项系统授权：麦克风。\n"
            "允许后即可在本机采集语音并离线转成文字；音频不会离开此设备。\n\n"
            "全局快捷键、把文字写入输入框都不需要额外系统授权——Windows 默认允许；"
            "本应用不会读取键盘记录，也不会上传任何音频。"
        )
    return (
        "macOS 规定：向其他应用的输入框写入文字（模拟 ⌘V 与读取光标位置）"
        "必须授予「辅助功能」权限。未授权时，本应用只能把文字放进剪贴板，"
        "这是系统硬性限制，应用无法代劳打开开关。\n\n"
    )


def permission_steps() -> str:
    """权限引导窗口的操作步骤（不含 macOS 专属路径时更安全）。"""
    if IS_WIN:
        return (
            "① 点下方「打开麦克风设置」。\n"
            "② 确认「麦克风访问权限」和「允许桌面应用访问麦克风」两个开关都已打开。\n"
            "③ 回到本窗口点「重新检测」。\n"
            "⚠️ 若已允许仍无法录音：检查系统输入音量是否为 0、麦克风是否被其他应用独占，"
            "或在「设置 → 识别 → 输入麦克风」里改选具体设备。"
        )
    return (
        "① 点下方「打开辅助功能设置」。\n"
        "② 在右侧列表找到「轻述 sayo」，打开开关；"
        "若列表里没有，点 ➕ 添加 /Applications/轻述 sayo.app。\n"
        "③ 回到本窗口点「重新检测」——显示已授权即完成，无需重启。\n"
        "⚠️ 开过开关仍显示未授权？多半是历史安装留下的**旧条目**（同名但签名已变）：\n"
        "把列表里所有「轻述 sayo」条目选中后按 − 删除，再用 ➕ 重新添加并打开即可。"
    )


def permission_state_text(label: str, granted: bool | None) -> str:
    """权限状态行。"""
    state = "已授权" if granted is True else ("未授权" if granted is False else "需人工确认")
    return f"{label}：{state}"


def accessibility_hint() -> str:
    """「辅助功能未授权」时的状态提示。"""
    if IS_WIN:
        # Windows 无此权限，正常不会出现；保留兜底措辞避免暴露 Mac 字样
        return "文字已放入剪贴板，可用 Ctrl+V 粘贴。"
    return f"文字已放入剪贴板，可用 {paste_text()} 粘贴。"


def need_accessibility_prompt() -> bool:
    """是否需要对「辅助功能」做授权引导弹窗。

    Windows 无此概念，返回 False —— 避免启动 8 秒后弹出一个满是 macOS 文案、
    还显示「本平台无需此权限」的诡异窗口（历史 bug）。
    """
    return IS_MAC
