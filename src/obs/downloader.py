"""磁力下载：libtorrent 跑在 OCS 进程里，不再依赖外部 aria2 守护进程。

之前的方案是「服务端起一个 aria2c，用 JSON-RPC 驱动」。在这台 NAS 的容器里 aria2 会周期性
退出（DHT 反序列化异常 / IPv6 绑端口失败），一退出任务就全丢，进度永远停在几十分之一。
换成 libtorrent 之后：

- 任务句柄活在进程内，不会因为外部进程重启而消失
- 元数据到手立刻能拿到文件列表，先让用户勾选，再按选择下（文件优先级 = 0/4）
- 进度直接读 handle.status()，不用跨进程对账，没有 gid 变来变去的问题

对外仍然沿用 server.py 现在这套接口语义：add / start / list / cancel。
"""

import os
import shutil
import threading
import time
import uuid

_PUMP_INTERVAL = 1.0
_VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts", ".flv", ".wmv", ".mpg", ".mpeg"}
_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}

_lock = threading.Lock()
_tasks = {}          # infohash -> task dict
_handles = {}        # infohash -> libtorrent handle
_session = None
_started = False
_log = print


def set_logger(fn):
    global _log
    _log = fn


class _Config(object):
    save_root = "downloads"      # 每个任务一个子目录
    upload_dir = "obs"           # 收尾时平铺到这里


_config = _Config()


def configure(save_root: str, upload_dir: str) -> None:
    _config.save_root = save_root
    _config.upload_dir = upload_dir


def start_loop() -> None:
    """起后台线程（幂等）。容器里没有 supervisor，线程就是我们的守护进程。"""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_pump_loop, daemon=True, name="lt-pump").start()
    _log("[dl] libtorrent 下载线程已启动")


def _get_session():
    global _session
    if _session is not None:
        return _session
    import libtorrent as lt
    settings = {
        "listen_interfaces": "0.0.0.0:6881-6890",
        "enable_dht": True,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "announce_to_all_trackers": True,
        "alert_mask": 0,
    }
    sess = lt.session(settings)
    for host, port in (("router.bittorrent.com", 6881),
                       ("dht.transmissionbt.com", 6881),
                       ("router.utorrent.com", 6881),
                       ("dht.libtorrent.org", 25401)):
        try:
            sess.add_dht_router(host, port)
        except Exception:
            pass
    _session = sess
    _log("[dl] libtorrent %s 已就绪" % lt.__version__)
    return sess


def add(magnet: str) -> dict:
    """加一个磁力链接；返回 infohash（当成 gid 用，稳定不变）。"""
    import libtorrent as lt
    sess = _get_session()
    params = lt.parse_magnet_uri(magnet)
    ih = str(params.info_hashes.v1)   # sha1_hash 直接 str() 就是 40 位十六进制
    with _lock:
        if ih in _tasks and not _tasks[ih].get("done"):
            return {"ok": True, "gid": ih, "existing": True}
    task_dir = os.path.join(_config.save_root, uuid.uuid4().hex[:16])
    os.makedirs(task_dir, exist_ok=True)
    params.save_path = task_dir
    handle = sess.add_torrent(params)
    with _lock:
        _tasks[ih] = {
            "gid": ih, "name": "", "dir": task_dir, "status": "active",
            "phase": "metadata", "done": False, "error": "",
            "total": 0, "completed": 0, "speed": 0, "seeders": 0, "connections": 0,
            "fileList": [], "files": [], "dropped": 0, "started": time.time(),
        }
        _handles[ih] = handle
    _log("[dl] 新增磁力任务 %s" % ih)
    return {"ok": True, "gid": ih}


def start(gid: str, indexes) -> dict:
    """用户勾好文件后真正开始下载。indexes 是 1 起的文件序号；空表示全下。"""
    with _lock:
        task = _tasks.get(gid)
        handle = _handles.get(gid)
    if not task or handle is None:
        return {"ok": False, "error": "没有这个任务"}
    picked = sorted({int(i) for i in (indexes or []) if str(i).isdigit()})
    try:
        if picked:
            num = len(task["fileList"])
            prio = [0] * num
            for i in picked:
                if 1 <= i <= num:
                    prio[i - 1] = 4
            handle.prioritize_files(prio)
    except Exception as exc:
        _log("[dl] 设置文件优先级失败：%s" % exc)
        return {"ok": False, "error": str(exc)}
    try:
        handle.resume()
    except Exception:
        pass
    with _lock:
        task["phase"] = "active"
        task["picked"] = picked
    _log("[dl] 开始下载 %s，选中 %s" % (gid, picked or "全部"))
    return {"ok": True, "gid": gid, "indexes": picked}


def cancel(gid: str) -> dict:
    with _lock:
        task = _tasks.pop(gid, None)
        handle = _handles.pop(gid, None)
    try:
        if handle is not None:
            _get_session().remove_torrent(handle, 1)   # 1 = 删文件
    except Exception:
        pass
    if task:
        shutil.rmtree(task.get("dir") or "", ignore_errors=True)
    _log("[dl] 取消 %s" % gid)
    return {"ok": True}


def list_tasks() -> dict:
    with _lock:
        items = sorted(_tasks.values(), key=lambda t: t.get("started", 0), reverse=True)
        out = []
        for t in items[:12]:
            total = t.get("total") or 0
            out.append({
                "gid": t["gid"],
                "name": t.get("name") or "(等待元数据)",
                "status": t.get("status"),
                "phase": t.get("phase"),
                "fileList": t.get("fileList") or [],
                "done": bool(t.get("done")),
                "percent": round(100.0 * (t.get("completed") or 0) / total, 1) if total else 0,
                "total": total,
                "completed": t.get("completed") or 0,
                "speed": t.get("speed") or 0,
                "seeders": t.get("seeders") or 0,
                "connections": t.get("connections") or 0,
                "error": t.get("error") or "",
                "files": t.get("files") or [],
                "dropped": t.get("dropped") or 0,
            })
    return {"ok": True, "tasks": out}


def _pump_loop():
    while True:
        try:
            _pump_once()
        except Exception as exc:
            _log("[dl] 轮询异常：%s" % exc)
        time.sleep(_PUMP_INTERVAL)


def _pump_once():
    sess = _get_session()
    with _lock:
        pending = [(ih, h) for ih, h in _handles.items()
                   if ih in _tasks and not _tasks[ih].get("done")]
    for ih, handle in pending:
        if not handle.is_valid():
            with _lock:
                t = _tasks.get(ih)
                if t:
                    t["done"] = True
                    t["status"] = "removed"
                    t["error"] = "任务已不在下载引擎里"
            continue
        st = handle.status()
        with _lock:
            task = _tasks.get(ih)
            if not task:
                continue
            task["connections"] = st.num_peers
            task["seeders"] = st.num_seeds
            task["speed"] = st.download_rate
            if not handle.has_metadata():
                task["phase"] = "metadata"
                continue
            if task["phase"] == "metadata":
                # 元数据到手：列出文件、先把所有文件优先级压到 0（等于暂不下载），等用户勾选
                try:
                    ti = handle.torrent_file()
                    fs = ti.files()
                    listing = []
                    for i in range(fs.num_files()):
                        listing.append({
                            "index": i + 1,
                            "path": fs.file_path(i),
                            "name": os.path.basename(fs.file_path(i)),
                            "size": fs.file_size(i),
                            "selected": True,
                        })
                    task["fileList"] = listing
                    task["name"] = ti.name()
                    task["phase"] = "choose"
                    task["status"] = "paused"
                    # 关键：元数据一到就先把所有文件压到优先级 0，否则它会立刻按「全选」狂下，
                    # 用户还没勾完就已经把整季（十几 GB）拽下来了。
                    try:
                        handle.prioritize_files([0] * len(listing))
                    except Exception as exc:
                        _log("[dl] 暂停下载失败：%s" % exc)
                except Exception as exc:
                    task["error"] = str(exc)
                continue
            if task["phase"] == "choose":
                continue
            # 下载中
            task["total"] = st.total_wanted
            task["completed"] = st.total_wanted_done
            if st.is_finished or (st.total_wanted > 0 and st.total_wanted_done >= st.total_wanted):
                done_files, dropped = _harvest(task)
                task["done"] = True
                task["phase"] = "done"
                task["status"] = "done"
                task["files"] = done_files
                task["dropped"] = dropped
                task["percent"] = 100.0
                try:
                    sess.remove_torrent(handle)
                except Exception:
                    pass
                _log("[dl] %s 下载完成：保留 %d 个文件，丢弃 %d 个" % (ih, len(done_files), dropped))


def _harvest(task: dict):
    """只把视频和字幕搬进 obs 目录（平铺），其余删掉，临时目录清空。"""
    src = task.get("dir") or ""
    upload_dir = _config.upload_dir
    kept, dropped = [], 0
    for root, _dirs, files in os.walk(src):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            path = os.path.join(root, fn)
            if ext in _VIDEO_EXT or ext in _SUB_EXT:
                dest = _unique(upload_dir, fn)
                try:
                    shutil.move(path, dest)
                    kept.append(os.path.basename(dest))
                except Exception as exc:
                    _log("[dl] 搬移失败 %s：%s" % (fn, exc))
            else:
                dropped += 1
    shutil.rmtree(src, ignore_errors=True)
    return kept, dropped


def _unique(upload_dir: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    dest = os.path.join(upload_dir, name)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(upload_dir, "%s (%d)%s" % (base, i, ext))
        i += 1
    return dest
