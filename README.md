# GameGallery

一个 **Steam 大屏幕风格** 的本地游戏藏品管理工具，基于 PyQt6 开发。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PyQt6](https://img.shields.io/badge/PyQt6-6.x-green)

## 功能特性

- 🎮 **游戏画廊展示**：卡片式布局，横向滚动浏览
- 🖼️ **多媒体支持**：封面、壁纸、CG 缩略图展示（支持 `.jpg` `.jpeg` `.png` `.bmp` `.webp`）
- ⭐ **收藏与评分**：收藏游戏、五星评分
- 📝 **游戏笔记**：每个游戏独立 `note.txt` 自动保存
- 🚀 **启动游戏**：右键设置 exe 启动程序，一键运行
- 🗂️ **分类管理**：支持大类/子类两级分类，可拖拽排序
- ✂️ **图片裁剪**：内置裁剪对话框，适配封面/壁纸比例
- 🔄 **备份导入/导出**：`games.json` + 分类目录 + 空文件夹完整打包为 zip
- ♻️ **安全删除**：删除的游戏移至 `.deleted/` 回收目录
- 🎨 **Steam 风格 UI**：暗色主题，沉浸式体验

## 目录结构

```
GameGallery/
├── gamegallery_pyqt6.py   # 主程序源码（单文件）
├── favicon.ico            # 程序图标
├── README.md              # 本文件
└── .gitignore             # Git 忽略规则
```

首次运行时，程序会自动创建：

```
GameGallery/
├── games.json             # 游戏元数据（收藏、评分、启动路径等）
└── config.ini             # 分类配置
```

## 快速开始

### 1. 安装依赖

```bash
pip install PyQt6
```

### 2. 运行源码

```bash
python gamegallery_pyqt6.py
```

### 3. 打包为单文件 exe

```bash
pyinstaller --onefile --noconsole --icon=favicon.ico --name GameGallery --clean gamegallery_pyqt6.py
```

打包完成后，`dist/GameGallery.exe` 即为可执行文件。

## 使用方法

1. **准备游戏数据**
   - 在程序目录下创建分类文件夹，例如：
     ```
     动作游戏/清版动作/鬼泣5/
     角色扮演/动作RPG/艾尔登法环/
     ```
   - 每个游戏文件夹中放入：
     - `cover.jpg` / `cover.png`：封面图
     - `wallpaper.jpg` / `wallpaper.png`：壁纸/详情页背景
     - `cg/` 文件夹：CG 图片

2. **配置分类**
   - 编辑 `config.ini`，格式如下：
     ```ini
     [动作游戏]
     清版动作=
     
     [角色扮演]
     动作RPG=
     JRPG=
     ```

3. **管理游戏**
   - 左键点击卡片：进入详情页
   - 右键点击卡片：设置封面/壁纸/CG、收藏、评分、启动 exe、重命名、移动分类、删除
   - 鼠标移到右侧：展开 CG 面板
   - ESC：返回画廊

## 备份与迁移

- **导出备份**：点击底部菜单“导出备份”，生成 `GameGallery_backup_*.zip`
- **导入备份**：点击底部菜单“导入备份”，选择 zip 文件
- 备份会完整保留 `games.json`、`config.ini` 以及所有分类目录（包括空文件夹）

## 注意事项

- 首次打包后的 exe 若 QFileDialog 崩溃，请确保使用源码中设置的 `DontUseNativeDialog` 选项
- 游戏图片路径为相对路径，迁移时保持目录结构即可
- `games.json` 和 `config.ini` 属于用户私有数据，不上传至 GitHub

## 许可证

MIT License
