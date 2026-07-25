# -*- coding: utf-8 -*-
"""轨迹导航生成系统 — Streamlit 界面
启动: 双击 run.bat, 或 .venv\\Scripts\\python -m streamlit run app.py
"""
import datetime
import io
import os
import shutil
import tempfile

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import navi_core as nc

st.set_page_config(page_title="轨迹导航生成系统", page_icon="🧭", layout="wide")

# 可选访问口令: 在 Streamlit Cloud 的 Secrets 里配置 APP_PASSWORD = "xxx" 即启用
try:
    _REQUIRED_PW = st.secrets.get("APP_PASSWORD", "")
except Exception:
    _REQUIRED_PW = ""
if _REQUIRED_PW and not st.session_state.get("auth_ok"):
    st.title("🧭 轨迹导航生成系统")
    _pw = st.text_input("请输入访问口令", type="password")
    if _pw:
        if _pw == _REQUIRED_PW:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("口令错误")
    st.stop()

st.title("🧭 轨迹导航生成系统")
st.caption("地名 / 经纬度 → 导航路线 → 导出 SHP · GeoJSON · 交互路线图（输出坐标系 WGS-84）")

# ---------------- 侧边栏参数 ----------------
MODE_MAP = {"🚗 驾车": "driving", "🚶 步行": "walking", "🚴 骑行": "cycling"}
ENGINE_MAP = {
    "自动（OSRM在线优先，失败转本地路网）": "auto",
    "OSRM在线（免key，全球路网，走系统代理）": "osrm",
    "本地OSM路网（免key，下载路网后本地算路）": "local",
    "OpenRouteService（免费key，支持长途）": "ors",
    "高德API（免费key，国内数据最好）": "amap",
    "直线连接（不走路网）": "straight",
}
GEO_HELP = "地名解析固定使用 OSM Nominatim（免key）"

with st.sidebar:
    st.header("⚙️ 参数设置")
    mode = MODE_MAP[st.selectbox("出行方式", list(MODE_MAP))]
    engine = ENGINE_MAP[st.selectbox("路由引擎", list(ENGINE_MAP))]
    alts = st.select_slider("每段备选方案数（0=只要最优路线）", [0, 1, 2], 0,
                            help="像导航App的方案一/方案二。备选以虚线显示，"
                                 "SHP/GeoJSON 分段图层中 alt 字段 0=主路线 1/2=备选。"
                                 "高德/直线引擎不支持。")

    ors_key = amap_key = osrm_url = None
    if engine == "ors":
        ors_key = st.text_input("ORS key", os.environ.get("ORS_KEY", ""),
                                type="password",
                                help="openrouteservice.org 免费注册，每天2000次")
    if engine == "osrm":
        osrm_url = st.text_input("自定义 OSRM 服务器（留空 = 官方公共服务器）",
                                 os.environ.get("OSRM_URL", ""),
                                 help="留空使用 routing.openstreetmap.de；自建请填如 http://localhost:5000") or None

    st.divider()
    geocoder = "nominatim"
    if engine == "amap":
        amap_key = st.text_input("高德 key", os.environ.get("AMAP_KEY", ""),
                                 type="password", help="lbs.amap.com 免费注册（Web服务类型key）")
    city = st.text_input("默认城市（提高地名命中率）", "",
                         placeholder="如：昆明") or None
    china_only = st.checkbox("地名限定中国境内", True)

    st.divider()
    coord_sys = "gcj02" if st.radio(
        "输入坐标的坐标系", ["WGS-84（GPS / OSM）", "GCJ-02（高德/腾讯地图拾取）"],
        help="导出永远是 WGS-84；如果坐标是从高德/腾讯地图上拾取的请选 GCJ-02，系统会自动纠偏"
    ).startswith("GCJ") else "wgs84"
    prefix = st.text_input("导出文件名前缀", "navi").strip() or "navi"

# ---------------- 输入区 ----------------
input_mode = st.radio(
    "输入方式", ["✍️ 直接输入", "📄 批量导入（CSV / Excel）", "🗺️ 地图选点"],
    horizontal=True)
txt, up = "", None

if input_mode == "✍️ 直接输入":
    txt = st.text_area(
        "每行一个点（按行的先后作为途经顺序）", height=170,
        placeholder="支持三种格式，可混用：\n翠湖公园\n昆明站,102.720287,25.018662\n102.690951,25.039813",
    )
    st.caption("分隔符支持英文/中文逗号、空格、Tab。多条线路请用『批量导入』的分组列。")

elif input_mode == "📄 批量导入（CSV / Excel）":
    up = st.file_uploader("上传 CSV 或 Excel", type=["csv", "xlsx", "xls"])
    tpl = ("名称,经度,纬度,线路\n昆明站,102.720287,25.018662,线路1\n"
           "翠湖公园,,,线路1\n云南大学,,,线路1\n"
           "篆新农贸市场,102.690951,25.039813,线路2\n麻园村,102.675143,25.059015,线路2\n")
    st.download_button("⬇️ 下载导入模板 (CSV)", tpl.encode("utf-8-sig"),
                       "导入模板.csv", "text/csv")
    st.caption("列名自动识别：名称/地名、经度/lon/x、纬度/lat/y、线路/分组/group。"
               "经纬度留空的行将按名称自动地理编码；『线路』列不同值会生成多条路线。")
    if up is not None:
        try:
            pts_preview, colmap = nc.read_table(io.BytesIO(up.getvalue()), up.name)
            st.success(f"已识别 {len(pts_preview)} 个点，列映射：{colmap}")
            st.dataframe(pd.DataFrame(pts_preview), height=200, use_container_width=True)
        except Exception as e:
            st.error(f"文件解析失败: {e}")

else:
    import folium
    from streamlit_folium import st_folium

    if "picked" not in st.session_state:
        st.session_state.picked = []
    picked = st.session_state.picked

    c_map, c_form = st.columns([5, 3])
    with c_map:
        # 每次重跑都用相同参数新建底图 -> 组件内容不变不重载, 视野由浏览器端保持;
        # 动态标记经 feature_group_to_add 增量更新
        base_map = folium.Map(location=[30.0, 105.0], zoom_start=5,
                              control_scale=True)
        fg = folium.FeatureGroup(name="选点")
        for i, p in enumerate(picked):
            folium.Marker(
                [p["lat"], p["lon"]], tooltip=f"{i + 1}. {p['name']}",
                icon=folium.DivIcon(html=(
                    f'<div style="background:#4363d8;color:#fff;border-radius:50%;'
                    f'width:24px;height:24px;line-height:24px;text-align:center;'
                    f'font-size:12px;font-weight:bold;border:2px solid #fff;'
                    f'box-shadow:0 1px 3px rgba(0,0,0,.4)">{i + 1}</div>'),
                    icon_size=(24, 24), icon_anchor=(12, 12))).add_to(fg)
        lc_prev = st.session_state.get("picker_last")
        if lc_prev:
            folium.Marker([lc_prev["lat"], lc_prev["lng"]], tooltip="待添加",
                          icon=folium.Icon(color="orange", icon="plus")).add_to(fg)
        mout = st_folium(base_map, height=450,
                         use_container_width=True, key="picker",
                         feature_group_to_add=fg,
                         returned_objects=["last_clicked"])
        if mout:
            lc_new = mout.get("last_clicked")
            if (lc_new and lc_new != lc_prev
                    and lc_new != st.session_state.get("picker_consumed")):
                st.session_state.picker_last = lc_new
                st.rerun()

    with c_form:
        lc = st.session_state.get("picker_last")
        if lc:
            st.success(f"已选坐标：{lc['lng']:.6f}, {lc['lat']:.6f}")
        else:
            st.info("在左侧地图上点击选取位置（可缩放/拖动）")
        pname = st.text_input("点名称", f"点{len(picked) + 1}",
                              key=f"pick_name_{len(picked)}")
        if st.button("➕ 添加该点", disabled=lc is None, use_container_width=True):
            picked.append({"name": pname.strip() or f"点{len(picked) + 1}",
                           "lon": round(lc["lng"], 6), "lat": round(lc["lat"], 6),
                           "group": "线路1"})
            st.session_state.picker_consumed = lc
            st.session_state.picker_last = None
            st.rerun()
        if picked:
            st.write(f"已添加 **{len(picked)}** 个轨迹点（表格可直接改名/删行）：")
            edited = st.data_editor(
                pd.DataFrame(picked)[["name", "lon", "lat"]],
                num_rows="dynamic", height=200, use_container_width=True,
                key=f"picked_editor_{len(picked)}")
            recs = []
            for r in edited.to_dict("records"):
                lon, lat = nc._tofloat(r.get("lon")), nc._tofloat(r.get("lat"))
                if lon is None or lat is None:
                    continue
                recs.append({"name": str(r.get("name") or f"点{len(recs) + 1}").strip(),
                             "lon": lon, "lat": lat, "group": "线路1"})
            st.session_state.picked = recs
            if st.button("🗑️ 清空所有选点", use_container_width=True):
                st.session_state.picked = []
                st.session_state.picker_last = None
                st.session_state.picker_consumed = None
                st.rerun()

run = st.button("🚀 生成导航路线", type="primary", use_container_width=True)

# ---------------- 生成 ----------------
if run:
    try:
        if input_mode == "📄 批量导入（CSV / Excel）":
            if up is None:
                st.warning("请先上传 CSV/Excel 文件")
                st.stop()
            raw_pts, _ = nc.read_table(io.BytesIO(up.getvalue()), up.name)
            src_note = f"文件 {up.name}"
        elif input_mode == "🗺️ 地图选点":
            picked_now = st.session_state.get("picked", [])
            if not picked_now:
                st.warning("请先在地图上添加点位")
                st.stop()
            raw_pts = [dict(p) for p in picked_now]
            src_note = "地图选点"
        else:
            if not txt.strip():
                st.warning("请先输入点位")
                st.stop()
            raw_pts = nc.parse_text_input(txt)
            src_note = "手动输入"

        if len(raw_pts) < 2:
            st.warning("至少需要 2 个点才能规划路线")
            st.stop()

        need_geo = sum(1 for p in raw_pts if p.get("lon") is None)
        pbar = st.progress(0.0, text="准备中…")

        def _cb(i, n, name):
            pbar.progress(i / max(n, 1), text=f"处理点位 {i + 1}/{n}: {name}")

        points, failed = nc.normalize_points(
            raw_pts, coord_sys=coord_sys, geocoder=geocoder, amap_key=amap_key,
            city=city, china_only=china_only, progress=_cb)
        pbar.progress(1.0, text="点位处理完成")

        if not points or len(points) < 2:
            st.error(f"有效点不足 2 个。地理编码失败: {failed}")
            st.stop()

        status = st.status(f"正在规划路线（{nc.ENGINES.get(engine, engine)} / {mode}）…",
                           expanded=True)
        routes, errors = nc.plan_routes(
            points, engine=engine, mode=mode, ors_key=ors_key,
            amap_key=amap_key, osrm_url=osrm_url, log=status.write,
            alternatives=alts)
        status.update(label="路线规划完成", state="complete", expanded=False)

        if not routes:
            st.error("没有成功规划出任何路线：\n\n" + "\n\n".join(errors))
            st.stop()

        if os.name == "nt":  # 本地运行: 落盘留档
            out_dir = os.path.join("output", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        else:  # 云端: 临时目录, 用完即删, 不占服务器空间
            out_dir = tempfile.mkdtemp(prefix="navi_")
        shp_files = nc.export_shp(points, routes, out_dir, prefix)
        geo_files = nc.export_geojson(points, routes, out_dir, prefix)
        map_path = os.path.join(out_dir, f"{prefix}_map.html")
        nc.make_map(points, routes, map_path)
        png_path = os.path.join(out_dir, f"{prefix}_map.png")
        try:
            with st.spinner("正在生成制图版 PNG（含图例/比例尺/指北针）…"):
                nc.make_static_map(points, routes, png_path)
        except Exception as e:
            st.warning(f"PNG 出图失败（不影响其他导出）: {e}")
            png_path = None
        shp_zip = nc.zip_files(shp_files, os.path.join(out_dir, f"{prefix}_shp.zip"))
        geo_zip = nc.zip_files(geo_files, os.path.join(out_dir, f"{prefix}_geojson.zip"))

        with open(shp_zip, "rb") as fh:
            shp_zip_bytes = fh.read()
        with open(geo_zip, "rb") as fh:
            geo_zip_bytes = fh.read()
        with open(map_path, "r", encoding="utf-8") as fh:
            map_html = fh.read()
        png_bytes = None
        if png_path:
            with open(png_path, "rb") as fh:
                png_bytes = fh.read()
        if os.name != "nt":
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir = None

        st.session_state["result"] = {
            "points": points, "routes": routes, "failed": failed, "errors": errors,
            "out_dir": out_dir, "shp_zip_bytes": shp_zip_bytes,
            "geo_zip_bytes": geo_zip_bytes, "map_html": map_html,
            "png_bytes": png_bytes, "src_note": src_note, "prefix": prefix,
        }
    except Exception as e:
        st.error(f"生成失败: {e}")
        st.stop()

# ---------------- 结果展示 ----------------
res = st.session_state.get("result")
if res:
    points, routes = res["points"], res["routes"]
    st.divider()
    st.subheader("📊 结果")

    c1, c2, c3, c4 = st.columns(4)
    total_d = sum(r["distance"] for r in routes.values())
    total_t = sum(r["duration"] for r in routes.values())
    c1.metric("路线数", len(routes))
    c2.metric("途经点数", len(points))
    c3.metric("总里程", nc.fmt_km(total_d))
    c4.metric("总耗时(约)", nc.fmt_dur(total_t))

    if res["failed"]:
        st.warning("以下地名未能解析（可加上城市名重试，或改用高德地理编码，或手动补坐标）：\n\n- "
                   + "\n- ".join(res["failed"]))
    for e in res["errors"]:
        st.warning(e)

    with st.expander("📋 点位明细", expanded=False):
        st.dataframe(pd.DataFrame(points)[
            ["group", "seq", "name", "lon", "lat", "source", "display"]],
            use_container_width=True)

    components.html(res["map_html"], height=560)

    if res.get("png_bytes"):
        with st.expander("🖼️ 制图版轨迹地图预览（图例·比例尺·指北针，300dpi）", expanded=True):
            st.image(res["png_bytes"], use_container_width=True)

    if res.get("out_dir"):
        st.caption(f"所有文件已保存到本地目录：`{os.path.abspath(res['out_dir'])}`")
    else:
        st.caption("⬇️ 结果在内存中生成（不占服务器存储），点击按钮即可下载。")
    pfx = res.get("prefix", "navi")
    d1, d2, d3, d4 = st.columns(4)
    d1.download_button("⬇️ SHP（点+线，zip）", res["shp_zip_bytes"],
                       f"{pfx}_shp.zip", "application/zip",
                       use_container_width=True)
    d2.download_button("⬇️ GeoJSON（zip）", res["geo_zip_bytes"],
                       f"{pfx}_geojson.zip", "application/zip",
                       use_container_width=True)
    d3.download_button("⬇️ 交互地图（HTML）", res["map_html"].encode("utf-8"),
                       f"{pfx}_map.html", "text/html",
                       use_container_width=True)
    if res.get("png_bytes"):
        d4.download_button("⬇️ 轨迹地图（PNG）", res["png_bytes"],
                           f"{pfx}_map.png", "image/png",
                           use_container_width=True)
