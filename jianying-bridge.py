# -*- coding: utf-8 -*-
"""
剪映素材桥接服务 (JianYing Bridge)
====================================
在本地运行的轻量 HTTP 服务，让素材库网页可以一键将视频复制到剪映使用。

功能：
- 接收网页发来的视频 URL，下载到本地固定文件夹
- 缓存已下载的视频（相同 URL 不重复下载）
- 将文件路径复制到剪贴板，方便在剪映导入对话框中粘贴
- 支持批量操作（多段素材一次处理）

使用方法：
  python jianying-bridge.py
  然后打开素材库网页，点击"复制到剪映"按钮即可。

按 Ctrl+C 停止服务。
"""

import os
import sys

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import json
import hashlib
import ctypes
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ========== 配置 ==========
PORT = 8787
# 视频存放目录（可自行修改）
VIDEO_DIR = Path("D:/JianYingVideos")
# 缓存索引文件
INDEX_FILE = VIDEO_DIR / ".index.json"
# ===========================


def set_clipboard(text):
    """将文本复制到 Windows 剪贴板"""
    try:
        ctypes.windll.user32.OpenClipboard(0)
        ctypes.windll.user32.EmptyClipboard()
        data = text.encode("utf-16-le")
        h = ctypes.windll.kernel32.GlobalAlloc(0x0042, len(data) + 2)
        p = ctypes.windll.kernel32.GlobalLock(h)
        ctypes.cdll.msvcrt.memcpy(p, data, len(data) + 2)
        ctypes.windll.kernel32.GlobalUnlock(h)
        ctypes.windll.user32.SetClipboardData(13, h)  # 13 = CF_UNICODETEXT
        ctypes.windll.user32.CloseClipboard()
        return True
    except Exception as e:
        print(f"[!] 剪贴板操作失败: {e}")
        return False


def load_index():
    """加载缓存索引"""
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_index(index):
    """保存缓存索引"""
    INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def url_to_filename(url):
    """根据 URL 生成唯一文件名"""
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    # 尝试从 URL 中提取原始扩展名
    ext = ".mp4"
    path = url.split("?")[0]
    for e in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
        if path.lower().endswith(e):
            ext = e
            break
    return f"{h}{ext}"


def download_video(url, index):
    """
    下载视频到本地。
    返回 (filepath, is_cached)
    """
    filename = url_to_filename(url)
    filepath = VIDEO_DIR / filename

    # 检查缓存：索引中有记录且文件确实存在
    if url in index and filepath.exists():
        print(f"[OK] 缓存命中: {filename}")
        return str(filepath), True

    # 下载
    print(f"[DL] 正在下载: {url[:80]}...")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "JianYingBridge/1.0"
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 256  # 256KB chunks

            with open(filepath, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        print(f"\r[DL] 下载中: {pct}% ({downloaded // 1024}KB / {total // 1024}KB)", end="", flush=True)

        print()  # newline

        # 更新索引
        index[url] = {
            "file": str(filepath),
            "filename": filename,
            "size": downloaded
        }
        save_index(index)

        print(f"[OK] 下载完成: {filename} ({downloaded // 1024}KB)")
        return str(filepath), False

    except Exception as e:
        # 清理未完成的文件
        if filepath.exists():
            filepath.unlink()
        print(f"[ERR] 下载失败: {e}")
        raise


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        """健康检查"""
        if self.path == "/ping":
            index = load_index()
            self._json_response(200, {
                "status": "ok",
                "video_dir": str(VIDEO_DIR),
                "cached_count": len(index)
            })
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/transfer":
            self._handle_transfer()
        elif self.path == "/copy-path":
            self._handle_copy_path()
        else:
            self._json_response(404, {"error": "not found"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _handle_transfer(self):
        """
        下载视频（或命中缓存）+ 复制路径到剪贴板
        请求体: { "urls": ["url1", "url2", ...] }
        返回: { "results": [...], "clipboard": "..." }
        """
        try:
            data = self._read_body()
            urls = data.get("urls", [])
            if not urls:
                self._json_response(400, {"error": "urls 不能为空"})
                return

            index = load_index()
            results = []
            paths = []

            for url in urls:
                try:
                    filepath, is_cached = download_video(url, index)
                    paths.append(filepath)
                    results.append({
                        "url": url,
                        "status": "cached" if is_cached else "downloaded",
                        "file": filepath,
                        "filename": os.path.basename(filepath)
                    })
                except Exception as e:
                    results.append({
                        "url": url,
                        "status": "error",
                        "error": str(e)
                    })

            # 复制路径到剪贴板
            clipboard_text = "\n".join(paths)
            clip_ok = set_clipboard(clipboard_text) if paths else False

            self._json_response(200, {
                "results": results,
                "clipboard": clipboard_text,
                "clipboard_ok": clip_ok,
                "total": len(urls),
                "success": sum(1 for r in results if r["status"] in ("cached", "downloaded")),
                "cached": sum(1 for r in results if r["status"] == "cached"),
                "downloaded": sum(1 for r in results if r["status"] == "downloaded"),
                "errors": sum(1 for r in results if r["status"] == "error")
            })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_copy_path(self):
        """
        仅复制已缓存视频的路径到剪贴板（不下载）
        请求体: { "urls": ["url1", ...] }
        """
        try:
            data = self._read_body()
            urls = data.get("urls", [])
            index = load_index()

            paths = []
            results = []
            for url in urls:
                if url in index and Path(index[url]["file"]).exists():
                    fp = index[url]["file"]
                    paths.append(fp)
                    results.append({"url": url, "status": "cached", "file": fp})
                else:
                    results.append({"url": url, "status": "not_found"})

            clipboard_text = "\n".join(paths)
            clip_ok = set_clipboard(clipboard_text) if paths else False

            self._json_response(200, {
                "results": results,
                "clipboard": clipboard_text,
                "clipboard_ok": clip_ok
            })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def log_message(self, format, *args):
        """自定义日志格式"""
        print(f"[{self.log_date_time_string()}] {format % args}")


def main():
    # 确保目录存在
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=" * 50)
    print(f"  剪映素材桥接服务 v1.0")
    print(f"=" * 50)
    print(f"  视频存储: {VIDEO_DIR}")
    print(f"  服务地址: http://localhost:{PORT}")
    print(f"  按 Ctrl+C 停止")
    print(f"=" * 50)
    print()

    server = HTTPServer(("127.0.0.1", PORT), BridgeHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] 服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
