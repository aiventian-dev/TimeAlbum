#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时光相册 · 只读本地服务

读取 macOS「照片」图库（.photoslibrary），只读取、绝不写入：
  · 数据库用 SQLite 只读 URI（mode=ro&immutable=1）打开，不建锁、不写 WAL；
  · 缩略图只从图库的 derivatives / originals 读取，生成结果缓存在本工具自己的 cache 目录里；
  · 图库里不会出现任何新建或修改的文件。

启动：python3 album_server.py [--port 8899] [--lib "图库路径"]
"""

import bisect
import hashlib
import hmac
import io
import json
import mimetypes
import os
import pickle
import plistlib
import re
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(APP_DIR, "cache")
HTML_FILE = os.path.join(APP_DIR, "时光相册.html")
SIPS = "/usr/bin/sips"

PASS_FILE = os.path.join(APP_DIR, "密码.txt")   # 写一行密码即开启访问密码
COOKIE_NAME = "album_auth"
COOKIE_DAYS = 30
LOGIN_FAILS = {}        # ip -> [次数, 最近时间]，简单节流

THUMB_SIZES = {"s": 260, "m": 540, "l": 1500}
INDEX_VERSION = 8   # 索引结构变化时递增，旧缓存自动失效（8：加入 EXIF 相机/镜头）

def auth_password():
    """密码来源：环境变量 ALBUM_PASSWORD 优先，其次同目录 密码.txt 的第一行。空 = 不启用密码。"""
    p = (os.environ.get("ALBUM_PASSWORD") or "").strip()
    if p:
        return p
    try:
        with io.open(PASS_FILE, encoding="utf-8") as f:
            lines = [x.strip() for x in f.read().splitlines() if x.strip()]
        return lines[0] if lines else ""
    except Exception:
        return ""


def _auth_sig(exp):
    key = ("album-gate|" + auth_password()).encode("utf-8")
    return hmac.new(key, str(exp).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_token():
    exp = int(time.time()) + COOKIE_DAYS * 86400
    return "%d.%s" % (exp, _auth_sig(exp))


def token_ok(tok):
    try:
        exp_s, sig = (tok or "").split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig, _auth_sig(exp))


CONFIG_FILE = os.path.join(APP_DIR, "libraries.json")
FOLDER_INDEX_VERSION = 2   # 2：文件夹库也读相机型号
IMG_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp", ".gif")


# ---------------------------------------------------------------- 配置


def _new_id(path):
    return hashlib.md5(path.encode("utf-8")).hexdigest()[:8]


def detect_kind(path):
    """判断一个路径是「照片」图库还是普通文件夹；不是图库也不是目录返回 None。"""
    path = os.path.expanduser(path.strip().rstrip("/"))
    if not os.path.isdir(path):
        return None, path
    if path.endswith(".photoslibrary") and os.path.exists(os.path.join(path, "database", "Photos.sqlite")):
        return "photos", path
    if os.path.isdir(path):
        return "folder", path
    return None, path


def load_config():
    """返回库列表。首次运行时把自动发现的 .photoslibrary 写进配置。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("libraries") if isinstance(data, dict) else data
            if isinstance(entries, list):
                out = []
                for e in entries:
                    if not isinstance(e, dict) or not e.get("path"):
                        continue
                    out.append({"id": e.get("id") or _new_id(e["path"]),
                                "name": e.get("name") or os.path.basename(e["path"]),
                                "path": e["path"],
                                "kind": e.get("kind") or ("photos" if e["path"].endswith(".photoslibrary") else "folder")})
                return out
        except Exception as exc:
            print("  (配置读取失败，重建：%s)" % exc)
    entries = []
    for path in discover_libraries():
        entries.append({"id": _new_id(path),
                        "name": os.path.basename(path).replace(".photoslibrary", ""),
                        "path": path, "kind": "photos"})
    save_config(entries)
    return entries


def save_config(entries):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"libraries": entries}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)

# ---------------------------------------------------------------- 图库发现


def discover_libraries(extra=None):
    """在当前用户可见范围内找出所有 .photoslibrary。"""
    found = []
    roots = [os.path.expanduser("~/Pictures"), "/Volumes"]
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name.endswith(".photoslibrary"):
                found.append(entry.path)
                continue
            # /Volumes 下面一层（外接盘根目录）
            if root == "/Volumes":
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_dir() and sub.name.endswith(".photoslibrary"):
                            found.append(sub.path)
                except OSError:
                    pass
    if extra:
        for p in extra:
            if p and os.path.isdir(p):
                found.append(p)
    out = []
    for p in found:
        if os.path.exists(os.path.join(p, "database", "Photos.sqlite")) and p not in out:
            out.append(p)
    return out




# ---------------------------------------------------------------- 地点


def _arch_val(objs, v):
    """NSKeyedArchiver 里字符串是以 UID 引用的，需要回表。"""
    if isinstance(v, plistlib.UID):
        i = v.data
        return objs[i] if 0 <= i < len(objs) else None
    return v


def decode_place(blob):
    """解析 Photos 存在 ZREVERSELOCATIONDATA 里的归档，得到可读地点：
    「兴趣点 · 省市区」或退化成「省市区」。"""
    if not blob:
        return ""
    try:
        arch = plistlib.loads(bytes(blob))
    except Exception:
        return ""
    objs = arch.get("$objects") or []

    def txt(v):
        v = _arch_val(objs, v)
        return v if isinstance(v, str) and v and v != "$null" else ""

    pm = next((o for o in objs if isinstance(o, dict) and "_formattedAddress" in o), None)
    poi = ""
    for o in objs:
        if isinstance(o, dict) and "placeType" in o:
            poi = txt(o.get("name"))
            if poi:
                break
    admin = ""
    if pm:
        bits = []
        country = txt(pm.get("_country"))
        if country and country != "中国":
            bits.append(country)
        for k in ("_subAdministrativeArea", "_city", "_subLocality"):
            v = txt(pm.get(k))
            if v and not any(v in b for b in bits):
                bits.append(v)
        admin = "".join(bits)
    elif not poi:
        for o in objs:
            if isinstance(o, dict) and "addressString" in o:
                admin = txt(o.get("addressString"))
                if admin:
                    break
    if poi and admin and poi in admin:
        poi = ""
    label = " · ".join([x for x in [poi, admin] if x])
    return label[:44]


# ---------------------------------------------------------------- 图库数据


class Library:
    def __init__(self, path):
        self.path = path
        self.kind = "photos"
        self.name = os.path.basename(path).replace(".photoslibrary", "") or "照片图库"
        self.key = hashlib.md5(path.encode("utf-8")).hexdigest()[:10]
        self.db = os.path.join(path, "database", "Photos.sqlite")
        self.derivatives = os.path.join(path, "resources", "derivatives")
        self.originals = os.path.join(path, "originals")
        self._lock = threading.Lock()
        self._assets = None      # dict uuid -> asset dict
        self._tree = None
        self._days = None        # "YYYY-MM-DD" -> [uuid]
        self._order = []         # 有照片的日期，升序

    # -- 索引 ---------------------------------------------------------
    @property
    def cache_file(self):
        return os.path.join(CACHE_DIR, "index", self.key + ".pickle")

    def assets(self):
        if self._assets is not None:
            return self._assets
        with self._lock:
            if self._assets is not None:
                return self._assets
            data = self._load_cache()
            if data is None:
                data = self._scan()
                self._save_cache(data)
            self._assets, self._tree, self._days = data[0], data[1], data[2]
            self.cams = data[3] if len(data) > 3 else []
            self._order = sorted(self._days.keys())
            return self._assets

    def _load_cache(self):
        try:
            st = os.stat(self.db)
            with open(self.cache_file, "rb") as f:
                blob = pickle.load(f)
            if (blob.get("ver") == INDEX_VERSION and blob.get("mtime") == st.st_mtime
                    and blob.get("size") == st.st_size):
                self._order = blob["order"]
                return blob["assets"], blob["tree"], blob["days"], blob.get("cams") or []
        except Exception:
            return None
        return None

    def _save_cache(self, data):
        try:
            st = os.stat(self.db)
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump({"ver": INDEX_VERSION, "mtime": st.st_mtime, "size": st.st_size,
                             "assets": data[0], "tree": data[1], "days": data[2],
                             "cams": data[3] if len(data) > 3 else [],
                             "order": sorted(data[2].keys())}, f, 4)
            os.replace(tmp, self.cache_file)
        except Exception as exc:
            print("  (索引缓存写入失败，忽略：%s)" % exc)

    def _scan(self):
        uri = "file:" + urllib.parse.quote(self.db, safe="/") + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.row_factory = None
        sql = ("SELECT a.ZUUID, a.ZKIND, a.ZDATECREATED, a.ZWIDTH, a.ZHEIGHT, a.ZFAVORITE, "
               "a.ZDURATION, a.ZDIRECTORY, a.ZFILENAME, a.ZLATITUDE, a.ZLONGITUDE, "
               "b.ZREVERSELOCATIONDATA, "
               "e.ZCAMERAMAKE, e.ZCAMERAMODEL, e.ZLENSMODEL, e.ZISO, e.ZAPERTURE, "
               "e.ZSHUTTERSPEED, e.ZFOCALLENGTH, e.ZFOCALLENGTHIN35MM, e.ZFLASHFIRED FROM ZASSET a "
               "LEFT JOIN ZADDITIONALASSETATTRIBUTES b ON b.ZASSET = a.Z_PK "
               "LEFT JOIN ZEXTENDEDATTRIBUTES e ON e.ZASSET = a.Z_PK "
               "WHERE a.ZTRASHEDSTATE = 0 AND a.ZDATECREATED > 0 AND a.ZHIDDEN = 0 "
               "AND a.ZKIND = 0")
        rows = con.execute(sql).fetchall()
        con.close()

        assets = {}
        days = {}
        cams = []
        cam_idx = {}
        for (uuid, kind, dcre, w, h, fav, dur, directory, filename, lat, lon, rev,
             make, model, lens, iso, ap, tv, fl, fl35, flash) in rows:
            if not uuid:
                continue
            ts = int(round(dcre + 978307200))
            tm = time.localtime(ts)
            if tm.tm_year < 1990 or tm.tm_year > 2100:
                continue
            key = uuid.upper()
            if not os.path.exists(os.path.join(self.derivatives, key[0], key + "_1_105_c.jpeg")):
                pass
            assets[key] = {
                "u": key,
                "t": ts,
                "k": 1 if kind == 1 else 0,
                "w": int(w or 0),
                "h": int(h or 0),
                "f": 1 if fav else 0,
                "d": float(dur or 0),
                "dir": directory or "",
                "fn": filename or "",
                "la": round(float(lat), 5) if lat and -90 < float(lat) < 90 else 0.0,
                "lo": round(float(lon), 5) if lon and -180 < float(lon) < 180 else 0.0,
                "p": decode_place(rev),
            }
            if model or make:
                triple = ((make or "").strip(), (model or "").strip(), (lens or "").strip())
                ci = cam_idx.get(triple)
                if ci is None:
                    ci = len(cams)
                    cam_idx[triple] = ci
                    cams.append({"m": triple[0], "d": triple[1], "l": triple[2]})
                assets[key]["x"] = [ci,
                                    int(iso) if iso else 0,
                                    round(float(ap), 2) if ap else 0,
                                    float(tv) if tv else 0,
                                    round(float(fl), 1) if fl else 0,
                                    int(fl35) if fl35 else 0,
                                    1 if flash else 0]
            dk = "%04d-%02d-%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday)
            days.setdefault(dk, []).append(key)

        for lst in days.values():
            lst.sort(key=lambda u: assets[u]["t"])
        tree = self._build_tree(assets, days)
        return assets, tree, days, cams

    @staticmethod
    def _build_tree(assets, days):
        def rank(a):
            # 封面优先：照片 > 视频，收藏 > 普通，画幅越大越靠前
            return (a["k"] == 0, a["f"], a["w"] * a["h"])
        years = {}
        for key, a in assets.items():
            tm = time.localtime(a["t"])
            y = tm.tm_year
            m = tm.tm_mon
            d = tm.tm_mday
            ynode = years.setdefault(y, {"y": y, "n": 0, "p": 0, "v": 0,
                                         "cover": None, "months": {}})
            ynode["n"] += 1
            ynode["v"] += a["k"]
            ynode["p"] += 1 - a["k"]
            mnode = ynode["months"].setdefault(m, {"m": m, "n": 0, "cover": None, "days": {}})
            mnode["n"] += 1
            dnode = mnode["days"].setdefault(d, {"d": d, "n": 0, "cover": None})
            dnode["n"] += 1
            for node in (ynode, mnode, dnode):
                if node["cover"] is None:
                    node["cover"] = key
                else:
                    if rank(assets[key]) > rank(assets[node["cover"]]):
                        node["cover"] = key
        out = []
        for y in sorted(years):
            ynode = years[y]
            months = []
            for m in sorted(ynode["months"]):
                mnode = ynode["months"][m]
                days_out = [{"d": d, "n": mnode["days"][d]["n"], "cover": mnode["days"][d]["cover"]}
                            for d in sorted(mnode["days"])]
                months.append({"m": m, "n": mnode["n"], "cover": mnode["cover"], "days": days_out})
            out.append({"y": y, "n": ynode["n"], "p": ynode["p"], "v": ynode["v"],
                        "cover": ynode["cover"], "months": months})
        return out

    # -- 文件定位 -----------------------------------------------------
    def derivative_thumb(self, uuid):
        c = uuid[0]
        p = os.path.join(self.derivatives, c, uuid + "_1_105_c.jpeg")
        if os.path.exists(p):
            return p
        return None

    def derivative_master(self, uuid):
        c = uuid[0]
        p = os.path.join(self.derivatives, "masters", c, uuid + "_4_5005_c.jpeg")
        if os.path.exists(p):
            return p
        return None

    ORIGINAL_ROOTS = ("originals", "scopes/cloudsharing/data")

    def original_path(self, uuid):
        a = self.assets().get(uuid)
        if not a:
            return None
        d, fn = a["dir"], a["fn"]
        if d.startswith("/"):
            p = os.path.join(d, fn)
            return p if os.path.exists(p) else None
        for root in self.ORIGINAL_ROOTS:   # 共享相册的原片可能在 scopes 目录下
            p = os.path.join(self.path, root, d, fn)
            if os.path.exists(p):
                return p
        return None

    def any_derivative(self, uuid):
        """兜底：命名的衍生图缺失时，找同 UUID 开头的其它衍生图。"""
        c = uuid[0]
        for folder in (os.path.join(self.derivatives, c),
                       os.path.join(self.derivatives, "masters", c),
                       os.path.join(self.originals, c)):
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        if entry.name.upper().startswith(uuid) and entry.is_file():
                            return entry.path
            except OSError:
                continue
        return None

    VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mpg", ".3gp")

    def source_for_thumb(self, uuid):
        p = self.derivative_thumb(uuid) or self.derivative_master(uuid)
        if p:
            return p
        o = self.original_path(uuid)
        if o and os.path.splitext(o)[1].lower() not in self.VIDEO_EXT:
            return o                      # 新导入的照片还没生成衍生图，直接压原片
        return self.any_derivative(uuid)

    def source_for_large(self, uuid):
        a = self.assets().get(uuid) or {}
        p = None
        if a.get("k") != 1:            # 视频不能拿原片去转码，用封面帧
            p = self.original_path(uuid)
        if p:
            return p
        return (self.derivative_thumb(uuid) or self.derivative_master(uuid)
                or self.any_derivative(uuid))

    def thumb_cache_path(self, uuid, size):
        return os.path.join(CACHE_DIR, "thumbs", self.key, size, uuid[0], uuid + ".jpg")

    def make_thumb(self, uuid, size):
        """生成并缓存缩略图，返回本地文件路径（失败返回 None）。"""
        dest = self.thumb_cache_path(uuid, size)
        if os.path.exists(dest):
            return dest
        src = self.source_for_large(uuid) if size == "l" else self.source_for_thumb(uuid)
        if src and os.path.splitext(src)[1].lower() in self.VIDEO_EXT:
            src = self.derivative_thumb(uuid) or self.derivative_master(uuid) or self.any_derivative(uuid)
        if not src:
            return None
        px = THUMB_SIZES[size]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = "%s.%d.tmp" % (dest, threading.get_ident())
        cmd = [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "86"]
        if size == "l":
            a = self.assets().get(uuid) or {}
            longest = max(a.get("w", 0), a.get("h", 0))
            if longest and longest < px:
                px = int(longest)          # 不放大，老照片保持原貌
        cmd += ["-Z", str(max(px, 64)), src, "--out", tmp]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            os.replace(tmp, dest)
            return dest
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None



class FolderLibrary:
    """普通文件夹：递归找图片文件，用 sips 读尺寸与拍摄时间（读不到退回文件修改时间）。"""

    kind = "folder"

    def __init__(self, path, name=None):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.name = name or os.path.basename(self.path) or "文件夹"
        self.key = hashlib.md5(("folder:" + self.path).encode("utf-8")).hexdigest()[:10]
        self._lock = threading.Lock()
        self._assets = None
        self._tree = None
        self._days = None
        self._order = []

    @property
    def cache_file(self):
        return os.path.join(CACHE_DIR, "index", "f" + self.key + ".pickle")

    def assets(self):
        if self._assets is not None:
            if time.time() - getattr(self, "_sig_ts", 0) < 5:
                return self._assets
            if self._signature() == getattr(self, "_sig_val", None):
                self._sig_ts = time.time()
                return self._assets
            self._assets = None            # 有新照片 / 删了照片：清内存重新扫
        with self._lock:
            if self._assets is not None:
                return self._assets
            data = self._load_cache()
            if data is None:
                data = self._scan()
                self._save_cache(data)
            self._assets, self._tree, self._days = data[0], data[1], data[2]
            self.cams = data[3] if len(data) > 3 else []
            self._order = sorted(self._days.keys())
            self._sig_val = self._signature()
            self._sig_ts = time.time()
            return self._assets

    def _signature(self):
        """整棵目录树的签名：每个子目录的 mtime/size。往任何一个文件夹里放新照片，
        该目录的 mtime 就变，签名随之变化，索引自动重扫。"""
        try:
            parts = []
            for dp, dns, fns in os.walk(self.path):
                dns[:] = [d for d in dns if not d.startswith(".")]
                try:
                    st = os.stat(dp)
                    parts.append("%s:%d:%d" % (dp, int(st.st_mtime), st.st_size))
                except OSError:
                    pass
            return "%d|%s" % (len(parts), hashlib.md5("|".join(parts).encode("utf-8")).hexdigest())
        except Exception:
            return "-"

    def _load_cache(self):
        try:
            sig = self._signature()
            with open(self.cache_file, "rb") as f:
                blob = pickle.load(f)
            if blob.get("ver") == FOLDER_INDEX_VERSION and blob.get("sig") == sig:
                self._sig_val, self._sig_ts = sig, time.time()
                return blob["assets"], blob["tree"], blob["days"], blob.get("cams") or []
        except Exception:
            return None
        return None

    def _save_cache(self, data):
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump({"ver": FOLDER_INDEX_VERSION, "sig": self._signature(),
                             "assets": data[0], "tree": data[1], "days": data[2],
                             "cams": data[3] if len(data) > 3 else []}, f, 4)
            os.replace(tmp, self.cache_file)
        except Exception as exc:
            print("  (索引缓存写入失败，忽略：%s)" % exc)

    def files(self):
        for dp, dns, fns in os.walk(self.path):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for fn in fns:
                if fn.startswith("."):
                    continue
                if os.path.splitext(fn)[1].lower() in IMG_EXT:
                    yield os.path.join(dp, fn)

    @staticmethod
    def probe(path):
        w = h = 0
        ts = None
        make = model = ""
        try:
            out = subprocess.run([SIPS, "-g", "all", path],
                                 capture_output=True, timeout=30, text=True).stdout
        except Exception:
            out = ""
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "pixelWidth" and v.isdigit():
                w = int(v)
            elif k == "pixelHeight" and v.isdigit():
                h = int(v)
            elif k == "make":
                make = v
            elif k == "model":
                model = v
            elif k == "creation" and v:
                try:
                    ts = int(time.mktime(time.strptime(v, "%Y:%m:%d %H:%M:%S")))
                except ValueError:
                    ts = None
        if ts is None:
            try:
                ts = int(os.stat(path).st_mtime)
            except OSError:
                ts = None
        return w, h, ts, make, model

    def _scan(self):
        files = list(self.files())
        total = len(files)
        t0 = time.time()
        print("  扫描文件夹 %s（%d 个图片文件）…" % (self.name, total), flush=True)
        assets = {}
        days = {}
        cams = []
        cam_idx = {}
        done = [0]

        def work(p):
            done[0] += 1
            if done[0] % 500 == 0:
                print("    %d/%d" % (done[0], total), flush=True)
            return (p,) + self.probe(p)

        with ThreadPoolExecutor(max_workers=8) as ex:
            for path, w, h, ts, make, model in ex.map(work, files):
                if not ts:
                    continue
                tm = time.localtime(ts)
                if tm.tm_year < 1900 or tm.tm_year > 2100:
                    continue
                rel = os.path.relpath(path, self.path)
                key = hashlib.md5(("f:" + rel).encode("utf-8")).hexdigest().upper()
                assets[key] = {"u": key, "t": ts, "k": 0, "w": w, "h": h, "f": 0, "d": 0,
                               "src": path, "dir": os.path.dirname(rel), "fn": os.path.basename(rel),
                               "la": 0.0, "lo": 0.0, "p": ""}
                if model or make:
                    triple = (make.strip(), model.strip(), "")
                    ci = cam_idx.get(triple)
                    if ci is None:
                        ci = len(cams)
                        cam_idx[triple] = ci
                        cams.append({"m": triple[0], "d": triple[1], "l": ""})
                    assets[key]["x"] = [ci, 0, 0, 0, 0, 0, 0]
                dk = "%04d-%02d-%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday)
                days.setdefault(dk, []).append(key)
        for lst in days.values():
            lst.sort(key=lambda u: assets[u]["t"])
        tree = Library._build_tree(assets, days)
        print("  完成：%d 张，%d 种机型，用时 %.1fs" % (len(assets), len(cams), time.time() - t0), flush=True)
        return assets, tree, days, cams

    def source_for_thumb(self, uuid):
        a = self.assets().get(uuid)
        return a["src"] if a else None

    def source_for_large(self, uuid):
        return self.source_for_thumb(uuid)

    def thumb_cache_path(self, uuid, size):
        return os.path.join(CACHE_DIR, "thumbs", self.key, size, uuid[0], uuid + ".jpg")

    def make_thumb(self, uuid, size):
        dest = self.thumb_cache_path(uuid, size)
        if os.path.exists(dest):
            return dest
        src = self.source_for_thumb(uuid)
        if not src or not os.path.exists(src):
            return None
        px = THUMB_SIZES[size]
        if size == "l":
            a = self.assets().get(uuid) or {}
            longest = max(a.get("w", 0), a.get("h", 0))
            if longest and longest < px:
                px = int(longest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = "%s.%d.tmp" % (dest, threading.get_ident())
        cmd = [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "86",
               "-Z", str(max(px, 64)), src, "--out", tmp]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=90)
            os.replace(tmp, dest)
            return dest
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None


# ---------------------------------------------------------------- 服务

LIBS = []          # type: list
LIBS_LOCK = threading.Lock()
LIBS_SIG = [None]
EXTRA_LIBS = []          # 命令行 --lib 传进来的路径，每次重建都要带上


def build_library(entry):
    if entry.get("kind") == "folder":
        return FolderLibrary(entry["path"], entry.get("name"))
    return Library(entry["path"])


def ensure_libs(extra=None):
    """按 libraries.json 构建库实例；配置变了就重建（不重启服务）。"""
    global LIBS
    entries = load_config()
    extra = list(extra or []) + EXTRA_LIBS
    if extra:
        have = {e["path"] for e in entries}
        for p in extra:
            if p and p not in have:
                kind, path = detect_kind(p)
                if kind:
                    entries.append({"id": _new_id(path), "name": os.path.basename(path).replace(".photoslibrary", ""),
                                    "path": path, "kind": kind})
    sig = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    with LIBS_LOCK:
        if LIBS_SIG[0] != sig:
            LIBS = [build_library(e) for e in entries]
            LIBS_SIG[0] = sig
        return LIBS


def add_library(path):
    kind, path = detect_kind(path)
    if not kind:
        raise ValueError("路径不存在或不是文件夹：%s" % path)
    entries = load_config()
    if any(e["path"] == path for e in entries):
        raise ValueError("这个库已经在列表里了")
    entry = {"id": _new_id(path),
             "name": os.path.basename(path).replace(".photoslibrary", "") or "照片图库",
             "path": path, "kind": kind}
    entries.append(entry)
    save_config(entries)
    ensure_libs()
    return entry


def rescan_library(lib_id):
    """丢掉这个库的内存索引与磁盘索引，下次请求重新扫描。"""
    entries = load_config()
    entry = next((e for e in entries if e["id"] == lib_id), None)
    if not entry:
        raise ValueError("没有这个库")
    with LIBS_LOCK:
        for lib in LIBS:
            if lib.path == entry["path"]:
                try:
                    if os.path.exists(lib.cache_file):
                        os.remove(lib.cache_file)
                except OSError:
                    pass
                lib._assets = None
                lib._tree = None
                lib._days = None
                lib._order = []
    return entry


def remove_library(lib_id):
    entries = load_config()
    keep = [e for e in entries if e["id"] != lib_id]
    if len(keep) == len(entries):
        raise ValueError("没有这个库")
    dropped = [e for e in entries if e["id"] == lib_id][0]
    save_config(keep)
    ensure_libs()
    with LIBS_LOCK:
        for lib in LIBS:
            if lib.path == dropped["path"]:
                try:
                    if os.path.exists(lib.cache_file):
                        os.remove(lib.cache_file)
                except OSError:
                    pass
    return dropped


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TimeAlbum/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("ALBUM_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def log_request(self, code="-", size="-"):
        """每个请求都记一行：谁、什么路径、返回了什么码。排「外部访问打不开」时先看这里。"""
        try:
            line = "%s %s %s -> %s (%s)" % (self.address_string(), getattr(self, "command", "-"),
                                            getattr(self, "path", "-"), code, size)
        except Exception:
            line = "?"
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        try:
            with io.open(os.path.join(CACHE_DIR, "server.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except Exception:
            pass

    # -- 访问密码 ---------------------------------------------------
    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == name:
                    return v.strip()
        return ""

    def _authed(self):
        if not auth_password():
            return True
        return token_ok(self._cookie(COOKIE_NAME))

    def _login_html(self, msg="", status=200):
        body = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>时光相册 · 进入</title><style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#100e0b;color:#efe7d6;font-family:-apple-system,"Songti SC",serif}
.box{width:min(88vw,360px);text-align:center}
.t{font-family:Didot,Baskerville,"Songti SC",serif;font-size:34px;letter-spacing:.06em}
.s{font-size:11px;color:#8a8172;letter-spacing:.16em;margin-top:8px;text-transform:uppercase}
form{margin-top:34px}
input{width:100%;padding:12px 14px;background:#1a1713;border:1px solid #2c2721;color:#efe7d6;
font-size:14px;letter-spacing:.06em;outline:none;text-align:center;font-family:inherit}
input:focus{border-color:#c79a5b}
button{margin-top:14px;width:100%;padding:12px;background:transparent;border:1px solid #c79a5b;
color:#c79a5b;font-size:13px;letter-spacing:.22em;cursor:pointer;font-family:inherit;transition:.2s}
button:hover{background:#c79a5b;color:#100e0b}
.e{margin-top:14px;font-size:12px;color:#c0714e;letter-spacing:.04em}
</style></head><body><div class="box">
<div class="t">时光相册</div><div class="s">read only archive</div>
<form method="post" action="/login">
<input name="p" type="password" placeholder="访问密码" autofocus autocomplete="current-password">
<button type="submit">进 入</button></form>
""" + ("""<div class="e">""" + msg + "</div>" if msg else "") + """
</div></body></html>"""
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- helpers -----------------------------------------------------
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, status=200, cache="public, max-age=604800"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, cache="public, max-age=2592000", attachment=False):
        try:
            st = os.stat(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        etag = '"%s"' % hashlib.md5(("%s|%s|%s" % (path, st.st_mtime, st.st_size)).encode()).hexdigest()
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _stream_video(self, path):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "video/mp4"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
        if start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/login":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                form = urllib.parse.parse_qs((raw or b"").decode("utf-8"))
                given = (form.get("p") or [""])[0].strip()
                pw = auth_password()
                ip = self.client_address[0]
                rec = LOGIN_FAILS.setdefault(ip, [0, 0])
                if pw and hmac.compare_digest(given, pw):
                    LOGIN_FAILS.pop(ip, None)
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie",
                        COOKIE_NAME + "=" + make_token() + "; Path=/; Max-Age=" + str(COOKIE_DAYS * 86400) + "; SameSite=Lax")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rec[0] += 1
                rec[1] = int(time.time())
                time.sleep(min(3.0, 0.5 * rec[0]))     # 连续失败逐步拖慢
                return self._login_html("密码不对，再试一次", 401)
            except (BrokenPipeError, ConnectionResetError):
                return
        if not self._authed():
            self.close_connection = True
            return self._json({"ok": False, "error": "need password"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            if path == "/api/library/add":
                entry = add_library(payload.get("path", ""))
                libs = ensure_libs()
                idx = next((i for i, l in enumerate(libs) if l.path == entry["path"]), -1)
                count = 0
                if idx >= 0:
                    libs[idx].assets()
                    count = len(libs[idx]._assets)
                return self._json({"ok": True, "library": dict(entry, i=idx), "count": count})
            if path == "/api/library/remove":
                dropped = remove_library(payload.get("id", ""))
                return self._json({"ok": True, "removed": dropped})
            if path == "/api/library/check":
                ident = payload.get("id", "")
                entry = next((e for e in load_config() if e.get("id") == ident), None)
                if not entry:
                    return self._json({"ok": False, "error": "没有这个库"}, 404)
                lib = next((l for l in ensure_libs() if l.path == entry["path"]), None)
                if lib is None:
                    return self._json({"ok": False, "error": "库还没加载"}, 404)
                if getattr(lib, "kind", "") != "folder":
                    return self._json({"ok": True, "changed": False, "supported": False,
                                       "count": 0})
                before = getattr(lib, "_sig_val", None) or lib._signature()
                lib._sig_ts = 0                    # 强制校验目录树签名
                lib.assets()                       # 有变化会自动重扫
                changed = getattr(lib, "_sig_val", None) != before
                return self._json({"ok": True, "changed": changed, "supported": True,
                                   "count": len(lib._assets or {})})
            if path == "/api/library/rescan":
                entry = rescan_library(payload.get("id", ""))
                return self._json({"ok": True, "library": entry})
            return self._json({"error": "not found"}, 404)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            sys.stderr.write("POSTERR %s: %s\n" % (path, exc))
            try:
                return self._json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

    @staticmethod
    def _month_items(lib, ym):
        a_map = lib._assets
        out = []
        for day, keys in lib._days.items():
            if not day.startswith(ym):
                continue
            for u in keys:
                a = a_map.get(u)
                if not a:
                    continue
                item = {"u": u, "t": a["t"], "k": a["k"], "w": a["w"], "h": a["h"], "d": a["d"]}
                if a["p"]:
                    item["p"] = a["p"]
                if a["la"]:
                    item["la"] = a["la"]
                    item["lo"] = a["lo"]
                if a.get("x"):
                    item["x"] = a["x"]
                out.append(item)
        out.sort(key=lambda it: it["t"])
        return out

    @staticmethod
    def _day_items(lib, day):
        a_map = lib._assets
        out = []
        for u in lib._days.get(day, []):
            a = a_map[u]
            item = {"u": u, "t": a["t"], "k": a["k"], "w": a["w"], "h": a["h"], "d": a["d"]}
            if a["p"]:
                item["p"] = a["p"]
            if a["la"]:
                item["la"] = a["la"]
                item["lo"] = a["lo"]
            if a.get("x"):
                item["x"] = a["x"]
            out.append(item)
        return out

    def _lib(self, idx):
        libs = ensure_libs()
        try:
            i = int(idx)
            return libs[i] if 0 <= i < len(libs) else None
        except (TypeError, ValueError):
            return None

    # -- 路由 -----------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        q = urllib.parse.parse_qs(parsed.query)

        if path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", COOKIE_NAME + "=; Path=/; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._authed():
            if path in ("/", "/index.html", "/时光相册.html", "/login"):
                return self._login_html()
            body = b'{"error":"need password"}'
            self.close_connection = True
            try:
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass
            return

        try:
            if path in ("/", "/index.html", "/时光相册.html"):
                return self._file(HTML_FILE, cache="no-cache")
            if path == "/api/auth":
                return self._json({"auth": bool(auth_password())})
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/libraries":
                entries = load_config()
                libs = ensure_libs()
                by_path = {e["path"]: e for e in entries}
                out = []
                for i, l in enumerate(libs):
                    e = by_path.get(l.path) or {}
                    out.append({"i": i, "id": e.get("id") or _new_id(l.path), "name": l.name,
                                "path": l.path, "kind": getattr(l, "kind", "photos"),
                                "exists": os.path.isdir(l.path)})
                return self._json(out)
            if path == "/api/tree":
                lib = self._lib(q.get("lib", ["0"])[0])
                if not lib:
                    return self._json({"error": "no library"}, 404)
                t0 = time.time()
                assets = lib.assets()
                statics = {"total": len(assets),
                           "photos": sum(1 - a["k"] for a in assets.values()),
                           "videos": sum(a["k"] for a in assets.values()),
                           "scan_ms": int((time.time() - t0) * 1000)}
                return self._json({"lib": lib.name, "statics": statics, "years": lib._tree,
                                   "cams": getattr(lib, "cams", []) or []})
            if path == "/api/month":
                lib = self._lib(q.get("lib", ["0"])[0])
                if not lib:
                    return self._json({"error": "no library"}, 404)
                lib.assets()
                return self._json(self._month_items(lib, q.get("ym", [""])[0]))
            if path == "/api/day":
                lib = self._lib(q.get("lib", ["0"])[0])
                if not lib:
                    return self._json({"error": "no library"}, 404)
                day = q.get("d", [""])[0]
                lib.assets()
                return self._json(self._day_items(lib, day))
            if path == "/api/stream":
                # 以某一天为锚点，连续返回前后若干个「日」的照片块
                lib = self._lib(q.get("lib", ["0"])[0])
                if not lib:
                    return self._json({"error": "no library"}, 404)
                lib.assets()
                days = lib._order
                if not days:
                    return self._json({"blocks": [], "hasOlder": False, "hasNewer": False})
                anchor = q.get("d", [days[0]])[0]
                try:
                    n = max(1, min(12, int(q.get("n", ["5"])[0])))
                except ValueError:
                    n = 5
                direction = q.get("dir", [""])[0]
                idx = bisect.bisect_left(days, anchor)
                if direction == "older":
                    start, end = max(0, idx - n), idx
                elif direction == "newer":
                    start, end = idx + 1, min(len(days), idx + 1 + n)
                else:
                    before = max(0, min(6, int(q.get("before", ["2"])[0]) or 0))
                    after = max(0, min(6, int(q.get("after", ["2"])[0]) or 0))
                    if idx >= len(days) or days[idx] != anchor:
                        idx = max(0, min(idx, len(days) - 1))
                    start, end = max(0, idx - before), min(len(days), idx + after + 1)
                blocks = []
                for dd in days[start:end]:
                    items = self._day_items(lib, dd)
                    blocks.append({"d": dd, "n": len(items), "items": items})
                return self._json({"anchor": anchor, "blocks": blocks,
                                   "oldest": days[start] if blocks else None,
                                   "newest": days[end - 1] if blocks else None,
                                   "hasOlder": start > 0, "hasNewer": end < len(days)})
            if path == "/api/randomsample":
                # 给首页年份条挑几个额外的代表图（按年返回 offset 抽样）
                lib = self._lib(q.get("lib", ["0"])[0])
                if not lib:
                    return self._json({"error": "no library"}, 404)
                year = q.get("y", [""])[0]
                try:
                    year = int(year)
                except ValueError:
                    return self._json([])
                lib.assets()
                picks = []
                for d in lib._days:
                    if d.startswith(str(year)) and (d.endswith("-01") or d.endswith("-15")):
                        uu = lib._days[d]
                        if uu:
                            picks.append(uu[len(uu) // 2])
                    if len(picks) >= 6:
                        break
                return self._json(picks)
            if path.startswith("/t/"):
                parts = path.split("/")            # ["", "t", lib, size, uuid]
                if len(parts) < 5:
                    return self._json({"error": "bad request"}, 400)
                lib = self._lib(parts[2])
                size = parts[3] if parts[3] in THUMB_SIZES else "s"
                uuid = parts[4].upper()
                if not lib:
                    return self._json({"error": "no library"}, 404)
                lib.assets()
                p = lib.make_thumb(uuid, size)
                if not p:
                    return self._json({"error": "no source"}, 404)
                return self._file(p)
            if path.startswith("/v/"):
                parts = path.split("/")
                lib = self._lib(parts[2]) if len(parts) > 3 else None
                uuid = parts[3].upper() if len(parts) > 3 else ""
                if not lib:
                    return self._json({"error": "no library"}, 404)
                p = lib.original_path(uuid)
                if not p:
                    return self._json({"error": "not found"}, 404)
                return self._stream_video(p)
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            sys.stderr.write("ERR %s: %s\n" % (path, exc))
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass


BIND_HOST = os.environ.get("ALBUM_HOST", "0.0.0.0")   # 0.0.0.0 = 局域网可访问

def lan_ip():
    """取本机在局域网里的地址，供手机 / 另一台电脑访问。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()

def main():
    port = 8905
    extra = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1]); i += 2
        elif args[i] == "--lib" and i + 1 < len(args):
            extra.append(args[i + 1]); i += 2
        else:
            i += 1
    os.makedirs(CACHE_DIR, exist_ok=True)
    EXTRA_LIBS.extend(extra)
    libs = ensure_libs()
    if not libs:
        print("图库列表是空的。用 --lib 指定路径，或在网页里用「库管理」添加。")
    for i, l in enumerate(libs):
        print("  [%d] %s  ←  %s" % (i, l.name, l.path))
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = None
    for p in range(port, port + 30):        # 端口被占用就顺延，不打扰已在跑的进程
        try:
            httpd = ThreadingHTTPServer((BIND_HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("8899–%d 端口都被占用，无法启动。" % (port + 29))
        sys.exit(1)
    print("时光相册启动：http://127.0.0.1:%d" % port)
    lan = lan_ip()
    if lan:
        print("同网段其他设备：http://%s:%d" % (lan, port))
    url = "http://127.0.0.1:%d" % port
    if os.environ.get("ALBUM_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
