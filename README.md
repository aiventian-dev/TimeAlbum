# 时光相册 · Time Album

一个跑在自己电脑上的**本地只读照片相册**。读取 macOS「照片」图库（`.photoslibrary`）或任意照片文件夹，按 **年 → 月 → 连续照片流** 三层浏览；照片越旧越泛黄、褪色、起颗粒，越新越接近原貌——时间本身就是这层滤镜。

照片不会被复制、移动或修改，程序只读取，缓存的缩略图放在本目录的 `cache/` 里。

## 特点

- **只读**：不写入、不删除、不改变原图，也不依赖云端账号
- **按时间做旧**：按离今天的时间自动调节褪色、颗粒，叠加胶片色彩
- **胶片模拟**：NCPortraGoldSuperiaVelviaHP5800T 七个片种，可调片种强度，可单独加胶片颗粒
- **三种缩略图尺寸**：`s/m/l`，按网格密度自动挑选
- **多图库**：同时挂载「照片库」和普通文件夹，随时重扫、增删
- **无级缩放**：滚轮、触控板双指、手机双指捏合；双击填满画面，可旋转摆正
- **详情状态可恢复**：刷新后仍停在那张照片

## 运行

需要 macOS 与 Python 3（系统自带即可，无需第三方依赖）。

```bash
git clone <本仓库地址>
cd 时光相册
cp libraries.example.json libraries.json   # 然后改成你自己的图库路径
python3 album_server.py                     # 默认 8905 端口
```

双击 `启动相册.command` 也可以，浏览器会自动打开 <http://127.0.0.1:8905>。

```bash
python3 album_server.py --port 9000
python3 album_server.py --lib "/Users/you/Pictures/照片图库.photoslibrary"
```

端口被占用时会自动顺延到 8906、8907……

## 密码（可选）

默认**不启用密码**。要加访问口令，在本目录建一个 `密码.txt`，第一行写口令即可，不需要改代码；或者用环境变量：

```bash
ALBUM_PASSWORD=my-pass python3 album_server.py
```

`密码.txt` 已在 `.gitignore` 里，不会被提交。截图、录屏前建议关掉口令并确认 `.gitignore` 生效。

## 目录

```
album_server.py        # 单文件服务端（标准库实现，含缩略图生成与缓存）
时光相册.html          # 单页前端（内联 CSS/JS，改完刷新即生效）
启动相册.command       # macOS 双击启动
libraries.example.json # 图库配置示例（复制为 libraries.json 使用）
品牌标识/              # 相机与手机品牌 SVG，用于详情页 EXIF 标识
cache/                 # 缩略图缓存（自动生成，不入库）
```

## 说明

- 视频不进入索引，只处理照片。
- 所有页面都是本机访问，请不要把端口直接暴露到公网；需要远程访问请自行加反向代理与鉴权。
- `品牌标识/` 中的 Logo 归各自品牌所有，仅用于照片 EXIF 的品牌识别展示，不代表任何隶属或授权关系。

## 许可

MIT，见 [LICENSE](LICENSE)。
