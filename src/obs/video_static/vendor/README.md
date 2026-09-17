# vendor

## hls.min.js

- 版本：hls.js **1.5.13**
- 来源：https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js
- 用途：浏览器端播放 HLS（m3u8）流。Chrome/Firefox/Edge 需此库；Safari 走原生 HLS，可不用。
- 更新方式：
  ```
  python3 -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/npm/hls.js@<版本>/dist/hls.min.js', 'public/vendor/hls.min.js')"
  ```
  或直接下载后覆盖本文件。
- 若本文件缺失：前端自动回退为直连 mp4/webm 播放（`/obs/`），功能不受影响，只是不走 HLS。
