# -*- coding: utf-8 -*-
"""
轨迹导航生成系统 — 命令行批处理入口

用法示例:
  python cli.py sample_points.csv
  python cli.py 地名列表.txt -m walking --city 昆明
  python cli.py points.xlsx -e amap --amap-key XXXX -o output -p demo

输入文件:
  .csv/.xlsx  列名自动识别(名称/经度/纬度/线路), 经纬度缺省时按名称地理编码
  .txt        每行: `地名` 或 `名称,lon,lat` 或 `lon,lat`
"""
import argparse
import datetime
import os
import sys

import navi_core as nc


def main():
    ap = argparse.ArgumentParser(description="轨迹导航生成: 地名/坐标 -> 路线 -> SHP/GeoJSON/地图")
    ap.add_argument("input", help="输入文件 (.csv/.xlsx/.txt)")
    ap.add_argument("-e", "--engine", default="auto",
                    choices=["auto", "osrm", "local", "ors", "amap", "straight"],
                    help="路由引擎, 默认 auto(OSRM在线优先, 失败转本地OSM路网)")
    ap.add_argument("-m", "--mode", default="driving",
                    choices=["driving", "walking", "cycling"], help="出行方式, 默认驾车")
    ap.add_argument("-g", "--geocoder", default="nominatim", choices=["nominatim", "amap"],
                    help="地理编码, 默认 OSM Nominatim")
    ap.add_argument("--city", default=None, help="默认城市, 提高地名命中率")
    ap.add_argument("--coord-sys", default="wgs84", choices=["wgs84", "gcj02"],
                    help="输入坐标的坐标系, 默认 wgs84")
    ap.add_argument("--amap-key", default=os.environ.get("AMAP_KEY"), help="高德 key")
    ap.add_argument("--ors-key", default=os.environ.get("ORS_KEY"), help="OpenRouteService key")
    ap.add_argument("--osrm-url", default=os.environ.get("OSRM_URL"), help="OSRM 服务器地址")
    ap.add_argument("--alts", type=int, default=0, choices=[0, 1, 2],
                    help="每段备选方案数, 默认 0 (仅最优路线)")
    ap.add_argument("--no-china", action="store_true", help="地名不限定中国境内")
    ap.add_argument("-o", "--out", default=None, help="输出目录, 默认 output/时间戳")
    ap.add_argument("-p", "--prefix", default="navi", help="输出文件名前缀")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"输入文件不存在: {args.input}")

    print(f"[1/4] 读取输入: {args.input}")
    raw = nc.read_input_file(args.input)
    print(f"      共 {len(raw)} 个点")

    def cb(i, n, name):
        print(f"      点位 {i + 1}/{n}: {name}")

    print(f"[2/4] 解析坐标 (地理编码: {args.geocoder})")
    points, failed = nc.normalize_points(
        raw, coord_sys=args.coord_sys, geocoder=args.geocoder,
        amap_key=args.amap_key, city=args.city,
        china_only=not args.no_china, progress=cb)
    if failed:
        print(f"      ⚠ 未解析出坐标: {failed}")
    if len(points) < 2:
        sys.exit("有效点不足 2 个, 无法规划路线")

    print(f"[3/4] 规划路线 (引擎: {args.engine}, 方式: {args.mode})")
    routes, errors = nc.plan_routes(
        points, engine=args.engine, mode=args.mode, ors_key=args.ors_key,
        amap_key=args.amap_key, osrm_url=args.osrm_url,
        log=lambda s: print("      " + s), alternatives=args.alts)
    for e in errors:
        print(f"      ⚠ {e}")
    if not routes:
        sys.exit("没有成功规划出任何路线")
    for g, rt in routes.items():
        n_alt = len(rt.get("alts", []))
        print(f"      ✔ 线路[{g}]: {nc.fmt_km(rt['distance'])} / 约{nc.fmt_dur(rt['duration'])}"
              f" / {len(rt['coords'])} 个折点"
              + (f" / {n_alt} 条备选" if n_alt else ""))

    out_dir = args.out or os.path.join("output", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    print(f"[4/4] 导出到 {os.path.abspath(out_dir)}")
    shp = nc.export_shp(points, routes, out_dir, args.prefix)
    geo = nc.export_geojson(points, routes, out_dir, args.prefix)
    map_path = os.path.join(out_dir, f"{args.prefix}_map.html")
    nc.make_map(points, routes, map_path)
    png_path = os.path.join(out_dir, f"{args.prefix}_map.png")
    try:
        nc.make_static_map(points, routes, png_path)
    except Exception as e:
        print(f"      ⚠ PNG 出图失败(不影响其他导出): {e}")
        png_path = None
    for f in shp + geo + [map_path] + ([png_path] if png_path else []):
        print(f"      {f}")
    print("完成 ✅  (SHP/GeoJSON 均为 WGS-84; 属性表 UTF-8, QGIS 直接打开)")


if __name__ == "__main__":
    main()
