#!/usr/bin/env python3
"""
QQ音乐下载器 - GUI版本
版本号: v1.0.0
"""

import asyncio
import json
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import logging
import sys

import aiohttp
import aiofiles
from qqmusic_api import search
from qqmusic_api.song import get_song_urls, SongFileType
from qqmusic_api.lyric import get_lyric
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT

# 尝试导入PIL用于二维码显示（已不再需要，但保留导入避免错误）
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


## ==================== 配置常量 ====================
class Config:
    BATCH_SIZE = 5
    COVER_SIZE = 800
    DOWNLOAD_TIMEOUT = 30
    MUSIC_DIR = Path("./music")
    FOLDER_NAME = "{songlist_name}"
    MIN_FILE_SIZE = 1024
    EXTERNAL_API_URL = "https://api.ygking.top"
    SEARCH_RESULTS_COUNT = 10
    USER_CONFIG_FILE = Path("./music_downloader_config.json")
    MAX_RETRY = 3
    SEARCH_RETRY_DELAY = 1
    WINDOW_SIZE = "900x700"
    WINDOW_TITLE = "QQ音乐下载器 v1.0"


## ==================== 日志配置 ====================
def setup_logging():
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logging.getLogger("qqmusic_api").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger(__name__)


## ==================== 数据类 ====================
@dataclass
class SongInfo:
    name: str
    singer: str
    mid: str
    is_vip: bool
    album_name: str
    album_mid: str


class DownloadError(Exception):
    pass


class MetadataError(Exception):
    pass


## ==================== 网络管理器 ====================
class NetworkManager:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=Config.DOWNLOAD_TIMEOUT)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


## ==================== 文件管理器 ====================
class FileManager:
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        return filename.strip()

    @staticmethod
    def ensure_directory(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path


## ==================== 封面管理器 ====================
class CoverManager:
    @staticmethod
    def get_cover_url_by_album_mid(mid: str, size: int = 800) -> Optional[str]:
        if not mid:
            return None
        return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"

    @staticmethod
    def get_cover_url_by_vs(vs: str, size: int = 800) -> Optional[str]:
        if not vs:
            return None
        return f"https://y.qq.com/music/photo_new/T062R{size}x{size}M000{vs}.jpg"

    @staticmethod
    async def get_valid_cover_url(song_data: Dict[str, Any], network: NetworkManager,
                                  size: int = 800) -> Optional[str]:
        album_mid = song_data.get('album', {}).get('mid', '')
        if album_mid:
            url = CoverManager.get_cover_url_by_album_mid(album_mid, size)
            cover_data = await CoverManager.download_cover(url, network)
            if cover_data:
                return url

        vs_values = song_data.get('vs', [])
        for vs in vs_values:
            if vs and isinstance(vs, str) and len(vs) >= 3:
                if ',' in vs:
                    parts = [p.strip() for p in vs.split(',') if p.strip()]
                    for part in parts:
                        if len(part) >= 3:
                            url = CoverManager.get_cover_url_by_vs(part, size)
                            cover_data = await CoverManager.download_cover(url, network)
                            if cover_data:
                                return url
                else:
                    url = CoverManager.get_cover_url_by_vs(vs, size)
                    cover_data = await CoverManager.download_cover(url, network)
                    if cover_data:
                        return url
        return None

    @staticmethod
    async def download_cover(url: str, network: NetworkManager) -> Optional[bytes]:
        if not url:
            return None
        try:
            session = await network.get_session()
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if len(content) > Config.MIN_FILE_SIZE:
                        if content.startswith(b'\xff\xd8') or content.startswith(b'\x89PNG'):
                            return content
        except Exception:
            pass
        return None


## ==================== 元数据管理器 ====================
class MetadataManager:
    def __init__(self, network: NetworkManager):
        self.network = network
        self.add_cover = True
        self.add_lyrics = True

    def set_options(self, add_cover: bool, add_lyrics: bool):
        self.add_cover = add_cover
        self.add_lyrics = add_lyrics

    async def add_metadata_to_flac(self, file_path: Path, song_info: SongInfo,
                                   lyrics_data: dict = None, song_data: Dict[str, Any] = None) -> bool:
        try:
            audio = FLAC(file_path)
            audio['title'] = song_info.name
            audio['artist'] = song_info.singer
            audio['album'] = song_info.album_name

            if self.add_cover and song_data:
                await self._add_cover_to_flac(audio, song_data)
            if self.add_lyrics and lyrics_data:
                self._add_lyrics_to_flac(audio, lyrics_data)

            audio.save()
            return True
        except Exception as e:
            logger.error(f"FLAC元数据添加失败: {e}")
            return False

    async def add_metadata_to_mp3(self, file_path: Path, song_info: SongInfo,
                                  lyrics_data: dict = None, song_data: Dict[str, Any] = None) -> bool:
        try:
            try:
                audio = ID3(file_path)
            except Exception:
                audio = ID3()

            tags_to_remove = ['APIC:', 'USLT:', 'TIT2', 'TPE1', 'TALB']
            for tag in tags_to_remove:
                if tag in audio:
                    del audio[tag]

            from mutagen.id3 import TIT2, TPE1, TALB
            audio.add(TIT2(encoding=3, text=song_info.name))
            audio.add(TPE1(encoding=3, text=song_info.singer))
            audio.add(TALB(encoding=3, text=song_info.album_name))

            if self.add_cover and song_data:
                await self._add_cover_to_mp3(audio, song_data)
            if self.add_lyrics and lyrics_data:
                self._add_lyrics_to_mp3(audio, lyrics_data)

            audio.save(file_path, v2_version=3)
            return True
        except Exception as e:
            logger.error(f"MP3元数据添加失败: {e}")
            return False

    async def _add_cover_to_flac(self, audio, song_data: Dict[str, Any]):
        cover_url = await CoverManager.get_valid_cover_url(song_data, self.network, Config.COVER_SIZE)
        if cover_url:
            cover_data = await CoverManager.download_cover(cover_url, self.network)
            if cover_data:
                image = Picture()
                image.type = 3
                image.mime = 'image/png' if cover_url.lower().endswith('.png') else 'image/jpeg'
                image.desc = 'Cover'
                image.data = cover_data
                audio.clear_pictures()
                audio.add_picture(image)

    async def _add_cover_to_mp3(self, audio, song_data: Dict[str, Any]):
        cover_url = await CoverManager.get_valid_cover_url(song_data, self.network, Config.COVER_SIZE)
        if cover_url:
            cover_data = await CoverManager.download_cover(cover_url, self.network)
            if cover_data:
                mime_type = 'image/png' if cover_url.lower().endswith('.png') else 'image/jpeg'
                from mutagen.id3 import APIC
                audio.add(APIC(encoding=3, mime=mime_type, type=3, desc='Cover', data=cover_data))

    def _add_lyrics_to_flac(self, audio, lyrics_data: dict):
        if lyric_text := lyrics_data.get('lyric'):
            audio['lyrics'] = lyric_text
        if trans_text := lyrics_data.get('trans'):
            audio['translyrics'] = trans_text

    def _add_lyrics_to_mp3(self, audio, lyrics_data: dict):
        if lyric_text := lyrics_data.get('lyric'):
            from mutagen.id3 import USLT
            audio.add(USLT(encoding=3, lang='eng', desc='Lyrics', text=lyric_text))


## ==================== 用户配置 ====================
class UserConfig:
    def __init__(self, config_file: Path = Config.USER_CONFIG_FILE):
        self.config_file = config_file
        self.config = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value
        self._save()


## ==================== QQ音乐下载器核心 ====================
class QQMusicDownloaderCore:
    QUALITY_OPTIONS = {
        1: ("🎧 高品质 (母带/全景声/FLAC，自动降级)", [
            (SongFileType.MASTER, "臻品母带"),
            (SongFileType.ATMOS_2, "臻品全景声"),
            (SongFileType.ATMOS_51, "臻品音质"),
            (SongFileType.FLAC, "FLAC无损"),
            (SongFileType.MP3_320, "MP3 320kbps"),
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
        2: ("📀 中品质 (MP3 320Kbps)", [
            (SongFileType.MP3_320, "MP3 320kbps"),
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
        3: ("🔊 低品质 (MP3 128Kbps)", [
            (SongFileType.MP3_128, "MP3 128kbps"),
        ]),
    }

    def __init__(self):
        self.download_dir = Config.MUSIC_DIR
        self.quality_level = 1
        self.network = NetworkManager()
        self.metadata_manager = MetadataManager(self.network)
        self.user_config = UserConfig()
        self.add_cover = True
        self.add_lyrics = True
        self._is_warmed_up = False  # 添加预热标志

    async def initialize(self):
        await self.network.get_session()
        self._load_settings()
        
        # 在后台进行API预热，不阻塞UI启动
        asyncio.create_task(self._warmup_api())

    async def _warmup_api(self):
        """预热API，建立连接池"""
        try:
            # 使用一个简单的搜索词来预热
            test_keyword = "周杰伦"
            for attempt in range(2):
                try:
                    await search.search_by_type(test_keyword, num=1)
                    self._is_warmed_up = True
                    logger.info("API预热成功")
                    return
                except Exception as e:
                    logger.warning(f"预热尝试 {attempt + 1}/2 失败: {e}")
                    if attempt < 1:
                        await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"API预热失败，后续搜索可能较慢: {e}")

    def _load_settings(self):
        self.quality_level = self.user_config.get('quality_level', 1)
        self.add_cover = self.user_config.get('add_cover', True)
        self.add_lyrics = self.user_config.get('add_lyrics', True)
        self.metadata_manager.set_options(self.add_cover, self.add_lyrics)

    def save_settings(self):
        self.user_config.set('quality_level', self.quality_level)
        self.user_config.set('add_cover', self.add_cover)
        self.user_config.set('add_lyrics', self.add_lyrics)

    async def close(self):
        await self.network.close()

    def _get_quality_strategy(self) -> List[Tuple[SongFileType, str]]:
        _, fallback_chain = self.QUALITY_OPTIONS.get(self.quality_level, self.QUALITY_OPTIONS[1])
        return fallback_chain

    def extract_song_info(self, song_data: Dict[str, Any]) -> SongInfo:
        song_name = song_data.get('title', '未知歌曲')
        singer_info = song_data.get('singer', [])
        singer_name = (singer_info[0].get('name', '未知歌手')
                       if singer_info and isinstance(singer_info, list)
                       else '未知歌手')
        return SongInfo(
            name=song_name,
            singer=singer_name,
            mid=song_data.get('mid', ''),
            is_vip=song_data.get('pay', {}).get('pay_play', 0) != 0,
            album_name=song_data.get('album', {}).get('name', ''),
            album_mid=song_data.get('album', {}).get('mid', '')
        )

    async def search_songs(self, keyword: str) -> List[Dict[str, Any]]:
        """搜索歌曲（带重试和预热检查）"""
        # 如果还没预热完成，等待一下
        if not self._is_warmed_up:
            self._is_warmed_up = True  # 避免每次都等待
            await asyncio.sleep(0.5)
        
        last_error = None
        for attempt in range(Config.MAX_RETRY):
            try:
                # 添加延迟，避免请求过快
                if attempt > 0:
                    await asyncio.sleep(Config.SEARCH_RETRY_DELAY * attempt)
                
                results = await search.search_by_type(keyword, num=Config.SEARCH_RESULTS_COUNT)
                if not results:
                    raise ValueError("未找到相关歌曲")
                return results
            except Exception as e:
                last_error = e
                if attempt < Config.MAX_RETRY - 1:
                    logger.warning(f"搜索失败 (尝试 {attempt + 1}/{Config.MAX_RETRY}): {e}")
                else:
                    logger.error(f"搜索失败，已达最大重试次数: {e}")
        
        raise DownloadError(f"搜索失败: {last_error}")

    async def download_song(self, song_data: Dict[str, Any], progress_callback=None) -> bool:
        try:
            song_info = self.extract_song_info(song_data)
            safe_filename = FileManager.sanitize_filename(f"{song_info.singer} - {song_info.name}")

            for file_type, quality_name in self._get_quality_strategy():
                file_path = self.download_dir / f"{safe_filename}{file_type.e}"
                
                if file_path.exists():
                    if progress_callback:
                        progress_callback(f"文件已存在: {file_path.name}")
                    return True

                if progress_callback:
                    progress_callback(f"尝试 {quality_name}: {song_info.name}")

                # 未登录状态，credential=None
                urls = await get_song_urls([song_info.mid], file_type=file_type, credential=None)
                url = urls.get(song_info.mid)
                
                if url:
                    session = await self.network.get_session()
                    async with session.get(url) as response:
                        if response.status == 200:
                            content = await response.read()
                            if len(content) > Config.MIN_FILE_SIZE:
                                async with aiofiles.open(file_path, 'wb') as f:
                                    await f.write(content)
                                
                                lyrics_data = None
                                if self.add_lyrics:
                                    try:
                                        lyrics_data = await get_lyric(song_info.mid)
                                    except Exception:
                                        pass
                                
                                if file_path.suffix.lower() == '.flac':
                                    await self.metadata_manager.add_metadata_to_flac(
                                        file_path, song_info, lyrics_data, song_data
                                    )
                                else:
                                    await self.metadata_manager.add_metadata_to_mp3(
                                        file_path, song_info, lyrics_data, song_data
                                    )
                                
                                if progress_callback:
                                    progress_callback(f"✅ 下载成功: {file_path.name}")
                                return True
            return False
        except Exception as e:
            logger.error(f"下载失败: {e}")
            return False


## ==================== GUI应用程序 ====================
class QQMusicDownloaderGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(Config.WINDOW_TITLE)
        self.root.geometry(Config.WINDOW_SIZE)
        self.root.minsize(800, 600)
        
        self.core = QQMusicDownloaderCore()
        self.loop = None
        self.thread = None
        self.is_running = False
        
        self._setup_ui()
        self._setup_menu()
        
        # 启动异步事件循环的线程
        self._start_async_loop()
        
    def _start_async_loop(self):
        """在新线程中运行异步事件循环"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._init_core())
            self.loop.run_forever()
        
        self.thread = threading.Thread(target=run_loop, daemon=True)
        self.thread.start()
    
    async def _init_core(self):
        """初始化核心"""
        await self.core.initialize()
        self._update_status("就绪", "green")
        # 更新UI中的设置
        self.quality_var.set(str(self.core.quality_level))
        self.cover_var.set(self.core.add_cover)
        self.lyrics_var.set(self.core.add_lyrics)
        
    def _setup_ui(self):
        """设置UI"""
        # 创建主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 创建Notebook（选项卡）
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # 创建各个选项卡（移除登录选项卡）
        self._create_search_tab()
        self._create_settings_tab()
        
        # 底部状态栏
        self.status_frame = ttk.Frame(main_frame)
        self.status_frame.pack(fill=tk.X, pady=(5, 0))
        
        self.status_label = ttk.Label(self.status_frame, text="初始化中...")
        self.status_label.pack(side=tk.LEFT)
        
        self.progress_bar = ttk.Progressbar(self.status_frame, mode='indeterminate')
        self.progress_bar.pack(side=tk.RIGHT, padx=5)
        
        # 日志输出区域
        log_frame = ttk.LabelFrame(main_frame, text="日志输出", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 配置日志颜色标签
        self.log_text.tag_config("success", foreground="green")
        self.log_text.tag_config("error", foreground="red")
        self.log_text.tag_config("info", foreground="blue")
        self.log_text.tag_config("warning", foreground="orange")
        
        # 显示信息提示
        self._log("=" * 50)
        self._log("QQ音乐下载器 v1.0")
        self._log("注意：部分VIP歌曲无法下载高音质版本")
        self._log("=" * 50)
        
    def _setup_menu(self):
        """设置菜单栏"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        # 文件菜单
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="选择下载目录", command=self._select_download_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_closing)
        
        # 帮助菜单
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="关于", command=self._show_about)
        
    def _create_search_tab(self):
        """创建单曲搜索下载选项卡"""
        tab = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(tab, text="🎵 单曲搜索")
        
        # 搜索框
        search_frame = ttk.Frame(tab)
        search_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(search_frame, text="歌曲名称:").pack(side=tk.LEFT, padx=(0, 5))
        self.search_entry = ttk.Entry(search_frame, width=50, font=("", 11))
        self.search_entry.pack(side=tk.LEFT, padx=(0, 10))
        self.search_entry.bind('<Return>', lambda e: self._search_songs())
        
        self.search_btn = ttk.Button(search_frame, text="🔍 搜索", command=self._search_songs)
        self.search_btn.pack(side=tk.LEFT)
        
        # 搜索结果列表
        result_frame = ttk.LabelFrame(tab, text="搜索结果", padding="5")
        result_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # 创建Treeview
        columns = ("#", "歌曲名", "歌手", "VIP")
        self.search_tree = ttk.Treeview(result_frame, columns=columns, show="headings", height=10)
        
        self.search_tree.heading("#", text="#")
        self.search_tree.heading("歌曲名", text="歌曲名")
        self.search_tree.heading("歌手", text="歌手")
        self.search_tree.heading("VIP", text="VIP")
        
        self.search_tree.column("#", width=40, anchor="center")
        self.search_tree.column("歌曲名", width=300)
        self.search_tree.column("歌手", width=200)
        self.search_tree.column("VIP", width=50, anchor="center")
        
        scrollbar = ttk.Scrollbar(result_frame, orient=tk.VERTICAL, command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=scrollbar.set)
        
        self.search_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 下载按钮
        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill=tk.X)
        
        self.download_selected_btn = ttk.Button(btn_frame, text="⬇️ 下载选中项", command=self._download_selected)
        self.download_selected_btn.pack(side=tk.LEFT, padx=5)
        
        self.download_all_btn = ttk.Button(btn_frame, text="📦 下载全部项", command=self._download_all_search_results)
        self.download_all_btn.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(btn_frame, text="🗑️ 清空结果", command=self._clear_search_results).pack(side=tk.LEFT, padx=5)
        
        # 提示标签
        tip_label = ttk.Label(tab, text="💡 提示：若日志显示\"[2001] API 返回的响应 code 不符合预期\"时，重新搜索即可", foreground="gray")
        tip_label.pack(pady=(5, 0))
        
        # 存储搜索结果
        self.search_results = []
        
    def _create_settings_tab(self):
        """创建设置选项卡"""
        tab = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(tab, text="⚙️ 设置")
        
        # 音质设置
        quality_frame = ttk.LabelFrame(tab, text="优先尝试下载音质", padding="10")
        quality_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.quality_var = tk.StringVar(value="1")
        
        for key, (name, _) in QQMusicDownloaderCore.QUALITY_OPTIONS.items():
            ttk.Radiobutton(
                quality_frame, text=name, variable=self.quality_var,
                value=str(key), command=self._on_quality_change
            ).pack(anchor=tk.W, pady=2)
        
        # 元数据设置
        metadata_frame = ttk.LabelFrame(tab, text="元数据选项", padding="10")
        metadata_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.cover_var = tk.BooleanVar(value=True)
        self.lyrics_var = tk.BooleanVar(value=True)
        
        ttk.Checkbutton(metadata_frame, text="添加歌曲封面", variable=self.cover_var,
                       command=self._on_cover_change).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(metadata_frame, text="添加歌词", variable=self.lyrics_var,
                       command=self._on_lyrics_change).pack(anchor=tk.W, pady=2)
        
        # 下载目录
        dir_frame = ttk.LabelFrame(tab, text="下载目录", padding="10")
        dir_frame.pack(fill=tk.X)
        
        self.dir_var = tk.StringVar(value=str(Config.MUSIC_DIR))
        
        dir_input_frame = ttk.Frame(dir_frame)
        dir_input_frame.pack(fill=tk.X)
        
        ttk.Entry(dir_input_frame, textvariable=self.dir_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(dir_input_frame, text="浏览", command=self._select_download_dir).pack(side=tk.RIGHT)
        
        # 保存按钮
        ttk.Button(tab, text="💾 保存设置", command=self._save_settings).pack(pady=10)
        
    def _log(self, message: str, tag: str = None):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] {message}\n"
        
        def _update():
            self.log_text.insert(tk.END, log_msg, tag)
            self.log_text.see(tk.END)
        
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            self.root.after(0, _update)
    
    def _update_status(self, message: str, color: str = "black"):
        """更新状态栏"""
        def _update():
            self.status_label.config(text=message, foreground=color)
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            self.root.after(0, _update)
    
    def _start_progress(self):
        """开始进度条动画"""
        def _start():
            self.progress_bar.start(10)
        if threading.current_thread() is threading.main_thread():
            _start()
        else:
            self.root.after(0, _start)
    
    def _stop_progress(self):
        """停止进度条动画"""
        def _stop():
            self.progress_bar.stop()
        if threading.current_thread() is threading.main_thread():
            _stop()
        else:
            self.root.after(0, _stop)
    
    def _run_async(self, coro, callback=None):
        """在异步事件循环中运行协程"""
        def _run():
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            if callback:
                future.add_done_callback(callback)
        threading.Thread(target=_run, daemon=True).start()
    
    async def _search_songs_async(self):
        """异步搜索歌曲"""
        keyword = self.search_entry.get().strip()
        if not keyword:
            self._log("请输入歌曲名称", "warning")
            return
        
        self._log(f"搜索: {keyword}")
        self._start_progress()
        self.search_btn.config(state='disabled')
        
        try:
            results = await self.core.search_songs(keyword)
            self.search_results = results
            
            # 清空Treeview
            for item in self.search_tree.get_children():
                self.search_tree.delete(item)
            
            if results:
                for i, song_data in enumerate(results, 1):
                    song_info = self.core.extract_song_info(song_data)
                    vip_mark = "✓" if song_info.is_vip else ""
                    self.search_tree.insert("", tk.END, values=(i, song_info.name, song_info.singer, vip_mark))
                self._log(f"找到 {len(results)} 首歌曲", "success")
                # 统计VIP歌曲
                vip_count = sum(1 for s in results if s.get('pay', {}).get('pay_play', 0) != 0)
                if vip_count > 0:
                    self._log(f"包含 {vip_count} 首VIP歌曲（可能无法下载高音质）", "warning")
            else:
                self._log("未找到相关歌曲", "warning")
        except Exception as e:
            self._log(f"搜索失败: {e}", "error")
        finally:
            self._stop_progress()
            self.search_btn.config(state='normal')
    
    def _search_songs(self):
        """搜索歌曲"""
        self._run_async(self._search_songs_async())
    
    def _download_selected(self):
        """下载选中的歌曲"""
        selection = self.search_tree.selection()
        if not selection:
            self._log("请先选择要下载的歌曲", "warning")
            return
        
        indices = [int(self.search_tree.item(item, 'values')[0]) - 1 for item in selection]
        songs = [self.search_results[i] for i in indices]
        
        self._download_songs(songs, "选中歌曲")
    
    def _download_all_search_results(self):
        """下载全部搜索结果"""
        if not self.search_results:
            self._log("没有搜索结果", "warning")
            return
        
        self._download_songs(self.search_results, f"全部{len(self.search_results)}首歌曲")
    
    def _download_songs(self, songs: List[Dict], description: str):
        """下载歌曲列表"""
        self._log(f"开始下载 {description}...")
        self._start_progress()
        
        async def _download():
            success = 0
            failed = 0
            total = len(songs)
            
            for i, song in enumerate(songs):
                song_info = self.core.extract_song_info(song)
                vip_tag = " [VIP]" if song_info.is_vip else ""
                self._log(f"[{i+1}/{total}] 下载: {song_info.singer} - {song_info.name}{vip_tag}")
                
                result = await self.core.download_song(song)
                if result:
                    success += 1
                    self._log(f"✅ 成功: {song_info.name}", "success")
                else:
                    failed += 1
                    self._log(f"❌ 失败: {song_info.name} (可能为VIP歌曲或无可用音质)", "error")
            
            self._log(f"下载完成! 成功: {success}, 失败: {failed}", "success" if success > 0 else "error")
            self._stop_progress()
        
        self._run_async(_download())
    
    def _on_quality_change(self):
        """音质改变"""
        self.core.quality_level = int(self.quality_var.get())
        self._log(f"音质已更改为: {QQMusicDownloaderCore.QUALITY_OPTIONS[self.core.quality_level][0]}")
    
    def _on_cover_change(self):
        """封面选项改变"""
        self.core.add_cover = self.cover_var.get()
        self.core.metadata_manager.add_cover = self.core.add_cover
    
    def _on_lyrics_change(self):
        """歌词选项改变"""
        self.core.add_lyrics = self.lyrics_var.get()
        self.core.metadata_manager.add_lyrics = self.core.add_lyrics
    
    def _select_download_dir(self):
        """选择下载目录"""
        directory = filedialog.askdirectory(initialdir=self.dir_var.get())
        if directory:
            self.dir_var.set(directory)
            self.core.download_dir = Path(directory)
            self._log(f"下载目录已更改为: {directory}")
    
    def _save_settings(self):
        """保存设置"""
        self.core.save_settings()
        self._log("设置已保存", "success")
    
    def _clear_search_results(self):
        """清空搜索结果"""
        for item in self.search_tree.get_children():
            self.search_tree.delete(item)
        self.search_results = []
        self._log("搜索结果已清空")
    
    def _show_about(self):
        """显示关于对话框"""
        about_text = """QQ音乐下载器 v1.0

功能特性：
• 单曲搜索下载（支持音质选择）
• 自动添加封面和歌词
• 多音质自动降级
• API预热机制
• 重试机制
• 无需登录即可使用
• 请遵守相关法律法规

文宇香香工作室 出品"""
        messagebox.showinfo("关于", about_text)
    
    def _on_closing(self):
        """关闭窗口"""
        if messagebox.askokcancel("退出", "确定要退出吗？"):
            self.is_running = False
            if self.loop:
                self.loop.call_soon_threadsafe(self.loop.stop)
            self.root.destroy()
    
    def run(self):
        """运行GUI"""
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.mainloop()


def main():
    app = QQMusicDownloaderGUI()
    app.run()


if __name__ == "__main__":
    main()
