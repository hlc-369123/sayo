"""内置主题：一套配色同时定义「听写浮层」（文字 + 曲线）与「应用界面」。

设计原则（按用户要求：简洁、高级）：
  · 浮层没有底板：文字用「描边 + 填充」保证在任意浅色/深色应用上都看得清
  · 曲线 1~4 条，颜色由浅入深、带柔光；声音小近直线，声音大条数与振幅一起增大
  · 界面：单色背景 + 一个强调色，无边框装饰堆叠；浅色主题（晨雾/樱粉）配深色文字
所有颜色都在本机定义，不读取任何远程资源。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtGui import QColor

_GLOW_ALPHAS = (46, 40, 34, 28)


def _glow(waves: tuple[QColor, ...]) -> tuple[QColor, ...]:
    return tuple(QColor(c.red(), c.green(), c.blue(), a)
                 for c, a in zip(waves, _GLOW_ALPHAS))


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    # 浮层文字：live = 正在识别，final = 最终结果；stroke = 描边（保证可读性）
    text_live: QColor
    text_final: QColor
    text_stroke: QColor
    # 浮层曲线：由浅入深的 1~4 条
    waves: tuple[QColor, ...]
    glow: tuple[QColor, ...] = field(default=())
    # 浮层底板（毛玻璃胶囊）：空字符串 = 无底板；8 位 hex 可带透明度
    panel_bg: str = ""
    panel_border: str = ""
    # 界面配色
    ui_bg: str = "#0F1218"
    ui_panel: str = "#161C24"
    ui_fg: str = "#E7EDF6"
    ui_muted: str = "#8A94A8"
    ui_accent: str = "#5EA0FF"
    ui_border: str = "#232B38"

    def __post_init__(self):
        if not self.glow:
            object.__setattr__(self, "glow", _glow(self.waves))


def _t(key: str, name: str, *, live: str, final: str, stroke: str,
       waves: tuple[str, ...], **ui) -> Theme:
    mk = lambda s, a=255: QColor(s)  # noqa: E731
    live_c = QColor(live)
    final_c = QColor(final)
    stroke_c = QColor(stroke)
    wave_c = tuple(QColor(w) for w in waves)
    return Theme(key=key, name=name, text_live=live_c, text_final=final_c,
                 text_stroke=stroke_c, waves=wave_c, **ui)


THEMES: dict[str, Theme] = {t.key: t for t in (
    _t("frost", "浅白毛玻璃",
       live="#1A1A1A", final="#1A1A1A", stroke="#FFFFFF",
       waves=("#3B7BFF", "#8A5CF6", "#7DD3FC", "#A78BFA"),
       ui_bg="#F2F3F7", ui_panel="#FFFFFF", ui_fg="#1A1A1A",
       ui_muted="#8A8FA0", ui_accent="#4F8BFF", ui_border="#E4E6EC",
       panel_bg="#C8F6F7FB", panel_border="#66FFFFFF"),
    _t("aurora", "极光",
       live="#F8FBFF", final="#D6E2F0", stroke="#060A12",
       waves=("#7DD3FC", "#60A5FA", "#8BA2FD", "#A78BFA"),
       ui_bg="#0F1218", ui_panel="#161C24", ui_fg="#E7EDF6",
       ui_muted="#8A94A8", ui_accent="#5EA0FF", ui_border="#232B38"),
    _t("mist", "晨雾（浅色）",
       live="#1E293B", final="#334155", stroke="#FFFFFF",
       waves=("#0EA5E9", "#14B8A6", "#38BDF8", "#2DD4BF"),
       ui_bg="#F3F6FA", ui_panel="#FFFFFF", ui_fg="#1B2531",
       ui_muted="#64748B", ui_accent="#2563EB", ui_border="#DCE3EC"),
    _t("ink_gold", "墨金",
       live="#F6EFDF", final="#E3D9C2", stroke="#14100A",
       waves=("#F2C14E", "#E8A33D", "#D97706", "#FACC15"),
       ui_bg="#121015", ui_panel="#1A171E", ui_fg="#F0E8D8",
       ui_muted="#A08F76", ui_accent="#D9A441", ui_border="#2B2532"),
    _t("celadon", "青瓷",
       live="#F2FBF7", final="#CFE5DC", stroke="#06120E",
       waves=("#5EEAD4", "#34D399", "#2DD4BF", "#A7F3D0"),
       ui_bg="#0E1512", ui_panel="#152019", ui_fg="#E4F0EA",
       ui_muted="#7E9C8F", ui_accent="#34D399", ui_border="#20302A"),
    _t("sakura", "樱粉（浅色）",
       live="#3D2530", final="#5B3A48", stroke="#FFFFFF",
       waves=("#F9A8D4", "#F472B6", "#FB7185", "#FDA4AF"),
       ui_bg="#FBF3F7", ui_panel="#FFFFFF", ui_fg="#3D2430",
       ui_muted="#A3738A", ui_accent="#EC4899", ui_border="#F0DCE4"),
    _t("graphite", "石墨",
       live="#F5F7FA", final="#C9D1DB", stroke="#0A0C0F",
       waves=("#E2E8F0", "#B7C1CE", "#8B98A8", "#64748B"),
       ui_bg="#101114", ui_panel="#191B1F", ui_fg="#E5E7EB",
       ui_muted="#8B929C", ui_accent="#A9B4C2", ui_border="#26292E"),
    _t("neon", "霓虹",
       live="#F0FDFF", final="#BFEFF6", stroke="#05121A",
       waves=("#22D3EE", "#E879F9", "#A78BFA", "#F472B6"),
       ui_bg="#0A0F14", ui_panel="#0F1820", ui_fg="#E6F7FF",
       ui_muted="#6E93A8", ui_accent="#22D3EE", ui_border="#17303C"),
    _t("ocean", "海盐",
       live="#F4FAFF", final="#CFE3F2", stroke="#050D18",
       waves=("#38BDF8", "#0EA5E9", "#7DD3FC", "#67E8F9"),
       ui_bg="#0B1220", ui_panel="#121B2E", ui_fg="#E3ECFA",
       ui_muted="#8398B8", ui_accent="#38BDF8", ui_border="#1D2B46"),
)}

DEFAULT_THEME = "aurora"


def get_theme(key: str | None) -> Theme:
    return THEMES.get((key or "").strip().lower(), THEMES[DEFAULT_THEME])


def theme_items() -> list[tuple[str, str]]:
    """(key, 显示名) 有序列表，供设置下拉框使用。"""
    return [(t.key, t.name) for t in THEMES.values()]


def ui_stylesheet(theme: Theme) -> str:
    """极简单色 + 强调色的应用界面样式。"""
    return f"""
QWidget {{ background: {theme.ui_bg}; color: {theme.ui_fg}; font-size: 13px; }}
QLabel {{ background: transparent; }}
QToolTip {{ background: {theme.ui_panel}; color: {theme.ui_fg};
            border: 1px solid {theme.ui_border}; padding: 4px; }}
QGroupBox {{
    background: {theme.ui_panel}; border: 1px solid {theme.ui_border};
    border-radius: 10px; margin-top: 16px; padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px;
                    color: {theme.ui_fg}; background: transparent; }}
QPushButton {{
    background: {theme.ui_panel}; border: 1px solid {theme.ui_border};
    border-radius: 8px; padding: 6px 16px; color: {theme.ui_fg};
}}
QPushButton:hover {{ border-color: {theme.ui_accent}; color: {theme.ui_accent}; }}
QPushButton:pressed {{ background: {theme.ui_bg}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {{
    background: {theme.ui_bg}; border: 1px solid {theme.ui_border};
    border-radius: 8px; padding: 4px 8px; color: {theme.ui_fg};
    selection-background-color: {theme.ui_accent};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {theme.ui_panel}; color: {theme.ui_fg};
    border: 1px solid {theme.ui_border};
    selection-background-color: {theme.ui_accent}; selection-color: {theme.ui_bg};
}}
QTabWidget::pane {{ border: 1px solid {theme.ui_border}; border-radius: 10px;
                    top: -1px; background: {theme.ui_panel}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {theme.ui_muted};
                padding: 8px 18px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {theme.ui_accent};
                         border-bottom: 2px solid {theme.ui_accent}; }}
QTabBar::tab:hover {{ color: {theme.ui_fg}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {theme.ui_border};
                        border-radius: 5px; background: {theme.ui_bg}; }}
QCheckBox::indicator:checked {{ background: {theme.ui_accent};
                                border-color: {theme.ui_accent}; }}
QTableWidget {{ background: {theme.ui_panel}; border: 1px solid {theme.ui_border};
                border-radius: 8px; gridline-color: {theme.ui_border}; }}
QTableWidget::item:selected {{ background: {theme.ui_accent}; color: {theme.ui_bg}; }}
QHeaderView::section {{ background: {theme.ui_panel}; color: {theme.ui_muted};
                        border: none; border-bottom: 1px solid {theme.ui_border};
                        padding: 6px; }}
QSlider::groove:horizontal {{ height: 4px; background: {theme.ui_border};
                              border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0;
                              border-radius: 7px; background: {theme.ui_accent}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {theme.ui_border}; border-radius: 4px;
                               min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {theme.ui_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""
