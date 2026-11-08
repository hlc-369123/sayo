"""本机听写历史：查看、搜索、复制、重新整理（重试）、删除、导出 JSON（全部留在本地）。"""
from __future__ import annotations

import json
import threading
import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class HistoryWindow(QWidget):
    COLS = ["时间", "应用", "模式", "原文", "输出文字"]
    reorganized = Signal(int, str)  # 重新整理完成：(行 id, 新输出)

    def __init__(self, storage, pipeline=None):
        super().__init__()
        self.storage = storage
        self.pipeline = pipeline
        self._reorg_busy = False
        self.setWindowTitle("本机听写历史")
        self.resize(860, 520)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索本机历史…")
        self.ed_search.textChanged.connect(self.reload)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.reload)
        btn_copy = QPushButton("复制选中输出")
        btn_copy.clicked.connect(self.copy_selected)
        btn_copy_raw = QPushButton("复制原文")
        btn_copy_raw.clicked.connect(self.copy_selected_raw)
        btn_reorg = QPushButton("重新整理选中（重试）")
        btn_reorg.clicked.connect(self.reorganize_selected)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self.delete_selected)
        btn_export = QPushButton("导出 JSON")
        btn_export.clicked.connect(self.export_json)
        btn_clear = QPushButton("清空全部")
        btn_clear.clicked.connect(self.clear_all)
        for w in [self.ed_search, btn_refresh, btn_copy, btn_copy_raw,
                  btn_reorg, btn_del, btn_export, btn_clear]:
            top.addWidget(w)
        v.addLayout(top)
        self.lb = QLabel("")
        v.addWidget(self.lb)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table)
        self.reorganized.connect(self._on_reorganized)
        self.reload()

    def reload(self):
        rows = self.storage.search_history(self.ed_search.text().strip())
        self.table.setRowCount(len(rows))
        self._rows = rows
        for i, r in enumerate(rows):
            vals = [
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])),
                r["app_name"] or "-", r["mode"] or "dictate",
                r["raw_text"], r["polished_text"],
            ]
            for j, val in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(val))
        self.lb.setText(f"共 {len(rows)} 条记录 · 全部保存在本机 SQLite 数据库")

    def _selected_row(self) -> dict | None:
        r = self.table.currentRow()
        return self._rows[r] if 0 <= r < len(self._rows) else None

    def _selected_id(self):
        row = self._selected_row()
        return row["id"] if row else None

    def copy_selected(self):
        row = self._selected_row()
        if row:
            from ..platform.inject import Clipboard
            Clipboard.write(row["polished_text"])

    def copy_selected_raw(self):
        row = self._selected_row()
        if row:
            from ..platform.inject import Clipboard
            Clipboard.write(row["raw_text"])

    # ------------------------------------------------ 重试：对原文重新润色整理
    def reorganize_selected(self):
        row = self._selected_row()
        if not row:
            return
        if not self.pipeline:
            QMessageBox.information(self, "不可用", "当前未连接识别流水线，无法重新整理。")
            return
        if self._reorg_busy:
            QMessageBox.information(self, "请稍候", "上一次重新整理还在进行中。")
            return
        self._reorg_busy = True
        self.lb.setText("本机模型重新整理中…（完成后自动刷新）")
        rid, raw, bundle = row["id"], row["raw_text"], row.get("bundle_id", "")
        threading.Thread(
            target=self._reorg_work, args=(rid, raw, bundle), daemon=True,
        ).start()

    def _reorg_work(self, rid: int, raw: str, bundle_id: str):
        try:
            p = self.pipeline
            from ..core.pipeline import resolve_tone
            tone = resolve_tone(p.settings, bundle_id)
            polished = p.polisher.polish(raw, tone=tone).text
            organized, source = p.llm.organize(
                polished, hotwords=p.dictionary.hotwords())
            final = organized if source == "local-llm" and organized else polished
            self.storage.update_history_polished(rid, final)
            self.reorganized.emit(rid, final)
        except Exception as exc:  # 后台失败也要恢复按钮状态
            self.reorganized.emit(rid, f"重新整理失败：{exc}")

    def _on_reorganized(self, rid: int, text: str):
        self._reorg_busy = False
        self.reload()
        self.lb.setText(self.lb.text() + " · 已更新"
                        if not text.startswith("重新整理失败") else text)

    def delete_selected(self):
        rid = self._selected_id()
        if rid:
            self.storage.delete_history(rid)
            self.reload()

    def clear_all(self):
        if QMessageBox.question(self, "确认", "彻底删除本机全部听写历史？此操作不可撤销。") == QMessageBox.Yes:
            self.storage.clear_history()
            self.reload()

    def export_json(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出到本机", "sayo-history.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.storage.export_history(), f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "导出完成", f"已导出到 {path}（未上传任何数据）")
