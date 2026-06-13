#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GameGallery - Steam 大屏幕风格游戏藏品展示工具 (修复版)
修复: QMenu崩溃、图片刷新、菜单功能、图片比例
"""

import sys
import os
import shutil
import json
import subprocess
import zipfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Tuple
from collections import defaultdict

from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *

# ============================================================================
# 配置
# ============================================================================
def _root_has_game_data(root: Path) -> bool:
    """检查目录是否包含游戏数据"""
    if not root.exists():
        return False
    if (root / "games.json").exists() or (root / "config.ini").exists():
        return True
    for item in root.iterdir():
        if item.is_dir() and not item.name.startswith('.'):
            return True
    return False


def _get_root_path() -> Path:
    """动态确定游戏数据根目录
    - 源码运行时：使用本文件所在目录
    - PyInstaller 打包后：优先 exe 所在目录，否则回退 D:/GameGallery
    """
    if getattr(sys, 'frozen', False):
        # 优先使用 exe 所在目录（便携模式）
        exe_dir = Path(sys.executable).parent
        if _root_has_game_data(exe_dir):
            return exe_dir
        # 回退到旧版硬编码路径 D:/GameGallery（兼容老用户数据）
        legacy = Path("D:/GameGallery")
        if _root_has_game_data(legacy):
            return legacy
        # 都没有则默认使用 exe 目录，启动后提示用户选择
        return exe_dir
    else:
        return Path(__file__).parent

ROOT_PATH = _get_root_path()
GAMES_JSON = ROOT_PATH / "games.json"
SUPPORTED_EXTS = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')

COL_BG = QColor("#0e141b")
COL_BG_LIGHT = QColor("#1a2330")
COL_ACCENT = QColor("#1a9fff")
COL_ACCENT_GLOW = QColor("#66c0f4")
COL_TEXT = QColor("#ffffff")
COL_TEXT_DIM = QColor("#8a8a8a")

CARD_W = 360
CARD_H = 200
CARD_RADIUS = 8
CARD_MARGIN = 16
ROW_TITLE_H = 50
TOPBAR_H = 60
BOTTOMBAR_H = 50
CG_PANEL_W = 380

# 文件对话框选项：禁用原生对话框，避免 PyInstaller 打包后崩溃
FILE_DIALOG_OPTIONS = QFileDialog.Option.DontUseNativeDialog

# ============================================================================
# 数据类
# ============================================================================
@dataclass
class GameInfo:
    name: str
    path: Path
    category: str
    sub: str
    cover: Optional[Path] = None
    wallpaper: Optional[Path] = None
    cg_files: List[Path] = field(default_factory=list)
    cover_pixmap: Optional[QPixmap] = None
    wallpaper_pixmap: Optional[QPixmap] = None
    # 元数据字段
    favorite: bool = False
    sort_weight: float = 0.0
    exe_path: Optional[str] = None
    exe_pixmap: Optional[QPixmap] = None
    note: str = ""
    rating: int = 0
    added_time: Optional[float] = None

# ============================================================================
# 元数据管理
# ============================================================================
class GameMetadata:
    """管理 games.json 元数据文件"""
    _data: Dict[str, dict] = {}
    _loaded = False

    @classmethod
    def _ensure_loaded(cls):
        if not cls._loaded:
            cls.load()
            cls._loaded = True

    @classmethod
    def load(cls):
        if GAMES_JSON.exists():
            try:
                with open(GAMES_JSON, 'r', encoding='utf-8') as f:
                    cls._data = json.load(f)
            except Exception as e:
                print(f"加载元数据失败: {e}")
                cls._data = {}
        else:
            cls._data = {}

    @classmethod
    def save(cls):
        ensure_root()
        try:
            with open(GAMES_JSON, 'w', encoding='utf-8') as f:
                json.dump(cls._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存元数据失败: {e}")

    @classmethod
    def _game_key(cls, game: GameInfo) -> str:
        return f"{game.category}/{game.sub}/{game.name}"

    @classmethod
    def get(cls, game: GameInfo) -> dict:
        cls._ensure_loaded()
        return cls._data.get(cls._game_key(game), {})

    @classmethod
    def set(cls, game: GameInfo, **kwargs):
        cls._ensure_loaded()
        key = cls._game_key(game)
        if key not in cls._data:
            cls._data[key] = {}
        cls._data[key].update(kwargs)
        cls.save()

    @classmethod
    def remove(cls, game: GameInfo):
        cls._ensure_loaded()
        key = cls._game_key(game)
        if key in cls._data:
            del cls._data[key]
            cls.save()

    @classmethod
    def cleanup_orphaned(cls, valid_games: List[GameInfo]):
        """清理已不存在的游戏元数据"""
        cls._ensure_loaded()
        valid_keys = {cls._game_key(g) for g in valid_games}
        orphaned = [k for k in cls._data if k not in valid_keys]
        if orphaned:
            for k in orphaned:
                del cls._data[k]
            cls.save()


# ============================================================================
# 图片加载器
# ============================================================================
class ImageLoader(QObject):
    """全局图片加载器 - 使用一次性 callback 机制避免信号连接累积"""
    _result_ready = pyqtSignal(str, QPixmap)

    def __init__(self):
        super().__init__()
        self._callbacks: Dict[str, List[Callable[[str, QPixmap], None]]] = {}
        self._result_ready.connect(self._dispatch_result)

    def load_once(self, path: str, size: QSize, callback: Callable[[str, QPixmap], None]):
        """加载图片，加载完成后只调用一次 callback（同一路径不会去重启动多个任务）"""
        if path not in self._callbacks:
            self._callbacks[path] = []
            QThreadPool.globalInstance().start(self._make_task(path, size))
        self._callbacks[path].append(callback)

    def _make_task(self, path: str, size: QSize):
        def _load():
            try:
                pixmap = QPixmap(path)
                if not pixmap.isNull() and not size.isEmpty():
                    pixmap = pixmap.scaled(size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                self._result_ready.emit(path, pixmap)
            except Exception as e:
                print(f"Load error [{path}]: {e}")
                self._result_ready.emit(path, QPixmap())
        return _load

    def _dispatch_result(self, path: str, pixmap: QPixmap):
        """主线程分发结果给所有等待的 callback"""
        callbacks = self._callbacks.pop(path, [])
        for callback in callbacks:
            try:
                callback(path, pixmap)
            except Exception as e:
                print(f"ImageLoader callback error [{path}]: {e}")

_image_loader = ImageLoader()

# ============================================================================
# 文件管理工具
# ============================================================================
def ensure_root():
    ROOT_PATH.mkdir(parents=True, exist_ok=True)

def get_categories() -> Dict[str, List[str]]:
    cats = defaultdict(list)
    ini_path = ROOT_PATH / "config.ini"
    if not ini_path.exists():
        for big in sorted(ROOT_PATH.iterdir()):
            if big.is_dir() and not big.name.startswith('.'):
                subs = [s.name for s in sorted(big.iterdir()) if s.is_dir()]
                cats[big.name] = subs if subs else ["默认分类"]
        return dict(cats)

    current = ""
    with open(ini_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('[') and line.endswith(']'):
                current = line[1:-1]
            elif '=' in line and current:
                sub = line.split('=')[0].strip()
                if sub:
                    cats[current].append(sub)
    return dict(cats)

def add_category(big_name: str, sub_name: str) -> bool:
    ensure_root()
    ini_path = ROOT_PATH / "config.ini"
    sub_path = ROOT_PATH / big_name / sub_name
    sub_path.mkdir(parents=True, exist_ok=True)

    lines = []
    if ini_path.exists():
        with open(ini_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    sections = {}
    current_sec = None
    for line in lines:
        s = line.strip()
        if s.startswith('[') and s.endswith(']'):
            current_sec = s[1:-1]
            sections[current_sec] = sections.get(current_sec, [])
        elif '=' in s and current_sec:
            sections[current_sec].append(s.split('=')[0].strip())

    if big_name not in sections:
        sections[big_name] = []
    if sub_name not in sections[big_name]:
        sections[big_name].append(sub_name)

    with open(ini_path, 'w', encoding='utf-8') as f:
        for sec_name, subs in sorted(sections.items()):
            f.write(f"[{sec_name}]\n")
            for sub in sorted(subs):
                f.write(f"{sub}=\n")
            f.write("\n")
    return True

def add_game(category: str, sub: str, game_name: str) -> Path:
    game_path = ROOT_PATH / category / sub / game_name
    game_path.mkdir(parents=True, exist_ok=True)
    (game_path / "cg").mkdir(exist_ok=True)
    # 记录添加时间到元数据
    temp_game = GameInfo(name=game_name, path=game_path, category=category, sub=sub)
    GameMetadata.set(temp_game, added_time=QDateTime.currentDateTime().toSecsSinceEpoch())
    return game_path

def copy_image(src: str, dst: Path) -> bool:
    try:
        shutil.copy2(src, dst)
        return True
    except Exception as e:
        print(f"Copy error: {e}")
        return False

def delete_game(game_path: Path) -> bool:
    try:
        shutil.rmtree(game_path)
        return True
    except Exception as e:
        print(f"Delete error: {e}")
        return False

def load_game_note(path: Path) -> str:
    """读取游戏目录下的 note.txt"""
    note_file = path / "note.txt"
    if note_file.exists():
        try:
            return note_file.read_text(encoding='utf-8')
        except Exception as e:
            print(f"读取笔记失败 {note_file}: {e}")
    return ""


def save_game_note(path: Path, text: str):
    """保存游戏笔记到 note.txt，空内容则删除文件"""
    note_file = path / "note.txt"
    try:
        if text.strip():
            note_file.write_text(text, encoding='utf-8')
        else:
            if note_file.exists():
                note_file.unlink()
    except Exception as e:
        print(f"保存笔记失败 {note_file}: {e}")


def scan_games() -> List[GameInfo]:
    games: List[GameInfo] = []
    if not ROOT_PATH.exists():
        return games

    ini_path = ROOT_PATH / "config.ini"
    if not ini_path.exists():
        for item in sorted(ROOT_PATH.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                g = _scan_game_folder(item, item.name, "默认分类")
                if g:
                    games.append(g)
        GameMetadata.cleanup_orphaned(games)
        return games

    current_section = ""
    with open(ini_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(';'):
                continue
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1]
            elif '=' in line and current_section:
                sub = line.split('=')[0].strip()
                sub_path = ROOT_PATH / current_section / sub
                if sub_path.exists():
                    for game_dir in sorted(sub_path.iterdir()):
                        if game_dir.is_dir():
                            g = _scan_game_folder(game_dir, current_section, sub)
                            if g:
                                games.append(g)
    GameMetadata.cleanup_orphaned(games)
    return games

def _scan_game_folder(path: Path, category: str, sub: str) -> Optional[GameInfo]:
    cover = None
    wallpaper = None
    for ext in SUPPORTED_EXTS:
        c = path / f"cover{ext}"
        if c.exists():
            cover = c
            break
    for ext in SUPPORTED_EXTS:
        w = path / f"wallpaper{ext}"
        if w.exists():
            wallpaper = w
            break

    cg_dir = path / "cg"
    cg_files = []
    if cg_dir.exists():
        for f in sorted(cg_dir.iterdir()):
            if f.suffix.lower() in SUPPORTED_EXTS:
                cg_files.append(f)

    game = GameInfo(
        name=path.name,
        path=path,
        category=category,
        sub=sub,
        cover=cover,
        wallpaper=wallpaper,
        cg_files=cg_files
    )

    # 加载笔记
    game.note = load_game_note(path)

    # 加载元数据
    meta = GameMetadata.get(game)
    game.favorite = meta.get("favorite", False)
    game.sort_weight = meta.get("sort_weight", 0.0)
    game.exe_path = meta.get("exe_path", None)
    game.rating = meta.get("rating", 0)
    game.added_time = meta.get("added_time", None)

    return game


# ============================================================================
# 图片裁剪对话框
# ============================================================================
class ImageCropDialog(QDialog):
    def __init__(self, image_path: str, target_w: int, target_h: int, title: str = "裁剪图片", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(900, 700)
        self.setStyleSheet("background-color: #0e141b;")

        self.image_path = image_path
        self.target_w = target_w
        self.target_h = target_h
        self.target_ratio = target_w / target_h
        self.original_pixmap = QPixmap(image_path)
        self.crop_rect = QRect()
        self.dragging = False
        self.drag_start = None
        self.crop_start_rect = None
        self.display_x = 0
        self.display_y = 0
        self.display_w = 0
        self.display_h = 0
        self.scale = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # 顶部信息栏
        info_layout = QHBoxLayout()
        self.info_label = QLabel(f"原图: {self.original_pixmap.width()}x{self.original_pixmap.height()} | 目标比例: {target_w}:{target_h}")
        self.info_label.setFont(QFont("Microsoft YaHei", 12))
        self.info_label.setStyleSheet("color: #8a8a8a;")
        info_layout.addWidget(self.info_label)
        info_layout.addStretch()

        self.hint_label = QLabel("拖动选框调整位置 | 滚轮缩放选框 | 双击确认")
        self.hint_label.setFont(QFont("Microsoft YaHei", 11))
        self.hint_label.setStyleSheet("color: #666666;")
        info_layout.addWidget(self.hint_label)
        layout.addLayout(info_layout)

        # 图片显示区域
        self.image_widget = QWidget(self)
        self.image_widget.setStyleSheet("background: #1a2330; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;")
        self.image_widget.setMinimumSize(600, 400)
        self.image_widget.setMouseTracking(True)
        self.image_widget.paintEvent = self._paint_image
        self.image_widget.mousePressEvent = self._mouse_press
        self.image_widget.mouseMoveEvent = self._mouse_move
        self.image_widget.mouseReleaseEvent = self._mouse_release
        self.image_widget.wheelEvent = self._wheel_event
        self.image_widget.mouseDoubleClickEvent = self._double_click
        layout.addWidget(self.image_widget, stretch=1)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.reset_btn = QPushButton("重置")
        self.reset_btn.setFixedSize(100, 36)
        self.reset_btn.setFont(QFont("Microsoft YaHei", 11))
        self.reset_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: white; border: 1px solid rgba(255,255,255,0.15); border-radius: 4px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.15); }"
        )
        self.reset_btn.clicked.connect(self._reset_crop)
        btn_layout.addWidget(self.reset_btn)

        btn_layout.addSpacing(20)

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(100, 36)
        cancel_btn.setFont(QFont("Microsoft YaHei", 11))
        cancel_btn.setStyleSheet(self.reset_btn.styleSheet())
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("确认裁剪")
        ok_btn.setFixedSize(120, 36)
        ok_btn.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        ok_btn.setStyleSheet(
            "QPushButton { background: #1a9fff; color: white; border: none; border-radius: 4px; }"
            "QPushButton:hover { background: #66c0f4; }"
        )
        ok_btn.clicked.connect(self._confirm_crop)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

        # 初始化裁剪区域
        self._init_crop_rect()

        # 最后显示窗口（避免在属性初始化前触发paint事件）
        self.showMaximized()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._init_crop_rect()

    def _init_crop_rect(self):
        if self.original_pixmap.isNull():
            return
        widget_w = self.image_widget.width()
        widget_h = self.image_widget.height()
        if widget_w <= 0 or widget_h <= 0:
            return
        img_w = self.original_pixmap.width()
        img_h = self.original_pixmap.height()

        # 计算图片在widget中的显示区域（居中，保持比例）
        display_ratio = widget_w / widget_h
        img_ratio = img_w / img_h
        if img_ratio > display_ratio:
            self.display_w = widget_w
            self.display_h = int(widget_w / img_ratio)
            self.display_x = 0
            self.display_y = (widget_h - self.display_h) // 2
        else:
            self.display_h = widget_h
            self.display_w = int(widget_h * img_ratio)
            self.display_y = 0
            self.display_x = (widget_w - self.display_w) // 2

        self.scale = self.display_w / img_w

        # 初始化裁剪区域为居中最大区域
        if self.target_ratio > img_w / img_h:
            # 目标更宽，按图片宽度计算
            cw = int(img_w * 0.9)
            ch = int(cw / self.target_ratio)
        else:
            # 目标更高，按图片高度计算
            ch = int(img_h * 0.9)
            cw = int(ch * self.target_ratio)

        cx = (img_w - cw) // 2
        cy = (img_h - ch) // 2
        self.crop_rect = QRect(cx, cy, cw, ch)
        self.image_widget.update()

    def _img_to_widget(self, ix: int, iy: int) -> QPoint:
        if self.scale <= 0:
            return QPoint(self.display_x, self.display_y)
        return QPoint(self.display_x + int(ix * self.scale), self.display_y + int(iy * self.scale))

    def _widget_to_img(self, wx: int, wy: int) -> QPoint:
        if self.scale <= 0:
            return QPoint(0, 0)
        return QPoint(int((wx - self.display_x) / self.scale), int((wy - self.display_y) / self.scale))

    def _paint_image(self, event):
        painter = QPainter(self.image_widget)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if self.original_pixmap.isNull() or self.display_w <= 0 or self.display_h <= 0:
            return

        # 绘制原图
        painter.drawPixmap(self.display_x, self.display_y, self.display_w, self.display_h, self.original_pixmap)

        if not self.crop_rect.isValid():
            return

        # 绘制暗色遮罩（裁剪区域外）
        widget_rect = self.image_widget.rect()
        p1 = self._img_to_widget(self.crop_rect.left(), self.crop_rect.top())
        p2 = self._img_to_widget(self.crop_rect.right(), self.crop_rect.bottom())
        crop_widget_rect = QRect(p1.x(), p1.y(), p2.x() - p1.x(), p2.y() - p1.y())

        # 四个暗色区域
        painter.fillRect(QRect(0, 0, widget_rect.width(), crop_widget_rect.top()), QColor(0, 0, 0, 160))
        painter.fillRect(QRect(0, crop_widget_rect.bottom(), widget_rect.width(), widget_rect.height() - crop_widget_rect.bottom()), QColor(0, 0, 0, 160))
        painter.fillRect(QRect(0, crop_widget_rect.top(), crop_widget_rect.left(), crop_widget_rect.height()), QColor(0, 0, 0, 160))
        painter.fillRect(QRect(crop_widget_rect.right(), crop_widget_rect.top(), widget_rect.width() - crop_widget_rect.right(), crop_widget_rect.height()), QColor(0, 0, 0, 160))

        # 绘制裁剪边框
        pen = QPen(QColor(26, 159, 255, 220))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(crop_widget_rect)

        # 绘制九宫格辅助线
        cw = crop_widget_rect.width()
        ch = crop_widget_rect.height()
        painter.setPen(QPen(QColor(26, 159, 255, 80), 1))
        painter.drawLine(crop_widget_rect.left() + cw // 3, crop_widget_rect.top(), crop_widget_rect.left() + cw // 3, crop_widget_rect.bottom())
        painter.drawLine(crop_widget_rect.left() + 2 * cw // 3, crop_widget_rect.top(), crop_widget_rect.left() + 2 * cw // 3, crop_widget_rect.bottom())
        painter.drawLine(crop_widget_rect.left(), crop_widget_rect.top() + ch // 3, crop_widget_rect.right(), crop_widget_rect.top() + ch // 3)
        painter.drawLine(crop_widget_rect.left(), crop_widget_rect.top() + 2 * ch // 3, crop_widget_rect.right(), crop_widget_rect.top() + 2 * ch // 3)

    def _mouse_press(self, event):
        if not self.crop_rect.isValid():
            return
        p = self._widget_to_img(int(event.position().x()), int(event.position().y()))
        if self.crop_rect.contains(p):
            self.dragging = True
            self.drag_start = p
            self.crop_start_rect = QRect(self.crop_rect)

    def _mouse_move(self, event):
        if not self.dragging or not self.crop_rect.isValid():
            return
        p = self._widget_to_img(int(event.position().x()), int(event.position().y()))
        dx = p.x() - self.drag_start.x()
        dy = p.y() - self.drag_start.y()

        new_x = self.crop_start_rect.x() + dx
        new_y = self.crop_start_rect.y() + dy
        new_w = self.crop_start_rect.width()
        new_h = self.crop_start_rect.height()

        # 限制在图片范围内
        img_w = self.original_pixmap.width()
        img_h = self.original_pixmap.height()
        new_x = max(0, min(new_x, img_w - new_w))
        new_y = max(0, min(new_y, img_h - new_h))

        self.crop_rect = QRect(new_x, new_y, new_w, new_h)
        self.image_widget.update()

    def _mouse_release(self, event):
        self.dragging = False
        self.drag_start = None
        self.crop_start_rect = None

    def _wheel_event(self, event: QWheelEvent):
        if not self.crop_rect.isValid():
            return
        delta = event.angleDelta().y()
        step = 0.05
        if delta > 0:
            factor = 1 + step
        else:
            factor = 1 - step

        cx = self.crop_rect.center().x()
        cy = self.crop_rect.center().y()
        new_w = int(self.crop_rect.width() * factor)
        new_h = int(new_w / self.target_ratio)

        img_w = self.original_pixmap.width()
        img_h = self.original_pixmap.height()

        # 限制最小尺寸
        min_w = int(img_w * 0.1)
        min_h = int(min_w / self.target_ratio)
        if new_w < min_w or new_h < min_h:
            return

        new_x = cx - new_w // 2
        new_y = cy - new_h // 2

        # 限制在图片范围内
        new_x = max(0, min(new_x, img_w - new_w))
        new_y = max(0, min(new_y, img_h - new_h))

        # 如果超出边界，调整大小
        if new_x + new_w > img_w:
            new_w = img_w - new_x
            new_h = int(new_w / self.target_ratio)
        if new_y + new_h > img_h:
            new_h = img_h - new_y
            new_w = int(new_h * self.target_ratio)

        self.crop_rect = QRect(new_x, new_y, new_w, new_h)
        self.image_widget.update()

    def _double_click(self, event):
        self._confirm_crop()

    def _reset_crop(self):
        self._init_crop_rect()

    def _confirm_crop(self):
        if not self.crop_rect.isValid() or self.original_pixmap.isNull():
            self.reject()
            return
        self.accept()

    def get_cropped_pixmap(self) -> QPixmap:
        if not self.crop_rect.isValid() or self.original_pixmap.isNull():
            return QPixmap()
        return self.original_pixmap.copy(self.crop_rect)


# ============================================================================
# CG 大图查看器
# ============================================================================
class CGViewerDialog(QDialog):
    def __init__(self, cg_files: List[Path], current_index: int, game_name: str = "", parent=None):
        super().__init__(parent)
        self.cg_files = cg_files
        self.current_index = current_index
        self.game_name = game_name
        self.setWindowTitle(f"CG 鉴赏 - {game_name}")
        self.setMinimumSize(800, 600)
        self.showMaximized()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setStyleSheet("background-color: #0a0a0a;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QWidget(self)
        self.header.setFixedHeight(50)
        self.header.setStyleSheet("background: rgba(0,0,0,0.7);")
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(20, 0, 20, 0)

        self.filename_label = QLabel(self._current_name(), self.header)
        self.filename_label.setFont(QFont("Microsoft YaHei", 12))
        self.filename_label.setStyleSheet("color: #aaaaaa;")
        header_layout.addWidget(self.filename_label)

        self.count_label = QLabel(self._count_text(), self.header)
        self.count_label.setFont(QFont("Microsoft YaHei", 11))
        self.count_label.setStyleSheet("color: #666666;")
        header_layout.addWidget(self.count_label)
        header_layout.addStretch()

        self.prev_btn = QPushButton("◀ 上一张", self.header)
        self.prev_btn.setFixedSize(90, 32)
        self.prev_btn.setFont(QFont("Microsoft YaHei", 11))
        self.prev_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: white; "
            "border: 1px solid rgba(255,255,255,0.2); border-radius: 4px; }"
            "QPushButton:hover { background: rgba(26,159,255,0.3); border: 1px solid #1a9fff; }"
            "QPushButton:disabled { background: rgba(255,255,255,0.05); color: #666666; border: 1px solid rgba(255,255,255,0.1); }"
        )
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.clicked.connect(self._show_prev)
        header_layout.addWidget(self.prev_btn)

        header_layout.addSpacing(10)

        self.next_btn = QPushButton("下一张 ▶", self.header)
        self.next_btn.setFixedSize(90, 32)
        self.next_btn.setFont(QFont("Microsoft YaHei", 11))
        self.next_btn.setStyleSheet(self.prev_btn.styleSheet())
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.clicked.connect(self._show_next)
        header_layout.addWidget(self.next_btn)

        header_layout.addSpacing(20)

        self.close_btn = QPushButton("关闭", self.header)
        self.close_btn.setFixedSize(80, 32)
        self.close_btn.setFont(QFont("Microsoft YaHei", 11))
        self.close_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: white; "
            "border: 1px solid rgba(255,255,255,0.2); border-radius: 4px; }"
            "QPushButton:hover { background: rgba(255,100,100,0.3); border: 1px solid #ff6666; }"
        )
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        header_layout.addWidget(self.close_btn)

        layout.addWidget(self.header)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.image_label = QLabel(self.scroll_area)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background: transparent;")
        self.scroll_area.setWidget(self.image_label)

        layout.addWidget(self.scroll_area)

        self.footer = QWidget(self)
        self.footer.setFixedHeight(40)
        self.footer.setStyleSheet("background: rgba(0,0,0,0.7);")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(20, 0, 20, 0)

        hint = QLabel("点击任意位置关闭 | ESC 关闭 | ← → 切换图片", self.footer)
        hint.setFont(QFont("Microsoft YaHei", 10))
        hint.setStyleSheet("color: #666666;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addWidget(hint)

        layout.addWidget(self.footer)

        self._update_nav_buttons()
        self._load_image()
        self.scroll_area.viewport().installEventFilter(self)

    def _current_name(self):
        if 0 <= self.current_index < len(self.cg_files):
            return Path(self.cg_files[self.current_index]).name
        return ""

    def _count_text(self):
        return f"({self.current_index + 1} / {len(self.cg_files)})"

    def _update_nav_buttons(self):
        self.prev_btn.setEnabled(self.current_index > 0)
        self.next_btn.setEnabled(self.current_index < len(self.cg_files) - 1)
        self.filename_label.setText(self._current_name())
        self.count_label.setText(self._count_text())

    def _show_prev(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._update_nav_buttons()
            self._load_image()

    def _show_next(self):
        if self.current_index < len(self.cg_files) - 1:
            self.current_index += 1
            self._update_nav_buttons()
            self._load_image()

    def _load_image(self):
        if not (0 <= self.current_index < len(self.cg_files)):
            return
        image_path = str(self.cg_files[self.current_index])
        self.image_path = image_path

        def _on_loaded(path: str, pixmap: QPixmap):
            if path == self.image_path:
                self.original_pixmap = pixmap
                self._fit_to_window()

        _image_loader.load_once(self.image_path, QSize(), _on_loaded)

    def _fit_to_window(self):
        if hasattr(self, 'original_pixmap') and not self.original_pixmap.isNull():
            avail = self.scroll_area.viewport().size()
            scaled = self.original_pixmap.scaled(avail,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.image_label.setPixmap(scaled)
            self.image_label.setFixedSize(scaled.size())

    def wheelEvent(self, event: QWheelEvent):
        delta_y = event.angleDelta().y()
        if delta_y > 0:
            self._show_prev()
        elif delta_y < 0:
            self._show_next()
        event.accept()

    def eventFilter(self, obj, event):
        if obj == self.scroll_area.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            self.close()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_to_window()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_Left:
            self._show_prev()
        elif event.key() == Qt.Key.Key_Right:
            self._show_next()
        else:
            super().keyPressEvent(event)


# ============================================================================
# 游戏卡片
# ============================================================================
class GameCard(QWidget):
    clicked = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    refresh_requested = pyqtSignal()
    favorite_toggled = pyqtSignal(object)
    drag_started = pyqtSignal(object)

    def __init__(self, game: GameInfo, parent=None):
        super().__init__(parent)
        self.game = game
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self._scale = 1.0
        self._glow = 0
        self._drag_start_pos = None

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)

        self._load_images()

    def _load_images(self):
        """加载游戏图片 - 自适应居中裁剪"""
        if self.game.cover and self.game.cover.exists():
            _image_loader.load_once(str(self.game.cover), QSize(), self._on_image_loaded)
        elif self.game.wallpaper and self.game.wallpaper.exists():
            _image_loader.load_once(str(self.game.wallpaper), QSize(), self._on_image_loaded)

    def _on_image_loaded(self, path: str, pixmap: QPixmap):
        if self.game.cover and str(self.game.cover) == path:
            self.game.cover_pixmap = pixmap
        elif self.game.wallpaper and str(self.game.wallpaper) == path:
            self.game.wallpaper_pixmap = pixmap
        self.update()

    def _on_anim_value(self, value):
        self._scale = 1.0 + value * 0.06
        self._glow = int(value * 100)
        self.update()

    def enterEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._anim.currentValue() if self._anim.currentValue() is not None else 1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag_start_pos is None:
            return
        # 判断移动距离是否超过阈值，避免误触发
        distance = (event.pos() - self._drag_start_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            return
        # 启动拖拽
        drag = QDrag(self)
        mime_data = QMimeData()
        # 使用游戏路径作为拖拽标识
        key = GameMetadata._game_key(self.game)
        mime_data.setText(key)
        drag.setMimeData(mime_data)
        # 创建拖拽时的缩略图
        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.pos())
        self.drag_started.emit(self.game)
        drag.exec(Qt.DropAction.MoveAction)
        self._drag_start_pos = None

    def mouseReleaseEvent(self, event):
        self._drag_start_pos = None
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.game)
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.pos())
        super().mouseReleaseEvent(event)

    def _show_context_menu(self, pos):
        """右键菜单 - 不使用样式表避免崩溃"""
        menu = QMenu(self)
        # 不设置样式表，使用系统默认样式

        has_cover = self.game.cover and self.game.cover.exists()
        has_wallpaper = self.game.wallpaper and self.game.wallpaper.exists()
        has_exe = self.game.exe_path and Path(self.game.exe_path).exists()

        if not has_cover:
            add_cover = menu.addAction("设置封面")
        else:
            add_cover = menu.addAction("更换封面")

        if not has_wallpaper:
            add_wallpaper = menu.addAction("设置壁纸")
        else:
            add_wallpaper = menu.addAction("更换壁纸")

        add_cg = menu.addAction("添加CG...")
        # 移动到分类
        move_action = menu.addAction("移动到分类...")

        menu.addSeparator()

        # 收藏切换
        fav_action = menu.addAction("取消收藏" if self.game.favorite else "收藏游戏")

        # exe 设置
        if not has_exe:
            set_exe = menu.addAction("设置启动程序...")
        else:
            set_exe = menu.addAction("更换启动程序...")
            clear_exe = menu.addAction("清除启动程序")

        menu.addSeparator()
        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除游戏")

        action = menu.exec(self.mapToGlobal(pos))

        if action == add_cover:
            self._add_image("cover")
        elif action == add_wallpaper:
            self._add_image("wallpaper")
        elif action == add_cg:
            self._add_cg()
        elif action == fav_action:
            self._toggle_favorite()
        elif action == set_exe:
            self._set_exe()
        elif 'clear_exe' in locals() and action == clear_exe:
            self._clear_exe()
        elif action == move_action:
            self._move_to_category()
        elif action == rename_action:
            self._rename_game()
        elif action == delete_action:
            self.delete_requested.emit(self.game)

    def _toggle_favorite(self):
        self.game.favorite = not self.game.favorite
        GameMetadata.set(self.game, favorite=self.game.favorite)
        self.favorite_toggled.emit(self.game)
        self.update()

    def _set_exe(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择游戏启动程序", "",
            "可执行文件 (*.exe);;所有文件 (*.*)",
            options=FILE_DIALOG_OPTIONS
        )
        if not file_path:
            return
        self.game.exe_path = file_path
        GameMetadata.set(self.game, exe_path=file_path)
        self._load_exe_icon()
        self.update()
        QMessageBox.information(self, "成功", "启动程序已设置")
        self.refresh_requested.emit()

    def _clear_exe(self):
        self.game.exe_path = None
        self.game.exe_pixmap = None
        GameMetadata.set(self.game, exe_path=None)
        self.update()
        self.refresh_requested.emit()

    def _move_to_category(self):
        """移动游戏到其他分类"""
        cats = get_categories()
        if not cats:
            QMessageBox.warning(self, "提示", "没有其他分类可用")
            return

        # 构建分类列表（排除当前分类）
        current_key = f"{self.game.category}/{self.game.sub}"
        items = []
        for big, subs in cats.items():
            for sub in subs:
                key = f"{big}/{sub}"
                if key != current_key:
                    items.append((f"{big} / {sub}", (big, sub)))

        if not items:
            QMessageBox.warning(self, "提示", "没有其他分类可用")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("移动到分类")
        dlg.setFixedSize(350, 180)
        dlg.setStyleSheet("QDialog { background-color: #1a2330; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; }")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("选择目标分类")
        title.setFont(QFont("Microsoft YaHei", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: white;")
        layout.addWidget(title)

        combo = QComboBox()
        combo.setStyleSheet(
            "QComboBox { background: rgba(255,255,255,0.05); color: white; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; padding: 8px 12px; font-size: 13px; }"
            "QComboBox:focus { border: 1px solid #1a9fff; }"
            "QComboBox::drop-down { border: none; width: 30px; }"
            "QComboBox QAbstractItemView { background: #1a2330; color: white; border: 1px solid rgba(255,255,255,0.1); selection-background-color: rgba(26,159,255,0.3); }"
        )
        for label, data in items:
            combo.addItem(label, data)
        layout.addWidget(combo)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel = QPushButton("取消")
        cancel.setFixedSize(80, 36)
        cancel.setStyleSheet("QPushButton { background: #333; color: white; border: none; border-radius: 4px; font-size: 13px; } QPushButton:hover { background: #333dd; }")
        cancel.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel)
        ok = QPushButton("移动")
        ok.setFixedSize(80, 36)
        ok.setStyleSheet("QPushButton { background: #1a9fff; color: white; border: none; border-radius: 4px; font-size: 13px; } QPushButton:hover { background: #1a9fffdd; }")
        ok.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok)
        layout.addLayout(btn_layout)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        data = combo.currentData()
        if not data:
            return
        target_big, target_sub = data

        # 执行移动
        src_path = self.game.path
        dst_path = ROOT_PATH / target_big / target_sub / self.game.name

        if dst_path.exists():
            QMessageBox.warning(self, "提示", f"目标分类中已存在 [{self.game.name}]")
            return

        try:
            # 创建目标目录
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            # 移动文件夹
            shutil.move(str(src_path), str(dst_path))
            # 更新元数据中的分类信息
            old_key = GameMetadata._game_key(self.game)
            # 更新 GameInfo
            self.game.category = target_big
            self.game.sub = target_sub
            self.game.path = dst_path
            # 更新封面/壁纸/cg路径
            if self.game.cover:
                self.game.cover = dst_path / self.game.cover.name
            if self.game.wallpaper:
                self.game.wallpaper = dst_path / self.game.wallpaper.name
            cg_dir = dst_path / "cg"
            if cg_dir.exists():
                self.game.cg_files = []
                for f in sorted(cg_dir.iterdir()):
                    if f.suffix.lower() in SUPPORTED_EXTS:
                        self.game.cg_files.append(f)
            # 迁移元数据
            meta = GameMetadata._data.pop(old_key, {})
            new_key = GameMetadata._game_key(self.game)
            GameMetadata._data[new_key] = meta
            GameMetadata.save()
            QMessageBox.information(self, "成功", f"[{self.game.name}] 已移动到 {target_big}/{target_sub}")
            self.refresh_requested.emit()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"移动失败: {e}")

    def _rename_game(self):
        """重命名游戏文件夹和元数据"""
        old_name = self.game.name
        new_name, ok = QInputDialog.getText(
            self, "重命名游戏", "请输入新名称：", text=old_name
        )
        if not ok or not new_name:
            return
        new_name = new_name.strip()
        if new_name == old_name:
            return

        invalid_chars = '\\/:*?"<>|'
        if any(c in new_name for c in invalid_chars):
            QMessageBox.warning(
                self, "提示",
                f"名称不能包含以下字符：{invalid_chars}"
            )
            return

        dst_path = self.game.path.parent / new_name
        if dst_path.exists():
            QMessageBox.warning(self, "提示", f"该分类下已存在 [{new_name}]")
            return

        try:
            old_path = self.game.path
            old_path_str = str(old_path)

            # 移动文件夹
            shutil.move(old_path_str, str(dst_path))

            # 更新 exe_path（如果指向原游戏文件夹内部）
            if self.game.exe_path and self.game.exe_path.startswith(old_path_str + os.sep):
                self.game.exe_path = str(dst_path) + self.game.exe_path[len(old_path_str):]
                self.game.exe_pixmap = None
                self._load_exe_icon()

            # 更新 GameInfo 路径相关字段
            self.game.name = new_name
            self.game.path = dst_path

            def _rebase(p: Optional[Path]) -> Optional[Path]:
                if not p:
                    return None
                try:
                    rel = p.relative_to(old_path)
                    return dst_path / rel
                except ValueError:
                    return p

            self.game.cover = _rebase(self.game.cover)
            self.game.wallpaper = _rebase(self.game.wallpaper)
            self.game.cg_files = [_rebase(p) for p in self.game.cg_files if p]

            # 迁移元数据 key
            old_key = f"{self.game.category}/{self.game.sub}/{old_name}"
            new_key = GameMetadata._game_key(self.game)
            meta = GameMetadata._data.pop(old_key, {})
            meta['exe_path'] = self.game.exe_path
            GameMetadata._data[new_key] = meta
            GameMetadata.save()

            QMessageBox.information(
                self, "成功",
                f"[{old_name}] 已重命名为 [{new_name}]"
            )
            self.refresh_requested.emit()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"重命名失败: {e}")

    def _load_exe_icon(self):
        """加载exe图标"""
        if self.game.exe_path and Path(self.game.exe_path).exists():
            try:
                icon_provider = QFileIconProvider()
                icon_provider.setOptions(QFileIconProvider.Option.DontUseCustomDirectoryIcons)
                file_info = QFileInfo(self.game.exe_path)
                icon = icon_provider.icon(file_info)
                if not icon.isNull():
                    pixmap = icon.pixmap(QSize(24, 24))
                    self.game.exe_pixmap = pixmap
            except Exception as e:
                print(f"加载exe图标失败: {e}")

    def _add_image(self, img_type: str):
        """添加/更换封面或壁纸，支持裁剪调整"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, f"选择{img_type}图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp)",
            options=FILE_DIALOG_OPTIONS
        )
        if not file_path:
            return

        # 如果选择的是同一个文件，不做任何操作
        ext = Path(file_path).suffix
        dst = self.game.path / f"{img_type}{ext}"
        if Path(file_path).resolve() == dst.resolve():
            QMessageBox.information(self, "提示", "选择的图片已经是当前文件")
            return

        # 打开裁剪对话框
        target_w = CARD_W if img_type == "cover" else 1920
        target_h = CARD_H if img_type == "cover" else 1080
        crop_dlg = ImageCropDialog(file_path, target_w, target_h, f"裁剪{img_type}", self)
        if crop_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        cropped = crop_dlg.get_cropped_pixmap()
        if cropped.isNull():
            QMessageBox.critical(self, "错误", "裁剪失败")
            return

        # 删除旧文件（如果存在且不是新文件）
        if img_type == "cover" and self.game.cover:
            old = self.game.path / f"cover{self.game.cover.suffix}"
            if old.exists() and old.resolve() != dst.resolve():
                old.unlink()
        elif img_type == "wallpaper" and self.game.wallpaper:
            old = self.game.path / f"wallpaper{self.game.wallpaper.suffix}"
            if old.exists() and old.resolve() != dst.resolve():
                old.unlink()

        # 保存裁剪后的图片
        try:
            cropped.save(str(dst))
            # 更新路径
            if img_type == "cover":
                self.game.cover = dst
                self.game.cover_pixmap = None
            else:
                self.game.wallpaper = dst
                self.game.wallpaper_pixmap = None

            # 重新加载并显示
            self._load_images()
            self.update()

            QMessageBox.information(self, "成功", f"{img_type}已更新")
            # 通知父窗口刷新
            self.refresh_requested.emit()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败: {e}")

    def _add_cg(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择CG图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp)",
            options=FILE_DIALOG_OPTIONS
        )
        if not files:
            return

        cg_dir = self.game.path / "cg"
        cg_dir.mkdir(exist_ok=True)
        success = 0
        for f in files:
            dst = cg_dir / Path(f).name
            if copy_image(f, dst):
                success += 1

        # 重新扫描CG文件
        self.game.cg_files = []
        if cg_dir.exists():
            for f in sorted(cg_dir.iterdir()):
                if f.suffix.lower() in SUPPORTED_EXTS:
                    self.game.cg_files.append(f)

        QMessageBox.information(self, "成功", f"已添加 {success}/{len(files)} 张CG")
        self.refresh_requested.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = int(CARD_W * self._scale)
        h = int(CARD_H * self._scale)
        x = (CARD_W - w) // 2
        y = (CARD_H - h) // 2 - int(self._scale * 4)

        if self._glow > 0:
            glow_path = QPainterPath()
            glow_path.addRoundedRect(x-3, y-3, w+6, h+6, CARD_RADIUS+3, CARD_RADIUS+3)
            painter.fillPath(glow_path, QColor(COL_ACCENT_GLOW.red(), COL_ACCENT_GLOW.green(),
                                               COL_ACCENT_GLOW.blue(), self._glow))

        shadow = QPainterPath()
        shadow.addRoundedRect(x+4, y+8, w, h, CARD_RADIUS, CARD_RADIUS)
        painter.fillPath(shadow, QColor(0, 0, 0, 120 + int(self._scale * 40)))

        clip = QPainterPath()
        clip.addRoundedRect(x, y, w, h, CARD_RADIUS, CARD_RADIUS)

        pixmap = self.game.cover_pixmap or self.game.wallpaper_pixmap
        if pixmap and not pixmap.isNull():
            painter.setClipPath(clip)
            # 自适应居中裁剪：保持比例，不拉伸，居中显示
            pw = pixmap.width()
            ph = pixmap.height()
            card_ratio = w / h
            img_ratio = pw / ph
            if img_ratio > card_ratio:
                # 图片更宽，按高度缩放，裁剪左右
                sh = int(h)
                sw = int(pw * h / ph)
                sx = x + (w - sw) // 2
                sy = y
            else:
                # 图片更高，按宽度缩放，裁剪上下
                sw = int(w)
                sh = int(ph * w / pw)
                sx = x
                sy = y + (h - sh) // 2
            painter.drawPixmap(sx, sy, sw, sh, pixmap)
            painter.setClipping(False)
        else:
            painter.fillPath(clip, COL_BG_LIGHT)
            painter.setPen(QColor(255, 255, 255, 40))
            painter.setFont(QFont("Microsoft YaHei", 36))
            icon_rect = QRect(x, y, w, h - 60)
            painter.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, "🎮")
            painter.setPen(COL_TEXT_DIM)
            painter.setFont(QFont("Microsoft YaHei", 11))
            hint_rect = QRect(x, y + h//2 - 10, w, 40)
            painter.drawText(hint_rect, Qt.AlignmentFlag.AlignCenter, "右键设置封面")

        grad = QLinearGradient(0, y+h-70, 0, y+h)
        grad.setColorAt(0, QColor(0, 0, 0, 0))
        grad.setColorAt(0.5, QColor(0, 0, 0, 180))
        grad.setColorAt(1, QColor(0, 0, 0, 240))
        painter.fillPath(clip, grad)

        painter.setPen(COL_TEXT)
        font = QFont("Microsoft YaHei", 12, QFont.Weight.Bold)
        painter.setFont(font)
        text_rect = QRect(x+12, y+h-42, w-24, 24)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        self.game.name)

        painter.setPen(COL_TEXT_DIM)
        painter.setFont(QFont("Microsoft YaHei", 8))
        cat_rect = QRect(x+12, y+h-20, w-24, 16)
        painter.drawText(cat_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        f"{self.game.category} / {self.game.sub}")

        pen = QPen(QColor(255, 255, 255, 25 + int(self._glow * 0.5)))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawPath(clip)

        # 收藏星形图标
        if self.game.favorite:
            star_path = QPainterPath()
            star_x = x + w - 32
            star_y = y + 8
            painter.fillPath(star_path, QColor(255, 200, 0, 220))
            painter.setPen(QColor(255, 170, 0, 255))
            painter.setFont(QFont("Microsoft YaHei", 14, QFont.Weight.Bold))
            painter.drawText(QRect(star_x, star_y, 24, 24), Qt.AlignmentFlag.AlignCenter, "★")

        # exe 启动图标
        if self.game.exe_path and Path(self.game.exe_path).exists():
            if self.game.exe_pixmap and not self.game.exe_pixmap.isNull():
                exe_x = x + 12
                exe_y = y + 10
                painter.drawPixmap(exe_x, exe_y, 20, 20, self.game.exe_pixmap)
            else:
                self._load_exe_icon()
                if self.game.exe_pixmap and not self.game.exe_pixmap.isNull():
                    painter.drawPixmap(x + 12, y + 10, 20, 20, self.game.exe_pixmap)

        if not (self.game.cover and self.game.cover.exists()):
            badge_path = QPainterPath()
            badge_path.addRoundedRect(x+w-80, y+10, 70, 22, 4, 4)
            painter.fillPath(badge_path, QColor(255, 170, 68, 180))
            painter.setPen(QColor(0, 0, 0, 200))
            painter.setFont(QFont("Microsoft YaHei", 8, QFont.Weight.Bold))
            painter.drawText(QRect(x+w-80, y+10, 70, 22),
                           Qt.AlignmentFlag.AlignCenter, "无封面")


# ============================================================================
# CG 缩略图
# ============================================================================
class CGThumb(QWidget):
    clicked = pyqtSignal(str, str)

    def __init__(self, path: Path, game_name: str, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.game_name = game_name
        self.setFixedSize(100, 124)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pixmap: Optional[QPixmap] = None
        self._hover = False

        # 加载原始图片，在 paintEvent 中按需高质量缩放，避免二次模糊
        _image_loader.load_once(self.path, QSize(), self._on_loaded)

    def _on_loaded(self, path: str, pixmap: QPixmap):
        if path == self.path:
            self.pixmap = pixmap
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.path, self.game_name)
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        r = 6 if not self._hover else 8
        path = QPainterPath()
        path.addRoundedRect(0, 0, 100, 100, r, r)

        if self.pixmap and not self.pixmap.isNull():
            painter.setClipPath(path)
            # 按比例缩放，居中裁剪
            pw = self.pixmap.width()
            ph = self.pixmap.height()
            scale = max(100 / pw, 100 / ph)
            sw = int(pw * scale)
            sh = int(ph * scale)
            sx = (100 - sw) // 2
            sy = (100 - sh) // 2
            painter.drawPixmap(sx, sy, sw, sh, self.pixmap)
            painter.setClipping(False)
        else:
            painter.fillPath(path, COL_BG_LIGHT)
            painter.setPen(COL_TEXT_DIM)
            painter.setFont(QFont("Microsoft YaHei", 12))
            painter.drawText(QRect(0, 0, 100, 100), Qt.AlignmentFlag.AlignCenter, "?")

        if self._hover:
            pen = QPen(COL_ACCENT_GLOW)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawPath(path)
        else:
            pen = QPen(QColor(255, 255, 255, 40))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawPath(path)

        painter.setPen(COL_TEXT_DIM if not self._hover else COL_ACCENT_GLOW)
        painter.setFont(QFont("Microsoft YaHei", 8))
        fname = Path(self.path).name
        if len(fname) > 12:
            fname = fname[:9] + "..."
        painter.drawText(QRect(0, 104, 100, 18),
                        Qt.AlignmentFlag.AlignCenter, fname)


# ============================================================================
# 横向滚动行
# ============================================================================
class GameRow(QWidget):
    game_selected = pyqtSignal(object)
    game_delete = pyqtSignal(object)
    refresh_requested = pyqtSignal()
    game_reordered = pyqtSignal(str, str, str)  # source_key, target_key, row_title
    game_moved_to_category = pyqtSignal(object, str, str)  # game, target_category, target_sub

    def __init__(self, title: str, games: List[GameInfo], parent=None):
        super().__init__(parent)
        self.setFixedHeight(CARD_H + ROW_TITLE_H + 30)
        self.title = title
        self._games = games[:]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 10, 40, 10)
        layout.setSpacing(12)

        self.title_label = QLabel(title)
        self.title_label.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: white; padding-left: 4px;")
        self.title_label.setCursor(Qt.CursorShape.OpenHandCursor)
        layout.addWidget(self.title_label)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background: transparent; border: none;")
        self.scroll.viewport().setStyleSheet("background: transparent;")
        self.scroll.setAcceptDrops(True)
        self.scroll.viewport().setAcceptDrops(True)

        container = QWidget(self.scroll)
        container.setAcceptDrops(True)
        h_layout = QHBoxLayout(container)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(CARD_MARGIN)
        h_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        for game in games:
            card = GameCard(game, container)
            card.clicked.connect(self.game_selected.emit)
            card.delete_requested.connect(self.game_delete.emit)
            card.refresh_requested.connect(self.refresh_requested.emit)
            card.favorite_toggled.connect(self._on_favorite_toggled)
            card.drag_started.connect(self._on_card_drag_started)
            h_layout.addWidget(card)

        h_layout.addStretch()
        self.scroll.setWidget(container)
        layout.addWidget(self.scroll)

        self.scroll.wheelEvent = self._wheel_event

        # 标题拖拽支持
        self.title_label.setMouseTracking(True)
        self.title_label.mousePressEvent = self._title_mouse_press
        self.title_label.mouseMoveEvent = self._title_mouse_move
        self._title_drag_start = None

    def _on_card_drag_started(self, game):
        pass

    def _title_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._title_drag_start = event.pos()

    def _title_mouse_move(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._title_drag_start is None:
            return
        distance = (event.pos() - self._title_drag_start).manhattanLength()
        if distance < QApplication.startDragDistance():
            return
        drag = QDrag(self.title_label)
        mime_data = QMimeData()
        mime_data.setText(f"ROW:{self.title}")
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.MoveAction)
        self._title_drag_start = None

    def _on_favorite_toggled(self, game):
        self.refresh_requested.emit()

    def _wheel_event(self, event: QWheelEvent):
        delta_x = event.angleDelta().x()
        delta_y = event.angleDelta().y()

        if abs(delta_x) > abs(delta_y):
            self.scroll.horizontalScrollBar().setValue(
                self.scroll.horizontalScrollBar().value() - delta_x
            )
            event.accept()
        else:
            event.ignore()

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        text = event.mimeData().text()
        if not text:
            event.ignore()
            return
        # 处理行标题拖拽（跨行移动游戏到另一分类）
        if text.startswith("ROW:"):
            event.ignore()
            return
        # 处理卡片拖拽
        source_key = text
        # 使用 position() 代替 pos() (PyQt6 兼容性)
        target_pos = event.position().toPoint()
        # 找到目标位置对应的卡片
        container = self.scroll.widget()
        if container:
            local_pos = container.mapFrom(self, target_pos)
            cards = [c for c in container.findChildren(GameCard) if c.parent() == container]
            insert_index = len(cards)
            for i, card in enumerate(cards):
                card_geo = card.geometry()
                if local_pos.x() < card_geo.center().x():
                    insert_index = i
                    break
            # 找到源卡片
            source_card = None
            for card in cards:
                if GameMetadata._game_key(card.game) == source_key:
                    source_card = card
                    break
            if source_card and source_card in cards:
                # 源卡片在当前行，执行行内排序
                source_idx = cards.index(source_card)
                if source_idx != insert_index and insert_index != source_idx + 1:
                    self._reorder_games(source_idx, insert_index)
                    event.acceptProposedAction()
                    return
            elif source_card:
                # 源卡片不在当前行，跨行移动
                # 解析当前行的分类
                if " / " in self.title:
                    parts = self.title.split(" / ")
                    target_cat = parts[0]
                    target_sub = parts[1]
                else:
                    # 最近游戏行，使用源游戏的分类
                    target_cat = source_card.game.category
                    target_sub = source_card.game.sub
                self.game_moved_to_category.emit(source_card.game, target_cat, target_sub)
                event.acceptProposedAction()
                return
        event.ignore()

    def _reorder_games(self, source_idx: int, target_idx: int):
        """重新排序游戏卡片并保存权重"""
        if not self._games or source_idx >= len(self._games):
            return
        game = self._games[source_idx]
        self._games.pop(source_idx)
        # 调整目标索引
        if target_idx > source_idx:
            target_idx -= 1
        target_idx = max(0, min(target_idx, len(self._games)))
        self._games.insert(target_idx, game)

        # 重新分配 sort_weight（按当前顺序，步长 10.0）
        for i, g in enumerate(self._games):
            g.sort_weight = float(i * 10.0)
            GameMetadata.set(g, sort_weight=g.sort_weight)

        self.refresh_requested.emit()

    def get_games(self) -> List[GameInfo]:
        return self._games[:]


# ============================================================================
# 详情页
# ============================================================================
class DetailPage(QWidget):
    back_clicked = pyqtSignal()
    refresh_requested = pyqtSignal()
    launch_requested = pyqtSignal(object)
    game_info_changed = pyqtSignal(object)  # 评分/笔记等局部信息变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game: Optional[GameInfo] = None
        self.setMouseTracking(True)

        self.bg_label = QLabel(self)
        # 不启用 setScaledContents，改为手动按比例缩放，避免窗口变化时拉伸变形
        self.bg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bg_label.setStyleSheet("background-color: #0e141b;")
        self._bg_original_pixmap: Optional[QPixmap] = None

        self.blur_effect = QGraphicsBlurEffect(self)
        self.blur_effect.setBlurRadius(0)
        self.bg_label.setGraphicsEffect(self.blur_effect)

        self.overlay = QWidget(self)
        # 左右两侧有暗色遮罩，中间区域透明，让壁纸清晰可见
        self.overlay.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 rgba(14,20,27,0.85), stop:0.25 rgba(14,20,27,0.6),"
            "stop:0.4 rgba(14,20,27,0.0), stop:0.6 rgba(14,20,27,0.0),"
            "stop:0.75 rgba(14,20,27,0.6), stop:1 rgba(14,20,27,0.85));"
        )

        self.content = QWidget(self)
        self.content.setStyleSheet("background: transparent;")
        content_layout = QHBoxLayout(self.content)
        content_layout.setContentsMargins(40, 60, 40, 60)
        content_layout.setSpacing(0)

        self.info_widget = QWidget(self.content)
        # 毛玻璃半透明背景效果
        self.info_widget.setStyleSheet(
            "QWidget {"
            "  background: rgba(20, 28, 40, 0.75);"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 12px;"
            "}"
        )
        info_layout = QVBoxLayout(self.info_widget)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        info_layout.setSpacing(20)
        info_layout.setContentsMargins(24, 24, 24, 24)

        self.name_label = QLabel(self.info_widget)
        self.name_label.setFont(QFont("Microsoft YaHei", 38, QFont.Weight.Bold))
        # 文字阴影效果，确保在任何背景上可读
        self.name_label.setStyleSheet(
            "color: white;"
            "text-shadow: 0 2px 8px rgba(0,0,0,0.8);"
        )
        self.name_label.setWordWrap(True)
        info_layout.addWidget(self.name_label)

        self.cat_label = QLabel(self.info_widget)
        self.cat_label.setFont(QFont("Microsoft YaHei", 13))
        self.cat_label.setStyleSheet("color: #8a8a8a;")
        info_layout.addWidget(self.cat_label)

        self.cg_count_label = QLabel(self.info_widget)
        self.cg_count_label.setFont(QFont("Microsoft YaHei", 11))
        self.cg_count_label.setStyleSheet("color: #66c0f4;")
        info_layout.addWidget(self.cg_count_label)

        # 收藏 + 启动按钮行
        self.action_widget = QWidget(self.info_widget)
        action_layout = QHBoxLayout(self.action_widget)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(10)

        self.fav_btn = QPushButton("★ 收藏" if False else "☆ 收藏", self.action_widget)
        self.fav_btn.setFixedSize(90, 32)
        self.fav_btn.setFont(QFont("Microsoft YaHei", 11))
        self.fav_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,200,0,0.15);"
            "  color: #ffcc00;"
            "  border: 1px solid rgba(255,200,0,0.4);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(255,200,0,0.25);"
            "}"
        )
        self.fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fav_btn.clicked.connect(self._toggle_favorite)
        action_layout.addWidget(self.fav_btn)

        self.launch_btn = QPushButton("▶ 启动游戏", self.action_widget)
        self.launch_btn.setFixedSize(120, 32)
        self.launch_btn.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        self.launch_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(26,159,255,0.2);"
            "  color: #66c0f4;"
            "  border: 1px solid rgba(26,159,255,0.4);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(26,159,255,0.35);"
            "  border: 1px solid #66c0f4;"
            "}"
            "QPushButton:disabled {"
            "  background: rgba(255,255,255,0.05);"
            "  color: #666666;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "}"
        )
        self.launch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_btn.clicked.connect(self._launch_game)
        action_layout.addWidget(self.launch_btn)

        self.set_exe_btn = QPushButton("设置启动", self.action_widget)
        self.set_exe_btn.setFixedSize(80, 32)
        self.set_exe_btn.setFont(QFont("Microsoft YaHei", 10))
        self.set_exe_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,255,255,0.08);"
            "  color: #8a8a8a;"
            "  border: 1px solid rgba(255,255,255,0.15);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(255,255,255,0.15);"
            "  color: white;"
            "}"
        )
        self.set_exe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_exe_btn.clicked.connect(self._set_exe_from_detail)
        action_layout.addWidget(self.set_exe_btn)

        action_layout.addStretch()
        info_layout.addWidget(self.action_widget)

        # 评分
        self.rating_widget = QWidget(self.info_widget)
        rating_layout = QHBoxLayout(self.rating_widget)
        rating_layout.setContentsMargins(0, 0, 0, 0)
        rating_layout.setSpacing(4)

        rating_title = QLabel("评分:", self.rating_widget)
        rating_title.setFont(QFont("Microsoft YaHei", 11))
        rating_title.setStyleSheet("color: #8a8a8a;")
        rating_layout.addWidget(rating_title)

        self.star_buttons = []
        for i in range(1, 6):
            btn = QPushButton("☆", self.rating_widget)
            btn.setFixedSize(28, 28)
            btn.setFont(QFont("Microsoft YaHei", 14))
            btn.setStyleSheet("color: #ffcc00; background: transparent; border: none;")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, r=i: self._set_rating(r))
            rating_layout.addWidget(btn)
            self.star_buttons.append(btn)

        rating_layout.addStretch()
        info_layout.addWidget(self.rating_widget)

        # 笔记
        note_title = QLabel("游戏笔记", self.info_widget)
        note_title.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        note_title.setStyleSheet("color: white;")
        info_layout.addWidget(note_title)

        self.note_edit = QTextEdit(self.info_widget)
        self.note_edit.setFont(QFont("Microsoft YaHei", 11))
        self.note_edit.setStyleSheet(
            "QTextEdit {"
            "  background: rgba(255,255,255,0.05);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 6px;"
            "  padding: 8px;"
            "}"
        )
        self.note_edit.setPlaceholderText("记录游戏心得...")
        self.note_edit.setMinimumHeight(80)
        self.note_edit.setMaximumHeight(140)
        info_layout.addWidget(self.note_edit)

        # 笔记自动保存定时器（防抖 800ms）
        self._note_save_timer = QTimer(self)
        self._note_save_timer.setSingleShot(True)
        self._note_save_timer.timeout.connect(self._save_note)
        self.note_edit.textChanged.connect(self._on_note_changed)

        self.save_note_btn = QPushButton("保存笔记", self.info_widget)
        self.save_note_btn.setFixedSize(90, 32)
        self.save_note_btn.setFont(QFont("Microsoft YaHei", 10))
        self.save_note_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(26,159,255,0.2);"
            "  color: #66c0f4;"
            "  border: 1px solid rgba(26,159,255,0.4);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(26,159,255,0.35);"
            "}"
        )
        self.save_note_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_note_btn.clicked.connect(self._save_note)
        info_layout.addWidget(self.save_note_btn)

        # 无封面提示
        self.no_cover_widget = QWidget(self.info_widget)
        no_cover_layout = QHBoxLayout(self.no_cover_widget)
        no_cover_layout.setContentsMargins(0, 0, 0, 0)
        no_cover_layout.setSpacing(10)

        self.no_cover_hint = QLabel("该游戏暂无封面和壁纸")
        self.no_cover_hint.setFont(QFont("Microsoft YaHei", 12))
        self.no_cover_hint.setStyleSheet("color: #ffaa44;")
        no_cover_layout.addWidget(self.no_cover_hint)

        self.add_cover_btn = QPushButton("添加封面", self.no_cover_widget)
        self.add_cover_btn.setFixedSize(100, 32)
        self.add_cover_btn.setFont(QFont("Microsoft YaHei", 11))
        self.add_cover_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,170,68,0.2);"
            "  color: #ffaa44;"
            "  border: 1px solid rgba(255,170,68,0.4);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(255,170,68,0.3);"
            "}"
        )
        self.add_cover_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_cover_btn.clicked.connect(lambda: self._add_detail_image("cover"))
        no_cover_layout.addWidget(self.add_cover_btn)

        self.add_wallpaper_btn = QPushButton("添加壁纸", self.no_cover_widget)
        self.add_wallpaper_btn.setFixedSize(100, 32)
        self.add_wallpaper_btn.setFont(QFont("Microsoft YaHei", 11))
        self.add_wallpaper_btn.setStyleSheet(self.add_cover_btn.styleSheet())
        self.add_wallpaper_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_wallpaper_btn.clicked.connect(lambda: self._add_detail_image("wallpaper"))
        no_cover_layout.addWidget(self.add_wallpaper_btn)

        no_cover_layout.addStretch()
        info_layout.addWidget(self.no_cover_widget)

        info_layout.addStretch()

        self.back_btn = QPushButton("<  返回画廊", self.info_widget)
        self.back_btn.setFixedSize(160, 44)
        self.back_btn.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.back_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,255,255,0.1);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.2);"
            "  border-radius: 6px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(255,255,255,0.2);"
            "  border: 1px solid #66c0f4;"
            "}"
        )
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.back_clicked.emit)
        info_layout.addWidget(self.back_btn)

        content_layout.addWidget(self.info_widget)
        self.info_widget.setFixedWidth(380)
        content_layout.addStretch(stretch=1)

        self.trigger_zone = QWidget(self)
        self.trigger_zone.setFixedWidth(60)
        self.trigger_zone.setCursor(Qt.CursorShape.PointingHandCursor)
        self.trigger_zone.setStyleSheet("background: rgba(255,255,255,0.02);")

        self.cg_panel = QFrame(self)
        self.cg_panel.setFixedWidth(CG_PANEL_W)
        self.cg_panel.setStyleSheet(
            "QFrame {"
            "  background: rgba(20, 28, 40, 0.96);"
            "  border-left: 1px solid rgba(255,255,255,0.1);"
            "}"
        )
        cg_layout = QVBoxLayout(self.cg_panel)
        cg_layout.setContentsMargins(20, 20, 20, 20)
        cg_layout.setSpacing(16)

        cg_title = QLabel("CG 鉴赏", self.cg_panel)
        cg_title.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        cg_title.setStyleSheet(
            "color: white;"
            "border-bottom: 1px solid rgba(255,255,255,0.1);"
            "padding-bottom: 10px;"
        )
        cg_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cg_layout.addWidget(cg_title)

        self.cg_scroll = QScrollArea(self.cg_panel)
        self.cg_scroll.setWidgetResizable(True)
        self.cg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cg_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cg_scroll.setStyleSheet("background: transparent; border: none;")

        self.cg_container = QWidget(self.cg_scroll)
        self.cg_grid = QGridLayout(self.cg_container)
        self.cg_grid.setContentsMargins(0, 0, 0, 0)
        self.cg_grid.setSpacing(10)
        self.cg_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self.cg_scroll.setWidget(self.cg_container)
        cg_layout.addWidget(self.cg_scroll)

        self.cg_panel.hide()
        self.cg_panel_visible = False
        self.cg_panel_x = 0.0

        self._panel_anim = QVariantAnimation(self)
        self._panel_anim.setDuration(350)
        self._panel_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._panel_anim.valueChanged.connect(self._on_panel_anim)

        self.trigger_zone.installEventFilter(self)
        self.cg_panel.installEventFilter(self)

    def set_game(self, game: GameInfo):
        self.game = game

        # 清除旧背景
        self.bg_label.setPixmap(QPixmap())
        self._bg_original_pixmap = None

        # 加载壁纸或封面作为背景
        if game.wallpaper and game.wallpaper.exists():
            self._load_bg(str(game.wallpaper))
        elif game.cover and game.cover.exists():
            self._load_bg(str(game.cover))

        # 默认显示CG面板
        QTimer.singleShot(100, self._show_cg_panel)

        self.name_label.setText(game.name)
        self.cat_label.setText(f"分类: {game.category} / {game.sub}")
        self.cg_count_label.setText(f"CG 数量: {len(game.cg_files)}")

        # 评分与笔记
        self._update_rating_stars()
        self.note_edit.setPlainText(game.note)

        has_cover = game.cover and game.cover.exists()
        has_wallpaper = game.wallpaper and game.wallpaper.exists()
        self.no_cover_widget.setVisible(not (has_cover or has_wallpaper))

        # 更新收藏按钮状态
        self._update_fav_btn()

        # 更新启动按钮状态
        has_exe = bool(game.exe_path and Path(game.exe_path).exists())
        self.launch_btn.setEnabled(has_exe)
        self.launch_btn.setVisible(True)
        self.set_exe_btn.setVisible(True)

        while self.cg_grid.count():
            item = self.cg_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if game.cg_files:
            cols = 3
            for i, cg_path in enumerate(game.cg_files):
                thumb = CGThumb(cg_path, game.name, self.cg_container)
                thumb.clicked.connect(self._open_cg_viewer)
                self.cg_grid.addWidget(thumb, i // cols, i % cols)
        else:
            empty = QLabel("暂无 CG", self.cg_container)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("color: #8a8a8a; font-size: 14px; padding: 40px;")
            self.cg_grid.addWidget(empty, 0, 0, 1, 3)

        self.cg_panel.hide()
        self.cg_panel_visible = False
        self.cg_panel_x = 0.0
        self._update_panel_pos()

    def _update_fav_btn(self):
        if self.game and self.game.favorite:
            self.fav_btn.setText("★ 已收藏")
            self.fav_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(255,200,0,0.25);"
                "  color: #ffcc00;"
                "  border: 1px solid rgba(255,200,0,0.5);"
                "  border-radius: 4px;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(255,200,0,0.35);"
                "}"
            )
        else:
            self.fav_btn.setText("☆ 收藏")
            self.fav_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(255,255,255,0.08);"
                "  color: #8a8a8a;"
                "  border: 1px solid rgba(255,255,255,0.15);"
                "  border-radius: 4px;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(255,255,255,0.15);"
                "  color: white;"
                "}"
            )

    def _toggle_favorite(self):
        if not self.game:
            return
        self.game.favorite = not self.game.favorite
        GameMetadata.set(self.game, favorite=self.game.favorite)
        self._update_fav_btn()
        self.refresh_requested.emit()

    def _launch_game(self):
        if not self.game or not self.game.exe_path:
            return
        try:
            exe = Path(self.game.exe_path)
            if exe.exists():
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
            else:
                QMessageBox.warning(self, "提示", "启动程序不存在")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"启动失败: {e}")

    def _set_exe_from_detail(self):
        if not self.game:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择游戏启动程序", "",
            "可执行文件 (*.exe);;所有文件 (*.*)",
            options=FILE_DIALOG_OPTIONS
        )
        if not file_path:
            return
        self.game.exe_path = file_path
        GameMetadata.set(self.game, exe_path=file_path)
        has_exe = Path(file_path).exists()
        self.launch_btn.setEnabled(has_exe)
        QMessageBox.information(self, "成功", "启动程序已设置")
        self.refresh_requested.emit()

    def _update_rating_stars(self):
        """根据当前评分更新星星显示"""
        if not self.game:
            return
        for i, btn in enumerate(self.star_buttons, 1):
            btn.setText("★" if i <= self.game.rating else "☆")

    def _set_rating(self, rating: int):
        """设置游戏评分"""
        if not self.game:
            return
        self.game.rating = rating
        GameMetadata.set(self.game, rating=rating)
        self._update_rating_stars()
        self.game_info_changed.emit(self.game)
        self._apply_bg_scale()

    def _on_note_changed(self):
        """笔记内容变化时启动防抖保存"""
        if not self.game:
            return
        self.save_note_btn.setText("保存中...")
        self._note_save_timer.start(800)

    def _save_note(self):
        """保存游戏笔记到 note.txt"""
        if not self.game:
            return
        text = self.note_edit.toPlainText()
        self.game.note = text
        save_game_note(self.game.path, text)
        self.game_info_changed.emit(self.game)
        self.save_note_btn.setText("已保存")
        QTimer.singleShot(1500, lambda: self.save_note_btn.setText("保存笔记"))
        self._apply_bg_scale()

    def _load_bg(self, path: str):
        """加载背景图片 - 自适应居中裁剪，不拉伸"""
        def _on_loaded(p: str, pixmap: QPixmap):
            if self.game and (str(self.game.wallpaper) == p or str(self.game.cover) == p):
                if not pixmap.isNull():
                    self._bg_original_pixmap = pixmap.copy()
                    self._apply_bg_scale()

        _image_loader.load_once(path, QSize(), _on_loaded)

    def _on_wp_loaded(self, path: str, pixmap: QPixmap):
        """兼容旧方法 - 自适应居中裁剪，不拉伸"""
        if self.game and (str(self.game.wallpaper) == path or str(self.game.cover) == path):
            if not pixmap.isNull():
                self._bg_original_pixmap = pixmap.copy()
                self._apply_bg_scale()

    def _open_cg_viewer(self, path: str, game_name: str):
        # 找到当前CG在列表中的索引
        current_index = 0
        for i, cg_path in enumerate(self.game.cg_files):
            if str(cg_path) == path:
                current_index = i
                break
        viewer = CGViewerDialog(self.game.cg_files, current_index, game_name, self)
        viewer.exec()

    def _add_detail_image(self, img_type: str):
        """在详情页添加封面/壁纸，支持裁剪调整"""
        if not self.game:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, f"选择{img_type}图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp)",
            options=FILE_DIALOG_OPTIONS
        )
        if not file_path:
            return

        ext = Path(file_path).suffix
        dst = self.game.path / f"{img_type}{ext}"

        # 如果选择的是同一个文件，不做任何操作
        if Path(file_path).resolve() == dst.resolve():
            QMessageBox.information(self, "提示", "选择的图片已经是当前文件")
            return

        # 打开裁剪对话框
        target_w = CARD_W if img_type == "cover" else 1920
        target_h = CARD_H if img_type == "cover" else 1080
        crop_dlg = ImageCropDialog(file_path, target_w, target_h, f"裁剪{img_type}", self)
        if crop_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        cropped = crop_dlg.get_cropped_pixmap()
        if cropped.isNull():
            QMessageBox.critical(self, "错误", "裁剪失败")
            return

        # 删除旧文件（如果存在且不是新文件）
        if img_type == "cover" and self.game.cover:
            old = self.game.path / f"cover{self.game.cover.suffix}"
            if old.exists() and old.resolve() != dst.resolve():
                old.unlink()
        elif img_type == "wallpaper" and self.game.wallpaper:
            old = self.game.path / f"wallpaper{self.game.wallpaper.suffix}"
            if old.exists() and old.resolve() != dst.resolve():
                old.unlink()

        # 保存裁剪后的图片
        try:
            cropped.save(str(dst))
            # 更新路径
            if img_type == "cover":
                self.game.cover = dst
                self.game.cover_pixmap = None
            else:
                self.game.wallpaper = dst
                self.game.wallpaper_pixmap = None

            # 重新加载背景
            _image_loader.load_once(str(dst), QSize(), self._on_wp_loaded)

            self.no_cover_widget.setVisible(False)
            QMessageBox.information(self, "成功", f"{img_type}已添加")
            self.refresh_requested.emit()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败: {e}")

    def eventFilter(self, obj, event):
        if obj == self.trigger_zone:
            if event.type() == QEvent.Type.Enter:
                self._show_cg_panel()
            return True
        elif obj == self.cg_panel:
            if event.type() == QEvent.Type.Leave:
                pos = self.mapFromGlobal(QCursor.pos())
                if not self.cg_panel.geometry().contains(pos) and not self.trigger_zone.geometry().contains(pos):
                    self._hide_cg_panel()
            return False
        return super().eventFilter(obj, event)

    def _show_cg_panel(self):
        if self.cg_panel_visible:
            return
        self.cg_panel_visible = True
        self.cg_panel.show()
        self.cg_panel.raise_()

        self._panel_anim.stop()
        self._panel_anim.setStartValue(0.0)
        self._panel_anim.setEndValue(1.0)
        self._panel_anim.start()

    def _hide_cg_panel(self):
        if not self.cg_panel_visible:
            return
        self.cg_panel_visible = False

        self._panel_anim.stop()
        self._panel_anim.setStartValue(self._panel_anim.currentValue() if self._panel_anim.currentValue() is not None else 1.0)
        self._panel_anim.setEndValue(0.0)
        self._panel_anim.start()

    def _on_panel_anim(self, value: float):
        self.cg_panel_x = value
        self._update_panel_pos()

    def _update_panel_pos(self):
        if not self.isVisible():
            return
        panel_w = CG_PANEL_W
        start_x = self.width()
        end_x = self.width() - panel_w
        current_x = int(start_x + (end_x - start_x) * self.cg_panel_x)
        self.cg_panel.setGeometry(current_x, 0, panel_w, self.height())

        if self.cg_panel_x <= 0.01 and not self.cg_panel_visible:
            self.cg_panel.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.bg_label.setGeometry(self.rect())
        self.overlay.setGeometry(self.rect())
        self.content.setGeometry(self.rect())
        self.trigger_zone.setGeometry(self.width()-60, 0, 60, self.height())
        self._update_panel_pos()
        self._apply_bg_scale()

    def _apply_bg_scale(self):
        """从原始背景图等比缩放，避免拉伸变形"""
        if not self._bg_original_pixmap or self._bg_original_pixmap.isNull():
            return
        target_size = self.bg_label.size()
        if target_size.isEmpty():
            return
        scaled = self._bg_original_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.bg_label.setPixmap(scaled)
        self.bg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)


# ============================================================================
# 管理对话框
# ============================================================================
class AddCategoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加分类")
        self.setFixedSize(400, 220)
        self.setStyleSheet(self._style())

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("添加新分类")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: white;")
        layout.addWidget(title)

        big_layout = QHBoxLayout()
        big_label = QLabel("大类名称:")
        big_label.setStyleSheet("color: #8a8a8a;")
        big_layout.addWidget(big_label)
        self.big_edit = QLineEdit()
        self.big_edit.setPlaceholderText("例如: 角色扮演")
        self.big_edit.setStyleSheet(self._input_style())
        big_layout.addWidget(self.big_edit)
        layout.addLayout(big_layout)

        sub_layout = QHBoxLayout()
        sub_label = QLabel("小类名称:")
        sub_label.setStyleSheet("color: #8a8a8a;")
        sub_layout.addWidget(sub_label)
        self.sub_edit = QLineEdit()
        self.sub_edit.setPlaceholderText("例如: 日式RPG")
        self.sub_edit.setStyleSheet(self._input_style())
        sub_layout.addWidget(self.sub_edit)
        layout.addLayout(sub_layout)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 36)
        cancel_btn.setStyleSheet(self._btn_style("#333"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("添加")
        ok_btn.setFixedSize(80, 36)
        ok_btn.setStyleSheet(self._btn_style("#1a9fff"))
        ok_btn.clicked.connect(self._on_ok)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def _on_ok(self):
        big = self.big_edit.text().strip()
        sub = self.sub_edit.text().strip()
        if not big or not sub:
            QMessageBox.warning(self, "提示", "大类和小类名称不能为空")
            return
        if add_category(big, sub):
            QMessageBox.information(self, "成功", f"分类 [{big}/{sub}] 已创建")
            self.accept()
        else:
            QMessageBox.critical(self, "错误", "创建分类失败")

    def _style(self):
        return (
            "QDialog { background-color: #1a2330; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; }"
            "QLabel { font-family: 'Microsoft YaHei'; }"
        )

    def _input_style(self):
        return (
            "QLineEdit {"
            "  background: rgba(255,255,255,0.05);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 4px;"
            "  padding: 6px 12px;"
            "  min-height: 18px;"
            "  font-size: 13px;"
            "}"
            "QLineEdit:focus {"
            "  border: 1px solid #1a9fff;"
            "}"
        )

    def _btn_style(self, color):
        return (
            f"QPushButton {{"
            f"  background: {color};"
            f"  color: white;"
            f"  border: none;"
            f"  border-radius: 4px;"
            f"  font-size: 13px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {color}dd;"
            f"}}"
        )


class AddGameDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加游戏")
        self.setFixedSize(400, 340)
        self.setStyleSheet(self._style())

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("添加新游戏")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: white;")
        layout.addWidget(title)

        cat_layout = QHBoxLayout()
        cat_label = QLabel("选择分类:")
        cat_label.setStyleSheet("color: #8a8a8a; font-size: 13px;")
        cat_label.setFont(QFont("Microsoft YaHei", 12))
        cat_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        cat_layout.addWidget(cat_label)
        self.cat_combo = QComboBox()
        self.cat_combo.setStyleSheet(self._combo_style())
        self._load_categories()
        cat_layout.addWidget(self.cat_combo)
        layout.addLayout(cat_layout)

        name_layout = QHBoxLayout()
        name_label = QLabel("游戏名称:")
        name_label.setStyleSheet("color: #8a8a8a; font-size: 13px;")
        name_label.setFont(QFont("Microsoft YaHei", 12))
        name_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        name_layout.addWidget(name_label)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: 最终幻想7")
        self.name_edit.setStyleSheet(self._input_style())
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)

        cover_layout = QHBoxLayout()
        self.cover_path = QLabel("未选择")
        self.cover_path.setStyleSheet("color: #8a8a8a; font-size: 12px;")
        self.cover_path.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        cover_layout.addWidget(self.cover_path)
        cover_btn = QPushButton("选择封面")
        cover_btn.setFixedSize(90, 30)
        cover_btn.setStyleSheet(self._btn_style("#333"))
        cover_btn.clicked.connect(self._select_cover)
        cover_layout.addWidget(cover_btn)
        layout.addLayout(cover_layout)

        # exe 文件选择
        exe_layout = QHBoxLayout()
        self.exe_path_label = QLabel("未选择")
        self.exe_path_label.setStyleSheet("color: #8a8a8a; font-size: 12px;")
        self.exe_path_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        exe_layout.addWidget(self.exe_path_label)
        exe_btn = QPushButton("选择exe")
        exe_btn.setFixedSize(90, 30)
        exe_btn.setStyleSheet(self._btn_style("#333"))
        exe_btn.clicked.connect(self._select_exe)
        exe_layout.addWidget(exe_btn)
        layout.addLayout(exe_layout)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 36)
        cancel_btn.setStyleSheet(self._btn_style("#333"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("添加")
        ok_btn.setFixedSize(80, 36)
        ok_btn.setStyleSheet(self._btn_style("#1a9fff"))
        ok_btn.clicked.connect(self._on_ok)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._cover_file = None
        self._exe_file = None

    def _load_categories(self):
        cats = get_categories()
        for big, subs in cats.items():
            for sub in subs:
                self.cat_combo.addItem(f"{big} / {sub}", (big, sub))

    def _select_cover(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp)",
            options=FILE_DIALOG_OPTIONS
        )
        if file_path:
            self._cover_file = file_path
            self.cover_path.setText(Path(file_path).name)
            self.cover_path.setStyleSheet("color: #66c0f4; font-size: 11px;")

    def _select_exe(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择游戏启动程序", "",
            "可执行文件 (*.exe)",
            options=FILE_DIALOG_OPTIONS
        )
        if file_path:
            self._exe_file = file_path
            self.exe_path_label.setText(Path(file_path).name)
            self.exe_path_label.setStyleSheet("color: #66c0f4; font-size: 11px;")
            # 自动提取exe文件名（不含扩展名）作为游戏名称
            exe_name = Path(file_path).stem
            if not self.name_edit.text().strip():
                self.name_edit.setText(exe_name)

    def _on_ok(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "游戏名称不能为空")
            return

        data = self.cat_combo.currentData()
        if not data:
            QMessageBox.warning(self, "提示", "请先选择一个分类")
            return

        big, sub = data
        game_path = add_game(big, sub, name)

        if self._cover_file:
            ext = Path(self._cover_file).suffix
            copy_image(self._cover_file, game_path / f"cover{ext}")

        # 保存exe路径到元数据
        if self._exe_file:
            # 创建一个临时 GameInfo 来设置元数据
            temp_game = GameInfo(name=name, path=game_path, category=big, sub=sub)
            GameMetadata.set(temp_game, exe_path=self._exe_file)

        QMessageBox.information(self, "成功", f"游戏 [{name}] 已创建\n路径: {game_path}")
        self.accept()

    def _style(self):
        return "QDialog { background-color: #1a2330; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; }"

    def _input_style(self):
        return (
            "QLineEdit {"
            "  background: rgba(255,255,255,0.05);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 4px;"
            "  padding: 8px 12px;"
            "  font-size: 13px;"
            "}"
            "QLineEdit:focus {"
            "  border: 1px solid #1a9fff;"
            "}"
        )

    def _combo_style(self):
        return (
            "QComboBox {"
            "  background: rgba(255,255,255,0.05);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 4px;"
            "  padding: 6px 12px;"
            "  min-height: 18px;"
            "  font-size: 13px;"
            "}"
            "QComboBox:focus {"
            "  border: 1px solid #1a9fff;"
            "}"
            "QComboBox::drop-down {"
            "  border: none;"
            "  width: 30px;"
            "}"
            "QComboBox QAbstractItemView {"
            "  background: #1a2330;"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  selection-background-color: rgba(26,159,255,0.3);"
            "}"
        )

    def _btn_style(self, color):
        return (
            f"QPushButton {{"
            f"  background: {color};"
            f"  color: white;"
            f"  border: none;"
            f"  border-radius: 4px;"
            f"  font-size: 13px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {color}dd;"
            f"}}"
        )


# ============================================================================
# 主窗口
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GameGallery - 游戏藏品展示")
        self.setMinimumSize(1280, 720)
        self.showMaximized()

        self.central = QWidget(self)
        self.setCentralWidget(self.central)
        self.main_layout = QVBoxLayout(self.central)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self._build_topbar()

        self.stack = QStackedWidget(self.central)
        self.main_layout.addWidget(self.stack)

        self.gallery_page = QWidget(self.stack)
        self.gallery_layout = QVBoxLayout(self.gallery_page)
        self.gallery_layout.setContentsMargins(0, 0, 0, 0)
        self.gallery_layout.setSpacing(0)
        self.gallery_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.gallery_scroll = QScrollArea(self.gallery_page)
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gallery_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.gallery_scroll.setStyleSheet("background: transparent; border: none;")
        self.gallery_scroll.verticalScrollBar().setStyleSheet(
            "QScrollBar:vertical {"
            "  background: transparent;"
            "  width: 8px;"
            "  margin: 0px;"
            "}"
            "QScrollBar::handle:vertical {"
            "  background: rgba(255,255,255,0.15);"
            "  border-radius: 4px;"
            "  min-height: 30px;"
            "}"
            "QScrollBar::handle:vertical:hover {"
            "  background: rgba(255,255,255,0.25);"
            "}"
        )

        self.rows_container = QWidget(self.gallery_scroll)
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 20, 0, 40)
        self.rows_layout.setSpacing(30)
        self.rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.gallery_scroll.setWidget(self.rows_container)
        self.gallery_layout.addWidget(self.gallery_scroll)

        self.stack.addWidget(self.gallery_page)

        self.detail_page = DetailPage(self.stack)
        self.detail_page.back_clicked.connect(self._show_gallery)
        self.detail_page.refresh_requested.connect(self._refresh)
        self.detail_page.game_info_changed.connect(self._update_status_bar)
        self.stack.addWidget(self.detail_page)

        self._build_bottombar()

        self.games: List[GameInfo] = []
        self._show_favorites_only = False
        self._search_text = ""
        self._current_game: Optional[GameInfo] = None

        # 检查并确认数据目录
        self._check_root_path()
        self._load_games()

        self.setStyleSheet(
            "QMainWindow, QWidget {"
            "  background-color: #0e141b;"
            "  color: #ffffff;"
            "  font-family: \"Microsoft YaHei\", \"Segoe UI\", sans-serif;"
            "}"
        )

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_time)
        self._timer.start(1000)
        self._update_time()

    def _build_topbar(self):
        self.topbar = QWidget(self.central)
        self.topbar.setFixedHeight(TOPBAR_H)
        self.topbar.setStyleSheet(
            "background: rgba(14,20,27,0.95);"
            "border-bottom: 1px solid rgba(255,255,255,0.05);"
        )
        layout = QHBoxLayout(self.topbar)
        layout.setContentsMargins(20, 0, 20, 0)

        self.logo_label = QLabel("GameGallery", self.topbar)
        self.logo_label.setFont(QFont("Microsoft YaHei", 16, QFont.Weight.Bold))
        self.logo_label.setStyleSheet("color: #66c0f4;")
        layout.addWidget(self.logo_label)

        layout.addSpacing(30)

        # 统计面板
        self.stats_widget = QWidget(self.topbar)
        stats_layout = QHBoxLayout(self.stats_widget)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(16)

        self.total_games_label = QLabel("游戏: 0", self.stats_widget)
        self.total_games_label.setFont(QFont("Microsoft YaHei", 11))
        self.total_games_label.setStyleSheet("color: #8a8a8a;")
        stats_layout.addWidget(self.total_games_label)

        self.total_cg_label = QLabel("CG: 0", self.stats_widget)
        self.total_cg_label.setFont(QFont("Microsoft YaHei", 11))
        self.total_cg_label.setStyleSheet("color: #8a8a8a;")
        stats_layout.addWidget(self.total_cg_label)

        self.recent_added_label = QLabel("最近添加: -", self.stats_widget)
        self.recent_added_label.setFont(QFont("Microsoft YaHei", 11))
        self.recent_added_label.setStyleSheet("color: #8a8a8a;")
        stats_layout.addWidget(self.recent_added_label)

        layout.addWidget(self.stats_widget)

        layout.addSpacing(30)

        # 搜索框
        self.search_edit = QLineEdit(self.topbar)
        self.search_edit.setPlaceholderText("搜索游戏...")
        self.search_edit.setFixedWidth(280)
        self.search_edit.setFont(QFont("Microsoft YaHei", 11))
        self.search_edit.setStyleSheet(
            "QLineEdit {"
            "  background: rgba(255,255,255,0.06);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 6px;"
            "  padding: 6px 12px;"
            "  font-size: 13px;"
            "}"
            "QLineEdit:focus {"
            "  border: 1px solid #1a9fff;"
            "}"
        )
        self.search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_edit)

        layout.addStretch()

        # 收藏筛选开关
        self.fav_filter_btn = QPushButton("☆ 全部", self.topbar)
        self.fav_filter_btn.setFixedSize(90, 32)
        self.fav_filter_btn.setFont(QFont("Microsoft YaHei", 10))
        self.fav_filter_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,255,255,0.06);"
            "  color: #8a8a8a;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(255,255,255,0.12);"
            "  color: white;"
            "}"
        )
        self.fav_filter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fav_filter_btn.clicked.connect(self._toggle_fav_filter)
        layout.addWidget(self.fav_filter_btn)

        layout.addSpacing(10)

        self.add_btn = QPushButton("+ 添加", self.topbar)
        self.add_btn.setFixedSize(90, 36)
        self.add_btn.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        self.add_btn.setStyleSheet(
            "QPushButton {"
            "  background: #1a9fff;"
            "  color: white;"
            "  border: none;"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: #66c0f4;"
            "}"
        )
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(self._show_add_menu)
        layout.addWidget(self.add_btn)

        layout.addSpacing(10)

        self.time_label = QLabel(self.topbar)
        self.time_label.setFont(QFont("Microsoft YaHei", 12))
        self.time_label.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(self.time_label)

        self.main_layout.addWidget(self.topbar)

    def _toggle_fav_filter(self):
        self._show_favorites_only = not self._show_favorites_only
        if self._show_favorites_only:
            self.fav_filter_btn.setText("★ 收藏")
            self.fav_filter_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(255,200,0,0.15);"
                "  color: #ffcc00;"
                "  border: 1px solid rgba(255,200,0,0.4);"
                "  border-radius: 4px;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(255,200,0,0.25);"
                "}"
            )
        else:
            self.fav_filter_btn.setText("☆ 全部")
            self.fav_filter_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(255,255,255,0.06);"
                "  color: #8a8a8a;"
                "  border: 1px solid rgba(255,255,255,0.1);"
                "  border-radius: 4px;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(255,255,255,0.12);"
                "  color: white;"
                "}"
            )
        self._refresh()

    def _on_search_changed(self, text: str):
        self._search_text = text.strip().lower()
        self._refresh()

    def _show_add_menu(self):
        """显示添加菜单 - 不使用样式表避免崩溃"""
        menu = QMenu(self)
        # 不设置任何样式表，使用系统默认样式

        act_cat = menu.addAction("添加分类")
        act_game = menu.addAction("添加游戏")
        menu.addSeparator()
        act_open = menu.addAction("打开文件夹")

        action = menu.exec(self.add_btn.mapToGlobal(QPoint(0, self.add_btn.height())))

        if action == act_cat:
            dlg = AddCategoryDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._refresh()
        elif action == act_game:
            dlg = AddGameDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._refresh()
        elif action == act_open:
            self._open_root_folder()

    def _update_time(self):
        try:
            self.time_label.setText(QDateTime.currentDateTime().toString("HH:mm"))
        except RuntimeError:
            pass

    def _build_bottombar(self):
        self.bottombar = QWidget(self.central)
        self.bottombar.setFixedHeight(BOTTOMBAR_H)
        self.bottombar.setStyleSheet(
            "background: rgba(14,20,27,0.95);"
            "border-top: 1px solid rgba(255,255,255,0.05);"
        )
        layout = QHBoxLayout(self.bottombar)
        layout.setContentsMargins(20, 0, 20, 0)

        self.menu_btn = QPushButton("菜单", self.bottombar)
        self.menu_btn.setFixedSize(100, 36)
        self.menu_btn.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        self.menu_btn.setStyleSheet(
            "QPushButton {"
            "  background: rgba(255,255,255,0.08);"
            "  color: white;"
            "  border: 1px solid rgba(255,255,255,0.1);"
            "  border-radius: 4px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(255,255,255,0.15);"
            "  border: 1px solid rgba(255,255,255,0.2);"
            "}"
        )
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.clicked.connect(self._show_bottom_menu)
        layout.addWidget(self.menu_btn)

        layout.addStretch()

        self.status_label = QLabel(
            "鼠标滚轮横向浏览 - 点击卡片进入详情 - 右键卡片管理 - 右侧查看CG",
            self.bottombar
        )
        self.status_label.setFont(QFont("Microsoft YaHei", 10))
        self.status_label.setStyleSheet("color: rgba(255,255,255,0.3);")
        layout.addWidget(self.status_label)

        layout.addStretch()

        self.refresh_btn = QPushButton("刷新", self.bottombar)
        self.refresh_btn.setFixedSize(90, 36)
        self.refresh_btn.setFont(QFont("Microsoft YaHei", 11))
        self.refresh_btn.setStyleSheet(self.menu_btn.styleSheet())
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._refresh)
        layout.addWidget(self.refresh_btn)

        self.main_layout.addWidget(self.bottombar)

    def _show_bottom_menu(self):
        """底部菜单功能"""
        menu = QMenu(self)

        about = menu.addAction("关于 GameGallery")
        menu.addSeparator()
        export_backup = menu.addAction("导出备份...")
        import_backup = menu.addAction("导入备份...")
        menu.addSeparator()
        open_folder = menu.addAction("打开游戏文件夹")

        action = menu.exec(self.menu_btn.mapToGlobal(QPoint(0, -160)))

        if action == about:
            QMessageBox.about(self, "关于",
                "GameGallery v2.0\n\n"
                "Steam 大屏幕风格游戏藏品展示工具\n"
                "支持管理游戏封面、壁纸、CG图片、收藏、搜索、快捷启动\n\n"
                f"当前数据目录: {ROOT_PATH}\n\n"
                "操作说明:\n"
                "- 鼠标滚轮: 横向浏览游戏\n"
                "- 左键点击: 进入游戏详情\n"
                "- 右键卡片: 管理游戏(设置封面/壁纸/CG/收藏/启动/删除)\n"
                "- 鼠标移到右侧: 查看CG面板\n"
                "- ESC: 返回画廊")
        elif action == open_folder:
            self._open_root_folder()
        elif action == export_backup:
            self._export_backup()
        elif action == import_backup:
            self._import_backup()

    def _is_backup_excluded(self, path: Path) -> bool:
        """判断路径是否应被排除在备份之外"""
        rel = path.relative_to(ROOT_PATH)
        parts = rel.parts
        # 排除以 . 开头的目录/文件（如 .deleted, .git）
        if any(p.startswith('.') for p in parts):
            return True
        # 排除 Python/打包相关缓存和产物
        if any(p in ('__pycache__', 'dist', 'build') for p in parts):
            return True
        if path.suffix.lower() in ('.spec', '.exe', '.pyc', '.py'):
            return True
        # 排除自动备份和手动导出的 zip（避免把备份本身又打包进去）
        if path.suffix == '.zip' and (
            path.name.startswith('auto_backup_') or
            path.name.startswith('GameGallery_backup_')
        ):
            return True
        return False

    def _create_backup_zip(self, zip_path: Path) -> Tuple[bool, List[str]]:
        """将当前游戏数据打包为 zip，返回 (是否成功, 写入的相对路径列表)
        空目录也会写入 zip，保证只有文件夹结构的数据能被完整迁移。
        """
        try:
            ensure_root()
            entries: List[str] = []
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for item in sorted(ROOT_PATH.rglob('*')):
                    if item == zip_path:
                        continue
                    if self._is_backup_excluded(item):
                        continue
                    rel = item.relative_to(ROOT_PATH)
                    rel_str = str(rel).replace('\\', '/')
                    if item.is_dir():
                        # 写入目录条目，确保空目录也被保留
                        if not rel_str.endswith('/'):
                            rel_str += '/'
                        zf.writestr(rel_str, '')
                        entries.append(rel_str)
                    elif item.is_file():
                        zf.write(item, rel_str)
                        entries.append(rel_str)
            print(f"备份完成: {len(entries)} 个条目 -> {zip_path}")
            return True, entries
        except Exception as e:
            print(f"创建备份失败: {e}")
            return False, []

    def _export_backup(self):
        """导出备份到用户选择的 zip 文件"""
        if not self._has_game_data():
            QMessageBox.warning(
                self, "提示",
                f"当前数据目录 [{ROOT_PATH}] 下未找到游戏数据，\n"
                "请确认游戏数据目录正确后再导出。"
            )
            return
        default_name = f"GameGallery_backup_{QDateTime.currentDateTime().toString('yyyyMMdd_HHmmss')}.zip"
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出备份", str(Path.home() / default_name),
            "ZIP 压缩包 (*.zip)",
            options=FILE_DIALOG_OPTIONS
        )
        if not file_path:
            return
        zip_path = Path(file_path)
        if zip_path.suffix.lower() != '.zip':
            zip_path = zip_path.with_suffix('.zip')
        ok, entries = self._create_backup_zip(zip_path)
        if ok:
            size_kb = zip_path.stat().st_size / 1024
            only_meta = all(
                e.lower() in ('games.json', 'config.ini') for e in entries
            )
            entries_summary = "\n".join(
                entries[:20] + ([f"... 等共 {len(entries)} 个条目"]
                                if len(entries) > 20 else [])
            ) or "(无条目)"
            msg = (
                f"备份已导出到:\n{zip_path}\n\n"
                f"大小: {size_kb:.1f} KB\n"
                f"数据目录: {ROOT_PATH}\n"
                f"包含条目数: {len(entries)}\n\n"
                f"条目列表:\n{entries_summary}"
            )
            if only_meta:
                msg += (
                    "\n\n警告: 备份中只检测到 games.json / config.ini，"
                    "未找到分类目录或图片文件。\n"
                    "请检查游戏数据目录是否正确，或游戏文件是否已丢失。"
                )
            QMessageBox.information(self, "成功", msg)
        else:
            QMessageBox.critical(self, "错误", "导出备份失败，请检查目录权限")

    def _safe_extract_zip(self, zip_path: Path):
        """安全解压 zip，校验每个条目防止路径遍历"""
        root_resolved = ROOT_PATH.resolve()
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                # 拒绝绝对路径或包含 .. 的相对路径
                parts = member.split('/')
                if '..' in parts or any(p.startswith('/') for p in parts):
                    print(f"跳过非法 zip 条目: {member}")
                    continue

                target = (ROOT_PATH / member).resolve()
                # 确保解压目标位于 ROOT_PATH 之内
                try:
                    target.relative_to(root_resolved)
                except ValueError:
                    print(f"跳过越界 zip 条目: {member}")
                    continue

                if member.endswith('/'):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)

    def _has_game_data(self, root: Optional[Path] = None) -> bool:
        """检查指定目录是否包含游戏数据"""
        if root is None:
            root = ROOT_PATH
        if not root.exists():
            return False
        # 有元数据文件或分类配置即认为有数据
        if (root / "games.json").exists() or (root / "config.ini").exists():
            return True
        # 或存在非隐藏子目录（可能是分类目录）
        for item in root.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                return True
        return False

    def _check_root_path(self):
        """启动时检查数据目录，无数据或不完整则提示"""
        global ROOT_PATH, GAMES_JSON
        print(f"ROOT_PATH: {ROOT_PATH}")

        # 完整性诊断
        data_files = [p for p in ROOT_PATH.rglob('*')
                      if p.is_file() and not self._is_backup_excluded(p)]
        data_dirs = [p for p in ROOT_PATH.rglob('*')
                     if p.is_dir() and not self._is_backup_excluded(p)]
        only_meta = (data_files and
                     all(p.name.lower() in ('games.json', 'config.ini')
                         for p in data_files) and
                     not data_dirs)

        if not self._has_game_data(ROOT_PATH) or only_meta:
            if only_meta and self._has_game_data(ROOT_PATH):
                reply = QMessageBox.question(
                    self, "数据目录不完整",
                    f"当前目录 [{ROOT_PATH}] 下只找到 games.json / config.ini，\n"
                    f"未检测到游戏分类目录或图片文件（共 {len(data_files)} 个文件）。\n\n"
                    "是否选择其他目录？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
            else:
                reply = QMessageBox.question(
                    self, "选择数据目录",
                    f"当前目录 [{ROOT_PATH}] 下未找到游戏数据。\n"
                    "是否选择其他目录？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
            if reply != QMessageBox.StandardButton.Yes:
                return

            chosen = QFileDialog.getExistingDirectory(
                self, "选择游戏数据目录", str(Path.home()),
                options=FILE_DIALOG_OPTIONS
            )
            if not chosen:
                return

            new_root = Path(chosen)
            ROOT_PATH = new_root
            GAMES_JSON = ROOT_PATH / "games.json"
            ensure_root()

    def _import_backup(self):
        """从 zip 文件导入备份"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "导入备份", str(Path.home()),
            "ZIP 压缩包 (*.zip)",
            options=FILE_DIALOG_OPTIONS
        )
        if not file_path:
            return
        zip_path = Path(file_path)

        reply = QMessageBox.question(
            self, "确认导入",
            "导入备份会覆盖当前同名文件和目录。\n"
            "程序会先自动备份当前数据到 .deleted/auto_backup_<时间>.zip\n"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if not zipfile.is_zipfile(zip_path):
            QMessageBox.critical(self, "错误", "选择的文件不是有效的 ZIP 压缩包")
            return

        try:
            # 预览 zip 内容
            with zipfile.ZipFile(zip_path, 'r') as zf:
                preview = "\n".join(zf.namelist()[:20])
                more = len(zf.namelist()) - 20
                if more > 0:
                    preview += f"\n... 等共 {len(zf.namelist())} 个条目"

            confirm = QMessageBox.question(
                self, "确认导入",
                f"即将导入以下备份内容:\n{preview}\n\n"
                "导入会覆盖当前同名文件，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

            # 先自动备份当前数据
            ensure_root()
            timestamp = int(QDateTime.currentDateTime().toSecsSinceEpoch())
            auto_backup_dir = ROOT_PATH / ".deleted"
            auto_backup_dir.mkdir(exist_ok=True)
            auto_backup_zip = auto_backup_dir / f"auto_backup_{timestamp}.zip"
            ok, _ = self._create_backup_zip(auto_backup_zip)
            if not ok:
                QMessageBox.warning(self, "警告", "自动备份当前数据失败，已取消导入")
                return

            # 安全解压导入的备份（防止路径遍历）
            self._safe_extract_zip(zip_path)

            self._refresh()
            QMessageBox.information(
                self, "成功",
                f"备份已导入并刷新\n当前数据自动备份至:\n{auto_backup_zip}"
            )
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导入失败: {e}")

    def _open_root_folder(self):
        """打开游戏根目录"""
        try:
            os.startfile(str(ROOT_PATH))
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法打开文件夹: {e}")

    def _load_games(self):
        self.games = scan_games()
        self._update_stats()
        self._build_rows()

    def _get_filtered_games(self) -> List[GameInfo]:
        """根据搜索和收藏筛选返回过滤后的游戏列表"""
        result = self.games

        # 收藏筛选
        if self._show_favorites_only:
            result = [g for g in result if g.favorite]

        # 搜索筛选
        if self._search_text:
            result = [
                g for g in result
                if (self._search_text in g.name.lower()
                    or self._search_text in g.category.lower()
                    or self._search_text in g.sub.lower())
            ]

        return result

    def _build_rows(self):
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        filtered = self._get_filtered_games()

        if not filtered:
            empty = QLabel(
                "未找到游戏数据，请点击上方 [+ 添加] 创建分类和游戏",
                self.rows_container
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setFont(QFont("Microsoft YaHei", 16))
            empty.setStyleSheet("color: #8a8a8a; padding: 100px;")
            self.rows_layout.addWidget(empty)
            return

        categories = defaultdict(list)
        for g in filtered:
            key = f"{g.category} / {g.sub}"
            categories[key].append(g)

        # 按 sort_weight 排序每个分类内的游戏
        for key in categories:
            categories[key].sort(key=lambda g: g.sort_weight)

        all_games = filtered[:8] if len(filtered) > 8 else filtered
        all_games.sort(key=lambda g: g.sort_weight)
        row = GameRow("最近游戏", all_games, self.rows_container)
        row.game_selected.connect(self._show_detail)
        row.game_delete.connect(self._on_delete_game)
        row.refresh_requested.connect(self._refresh)
        row.setAcceptDrops(True)
        self.rows_layout.addWidget(row)

        for cat_name, cat_games in categories.items():
            if len(cat_games) > 0:
                row = GameRow(cat_name, cat_games, self.rows_container)
                row.game_selected.connect(self._show_detail)
                row.game_delete.connect(self._on_delete_game)
                row.refresh_requested.connect(self._refresh)
                row.game_moved_to_category.connect(self._on_game_moved_to_category)
                row.setAcceptDrops(True)
                self.rows_layout.addWidget(row)

        self.rows_layout.addStretch()

    def _on_game_moved_to_category(self, game: GameInfo, target_category: str, target_sub: str):
        """处理跨行拖拽移动游戏到其他分类"""
        # 解析目标分类
        if not target_category or not target_sub:
            return
        
        # 如果目标分类与当前分类相同，不做任何操作
        if game.category == target_category and game.sub == target_sub:
            return
        
        # 执行移动
        src_path = Path(game.path)
        dst_path = ROOT_PATH / target_category / target_sub / game.name
        
        if not src_path.exists():
            QMessageBox.warning(self, "提示", f"源游戏目录不存在 [{game.name}]")
            return
        
        if dst_path.exists():
            QMessageBox.warning(self, "提示", f"目标分类中已存在 [{game.name}]")
            return
        
        try:
            # 创建目标目录
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            # 移动文件夹
            shutil.move(str(src_path), str(dst_path))
            # 更新元数据中的分类信息
            old_key = GameMetadata._game_key(game)
            # 更新 GameInfo
            game.category = target_category
            game.sub = target_sub
            game.path = dst_path
            # 更新封面/壁纸/cg路径
            if game.cover:
                game.cover = dst_path / game.cover.name
            if game.wallpaper:
                game.wallpaper = dst_path / game.wallpaper.name
            cg_dir = dst_path / "cg"
            if cg_dir.exists():
                game.cg_files = []
                for f in sorted(cg_dir.iterdir()):
                    if f.suffix.lower() in SUPPORTED_EXTS:
                        game.cg_files.append(f)
            # 迁移元数据
            meta = GameMetadata._data.pop(old_key, {})
            new_key = GameMetadata._game_key(game)
            GameMetadata._data[new_key] = meta
            GameMetadata.save()
            self._refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"移动失败: {e}")

    def _show_detail(self, game: GameInfo):
        self.detail_page.set_game(game)
        self._current_game = game
        self._update_status_bar(game)
        self.stack.setCurrentIndex(1)

    def _show_gallery(self):
        self._current_game = None
        self._update_status_bar(None)
        self.stack.setCurrentIndex(0)

    def _update_stats(self):
        """更新顶部统计面板"""
        total_games = len(self.games)
        total_cg = sum(len(g.cg_files) for g in self.games)
        recent = "-"
        if self.games:
            games_with_time = [g for g in self.games if g.added_time]
            if games_with_time:
                recent_game = max(games_with_time, key=lambda g: g.added_time)
                recent_dt = QDateTime.fromSecsSinceEpoch(int(recent_game.added_time))
                recent = f"{recent_game.name} ({recent_dt.toString('MM-dd')})"
        self.total_games_label.setText(f"游戏: {total_games}")
        self.total_cg_label.setText(f"CG: {total_cg}")
        self.recent_added_label.setText(f"最近添加: {recent}")

    def _update_status_bar(self, game: Optional[GameInfo] = None):
        """更新底部状态栏为当前选中游戏信息"""
        if not game:
            self.status_label.setText(
                "鼠标滚轮横向浏览 - 点击卡片进入详情 - 右键卡片管理 - 右侧查看CG"
            )
            return
        fav = "★ 已收藏" if game.favorite else "☆ 未收藏"
        rating = "⭐" * game.rating if game.rating > 0 else "未评分"
        self.status_label.setText(
            f"{game.name} | {game.category}/{game.sub} | "
            f"CG:{len(game.cg_files)} | {fav} | 评分:{rating}"
        )

    def _on_delete_game(self, game: GameInfo):
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要从管理列表中移除 [{game.name}] 吗？\n\n"
            f"游戏文件夹将被移动到回收目录，\n"
            f"不会永久删除，可随时恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                # 移动游戏文件夹到回收目录
                deleted_dir = ROOT_PATH / ".deleted"
                deleted_dir.mkdir(exist_ok=True)
                src_path = Path(game.path)
                # 避免重名
                dst_name = game.name
                dst_path = deleted_dir / dst_name
                counter = 1
                while dst_path.exists():
                    dst_name = f"{game.name}_{counter}"
                    dst_path = deleted_dir / dst_name
                    counter += 1
                shutil.move(str(src_path), str(dst_path))
                # 从元数据中移除
                GameMetadata.remove(game)
                QMessageBox.information(self, "成功", f"游戏 [{game.name}] 已移除\n文件夹已移动到: {dst_path}")
                self._refresh()
            except Exception as e:
                QMessageBox.critical(self, "错误", f"移除失败: {e}")

    def _refresh(self):
        """刷新画廊并保留滚动位置"""
        scrollbar = self.gallery_scroll.verticalScrollBar()
        old_value = scrollbar.value()
        self._load_games()
        # 在界面重建完成后恢复滚动位置
        QTimer.singleShot(0, lambda: scrollbar.setValue(old_value))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.stack.currentIndex() == 1:
            self._show_gallery()
        else:
            super().keyPressEvent(event)


# ============================================================================
# 入口
# ============================================================================
if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())