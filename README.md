# 🧭 轨迹导航生成系统

输入**地名或经纬度**（支持批量导入），基于 OSM 等路网生成**导航路线**，导出 **SHP（点+线）**、**GeoJSON** 和**交互式路线图（HTML）**。输出坐标系统一为 **WGS-84 (EPSG:4326)**，QGIS / ArcGIS 可直接打开。

## 快速开始

双击 **`run.bat`**（首次运行会自动创建虚拟环境并安装依赖），浏览器自动打开 `http://localhost:8501`。

也可以手动启动：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m streamlit run app.py
```

## 两种用法

### ① 网页界面（推荐）

- **直接输入**：文本框每行一个点，三种格式可混用（按行序作为途经顺序）：
  ```
  翠湖公园                      ← 纯地名，自动地理编码
  昆明站,102.720287,25.018662   ← 名称 + 经度 + 纬度
  102.690951,25.039813          ← 纯坐标
  ```
- **地图选点**：在地图上点击取点、命名、指定线路，表格中可改名/删行，适合手工描绘轨迹。
- **批量导入**：上传 CSV / Excel，列名自动识别：
  | 含义 | 可识别的列名 | 必填 |
  |---|---|---|
  | 名称 | 名称 / 地名 / name / 站点 | 与经纬度二选一 |
  | 经度 | 经度 / lon / lng / x | 留空则按名称编码 |
  | 纬度 | 纬度 / lat / y | 同上 |
  | 线路 | 线路 / 分组 / group / route | 选填；不同值生成多条路线 |

  界面里可下载导入模板；示例文件见 `sample_points.csv`。

### ② 命令行（适合批处理）

```bash
.venv\Scripts\python cli.py sample_points.csv --city 昆明
.venv\Scripts\python cli.py 地名.txt -m walking -e local -o output -p demo
```

参数：`-e` 引擎(local/ors/amap/osrm/straight)、`-m` 方式(driving/walking/cycling)、`-g` 地理编码(nominatim/amap)、`--city` 默认城市、`--coord-sys gcj02` 输入为火星坐标、`-o` 输出目录、`-p` 文件名前缀。key 也可用环境变量 `AMAP_KEY` / `ORS_KEY`。

## 路由引擎对比

| 引擎 | key | 说明 |
|---|---|---|
| **自动**（默认） | 不需要 | OSRM 在线优先，失败自动切换到本地OSM路网 |
| OSRM 在线 | 不需要 | 公共服务器 routing.openstreetmap.de，全球 OSM 路网，支持驾/步/骑、长途，秒级响应。国内网络需挂代理（系统代理/TUN 均可被自动识别） |
| 本地OSM路网 | 不需要 | 经 Overpass 下载 OSM 路网后本地算路。首次下载约 1~3 分钟（有缓存），适合**约 100 km 内**的市内/短途路线 |
| OpenRouteService | 免费 key（[openrouteservice.org](https://openrouteservice.org)） | 全球 OSM 路网，每天 2000 次 |
| 高德 API | 免费 key（[lbs.amap.com](https://lbs.amap.com)，Web服务类型） | 国内路网数据最好；系统自动做 GCJ-02→WGS-84 纠偏 |
| 直线连接 | 不需要 | 不走路网，点间直连（耗时按方式估算） |

地名解析（地理编码）固定使用 **OSM Nominatim**（免 key，限速 1 次/秒）。建议始终填写"默认城市"以避免重名地名错配；搜不到的小地名请直接给坐标或用地图选点。

**备选路线**：侧边栏"每段备选方案数"设为 1 或 2，可像导航 App 一样给出方案一/方案二（地图上以虚线显示）。OSRM/ORS 用原生备选接口（走法差异不大时可能返回不足额）；本地引擎用罚权重法强制绕行；高德/直线引擎不支持。

## 输出文件（`output/时间戳/`）

| 文件 | 内容 |
|---|---|
| `*_points.shp` | 途经点（字段：seq 序号、name、group 线路、lon、lat、source 坐标来源） |
| `*_routes.shp` | 全程线，每条线路 1 个要素（mode、engine、dist_km、dur_min、start、end） |
| `*_legs.shp` | 分段线，相邻两点间 1 个要素（leg 段号、**alt 方案号：0=主路线 1/2=备选**、from_pt、to_pt、dist_km、dur_min） |
| `*_points/_routes/_legs.geojson` | 同内容的 GeoJSON 版 |
| `*_map.html` | 交互路线图（可缩放、显示各段里程耗时、多线路分色、图层开关） |
| `*_map.png` | 制图版轨迹地图（OSM 底图 + 图例 + 比例尺 + 指北针，300dpi，可直接用于报告） |

SHP 附带 `.prj`（WGS-84）与 `.cpg`（UTF-8），中文属性在 QGIS 中正常显示。

## 部署到 Streamlit Community Cloud（免费在线版）

1. 把本仓库推送到 GitHub（`packages.txt` 提供云端中文字体，`requirements.txt` 提供依赖，均已备好）。
2. 打开 [share.streamlit.io](https://share.streamlit.io) → 用 GitHub 账号登录 → **Create app** → 选择本仓库、分支 `main`、主文件 `app.py` → Deploy。
3. （可选）App 设置 → **Secrets** 中加一行 `APP_PASSWORD = "你的口令"`，即可开启访问口令；不加则公开访问。

云端注意：服务器在海外，OSRM/Nominatim 直连畅通（无需代理）；容器内存约 1GB，"本地OSM路网"引擎下载大范围路网可能超限，云端建议用默认的"自动/OSRM在线"引擎；长时间无人访问会休眠，再次打开需约 1 分钟唤醒。

## 常见问题

- **坐标系**：OSM/GPS 为 WGS-84；高德/腾讯地图上拾取的坐标是 GCJ-02（火星坐标），两者相差约 100~600 m。若输入坐标来自高德/腾讯，请在侧边栏选择"GCJ-02"，系统会自动纠偏；本系统**输出一律 WGS-84**。
- **地名搜不到 / 搜偏**：加上城市名（如"翠湖公园 昆明"）、填写"默认城市"、改用高德地理编码，或直接手动给坐标。
- **本地引擎首次很慢**：在下载 OSM 路网（Overpass），完成后缓存在 `cache/` 目录，同区域再次使用秒级完成。
- **OSRM 在线连不上**：Nominatim / Overpass / OSRM 服务器在国内直连不稳定，开启代理（系统代理或 TUN 模式）即可；"自动"引擎会在 OSRM 失败时自行切换到本地路网。
- **长途路线**：本地引擎限制约 100 km，跨市/跨省请用 OSRM 在线、OpenRouteService 或高德。
- **经纬度写反**：系统会自动检测并纠正（中国范围内）。
