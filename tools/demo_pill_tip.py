"""临时演示：把胶囊浮层画出来，模拟鼠标悬停在右缘，渲染成 PNG。

仅用于（开发期）截图验证悬停提示气泡，不进生产代码路径。
用法：
    PYTHONPATH=. .venv/bin/python tools/demo_pill_tip.py [aurora|...] [out.png]
"""
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor
from PySide6.QtCore import Qt

from app.ui.overlay import _PillWindow
from app.ui.themes import get_theme

theme_key = sys.argv[1] if len(sys.argv) > 1 else "aurora"
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pill_tip_demo.png"

app = QApplication(sys.argv)
theme = get_theme(theme_key)
w = _PillWindow(theme, width=440)
w.set_visible(True)
w.set_state("listening")
w.set_content("把会议改到明天上午十点，记得同步给小李", "聆听中…", True)
w._timer.stop()  # 冻结动画，截图稳定

# 模拟鼠标悬停在右缘拉伸热区
rx = w._w - 6
w._set_tip("拖动右边缘：向左缩短 · 向右拉长", rx)

# 离屏渲染整个窗口（含顶部提示气泡）到透明 pixmap，再合成到浅灰背景便于查看
pix = QPixmap(w.size())
pix.fill(QColor(0, 0, 0, 0))
w.render(pix)

bg = QPixmap(pix.size())
bg.fill(QColor(226, 228, 232))
p = QPainter(bg)
p.drawPixmap(0, 0, pix)
p.end()
bg.save(out)
print(f"已保存：{out}  ({pix.width()}x{pix.height()})")
