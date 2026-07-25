# -*- coding: utf-8 -*-
"""
轨迹导航核心模块
- 地理编码: OSM Nominatim(免key) / 高德(免费key, 自动 GCJ02->WGS84)
- 路径规划: 本地OSM路网(osmnx, 免key) / OpenRouteService(免费key) / 高德API(免费key)
            / OSRM(自建或可达的公网) / 直线连接
- 导出:    SHP(点 / 全程线 / 分段线) + GeoJSON + folium 交互地图
所有输出坐标一律为 WGS-84 (EPSG:4326)。
"""
import json
import math
import os
import re
import time
import zipfile

import requests

WGS84_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
             'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
             'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')

UA = {"User-Agent": "trajectory-navi/1.0 (local research tool)"}

# 公网 OSRM(国内网络通常不可达, 保留给自建/代理用户)
DEFAULT_OSRM = {
    "driving": "https://routing.openstreetmap.de/routed-car",
    "walking": "https://routing.openstreetmap.de/routed-foot",
    "cycling": "https://routing.openstreetmap.de/routed-bike",
}

# 直线/步行/骑行的估算速度 km/h
EST_SPEED = {"driving": 40.0, "walking": 4.8, "cycling": 15.0}

MODES = ("driving", "walking", "cycling")


# ============================================================
# 坐标转换 WGS84 <-> GCJ02 (火星坐标, 高德/腾讯使用)
# ============================================================
def _out_of_china(lon, lat):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _tf_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _tf_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _delta(lon, lat):
    a, ee = 6378245.0, 0.00669342162296594323
    dlat = _tf_lat(lon - 105.0, lat - 35.0)
    dlon = _tf_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return dlon, dlat


def wgs84_to_gcj02(lon, lat):
    if _out_of_china(lon, lat):
        return lon, lat
    dlon, dlat = _delta(lon, lat)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon, lat):
    """迭代求逆, 精度约 1e-6 度(<0.2m)"""
    if _out_of_china(lon, lat):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(4):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon += lon - glon
        wlat += lat - glat
    return wlon, wlat


def haversine(a, b):
    """两点球面距离(米), a/b = (lon, lat)"""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


# ============================================================
# 地理编码
# ============================================================
_geo_cache = {}


def geocode_nominatim(name, city=None, china_only=True):
    key = ("osm", name, city, china_only)
    if key in _geo_cache:
        return _geo_cache[key]
    q = f"{city} {name}" if city and city not in name else name
    params = {"q": q, "format": "json", "limit": 1, "accept-language": "zh"}
    if china_only:
        params["countrycodes"] = "cn"
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params=params, headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    time.sleep(1.05)  # Nominatim 官方限速 1 次/秒
    res = None
    if data:
        res = {"lon": float(data[0]["lon"]), "lat": float(data[0]["lat"]),
               "display": data[0].get("display_name", ""), "source": "OSM"}
    _geo_cache[key] = res
    return res


def geocode_amap(name, key, city=None):
    ck = ("amap", name, city)
    if ck in _geo_cache:
        return _geo_cache[ck]
    res = None
    # 先用 POI 搜索(对地名/兴趣点效果好), 再退回结构化地址编码
    try:
        r = requests.get("https://restapi.amap.com/v3/place/text",
                         params={"keywords": name, "city": city or "", "key": key,
                                 "offset": 1, "page": 1}, timeout=30).json()
        if r.get("status") == "1" and r.get("pois"):
            lon, lat = map(float, r["pois"][0]["location"].split(","))
            lon, lat = gcj02_to_wgs84(lon, lat)
            res = {"lon": lon, "lat": lat,
                   "display": r["pois"][0].get("name", "") + " " + str(r["pois"][0].get("address", "")),
                   "source": "高德"}
    except Exception:
        pass
    if res is None:
        r = requests.get("https://restapi.amap.com/v3/geocode/geo",
                         params={"address": name, "city": city or "", "key": key}, timeout=30).json()
        if r.get("status") == "1" and r.get("geocodes"):
            lon, lat = map(float, r["geocodes"][0]["location"].split(","))
            lon, lat = gcj02_to_wgs84(lon, lat)
            res = {"lon": lon, "lat": lat,
                   "display": r["geocodes"][0].get("formatted_address", ""), "source": "高德"}
        elif r.get("status") != "1" and r.get("infocode", "").startswith("1000"):
            raise RuntimeError(f"高德接口错误: {r.get('info')} (检查key是否有效)")
    time.sleep(0.35)
    _geo_cache[ck] = res
    return res


def geocode(name, geocoder="nominatim", amap_key=None, city=None, china_only=True):
    if geocoder == "amap":
        if not amap_key:
            raise RuntimeError("使用高德地理编码需要提供 key")
        return geocode_amap(name, amap_key, city)
    return geocode_nominatim(name, city, china_only)


# ============================================================
# 路由引擎 —— 统一返回:
# {"coords":[[lon,lat],...], "legs":[{"coords":[...],"distance":m,"duration":s},...],
#  "distance": m, "duration": s}
# ============================================================
def _merge_legs(legs):
    full = []
    for lg in legs:
        for c in lg["coords"]:
            if not full or full[-1] != c:
                full.append(c)
    return {"coords": full, "legs": legs, "alts": [],
            "distance": sum(l["distance"] for l in legs),
            "duration": sum(l["duration"] for l in legs)}


def route_straight(coords, mode="driving"):
    spd = EST_SPEED.get(mode, 40.0) / 3.6
    legs = []
    for a, b in zip(coords[:-1], coords[1:]):
        d = haversine(a, b)
        legs.append({"coords": [list(a), list(b)], "distance": d, "duration": d / spd})
    return _merge_legs(legs)


def route_osrm(coords, mode="driving", base_url=None, timeout=90, alternatives=0):
    base = (base_url or DEFAULT_OSRM[mode]).rstrip("/")
    if alternatives <= 0:
        locs = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
        r = requests.get(f"{base}/route/v1/{mode}/{locs}",
                         params={"overview": "full", "geometries": "geojson", "steps": "true"},
                         headers=UA, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"OSRM 返回错误: {data.get('code')} {data.get('message', '')}")
        rt = data["routes"][0]
        legs = []
        for leg in rt["legs"]:
            pts = []
            for step in leg.get("steps", []):
                for c in step["geometry"]["coordinates"]:
                    if not pts or pts[-1] != c:
                        pts.append(c)
            if not pts:  # 无 steps 时退化为全程几何
                pts = rt["geometry"]["coordinates"]
            legs.append({"coords": pts, "distance": float(leg["distance"]),
                         "duration": float(leg["duration"])})
        out = _merge_legs(legs)
        out["alts"] = []
        return out
    # 备选模式: 逐段请求 (OSRM 的 alternatives 仅支持两点间)
    legs, alts = [], []
    for i, (a, b) in enumerate(zip(coords[:-1], coords[1:])):
        locs = f"{a[0]:.6f},{a[1]:.6f};{b[0]:.6f},{b[1]:.6f}"
        r = requests.get(f"{base}/route/v1/{mode}/{locs}",
                         params={"overview": "full", "geometries": "geojson",
                                 "alternatives": str(min(alternatives, 3))},
                         headers=UA, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"OSRM 返回错误: {data.get('code')} {data.get('message', '')}")
        rts = data["routes"]
        legs.append({"coords": [list(c) for c in rts[0]["geometry"]["coordinates"]],
                     "distance": float(rts[0]["distance"]),
                     "duration": float(rts[0]["duration"])})
        for ai, art in enumerate(rts[1:alternatives + 1]):
            alts.append({"leg": i + 1, "alt": ai + 1,
                         "coords": [list(c) for c in art["geometry"]["coordinates"]],
                         "distance": float(art["distance"]),
                         "duration": float(art["duration"])})
    out = _merge_legs(legs)
    out["alts"] = alts
    return out


ORS_PROFILE = {"driving": "driving-car", "walking": "foot-walking", "cycling": "cycling-regular"}


def route_ors(coords, mode="driving", key=None, alternatives=0):
    if not key:
        raise RuntimeError("使用 OpenRouteService 需要免费 key (openrouteservice.org 注册)")
    url = f"https://api.openrouteservice.org/v2/directions/{ORS_PROFILE[mode]}/geojson"
    if alternatives > 0:
        # ORS 备选路线仅支持两点间: 逐段请求
        legs, alts = [], []
        for i, (a, b) in enumerate(zip(coords[:-1], coords[1:])):
            body = {"coordinates": [[round(a[0], 6), round(a[1], 6)],
                                    [round(b[0], 6), round(b[1], 6)]],
                    "alternative_routes": {"target_count": min(alternatives, 2) + 1,
                                           "share_factor": 0.6, "weight_factor": 1.6}}
            r = requests.post(url, json=body, headers={"Authorization": key, **UA}, timeout=90)
            data = r.json()
            if r.status_code != 200 or not data.get("features"):
                raise RuntimeError(f"ORS 返回错误: {data.get('error', data)}")
            feats = data["features"]
            s = feats[0]["properties"]["summary"]
            legs.append({"coords": [list(c) for c in feats[0]["geometry"]["coordinates"]],
                         "distance": float(s["distance"]), "duration": float(s["duration"])})
            for ai, f in enumerate(feats[1:alternatives + 1]):
                s = f["properties"]["summary"]
                alts.append({"leg": i + 1, "alt": ai + 1,
                             "coords": [list(c) for c in f["geometry"]["coordinates"]],
                             "distance": float(s["distance"]), "duration": float(s["duration"])})
        out = _merge_legs(legs)
        out["alts"] = alts
        return out
    r = requests.post(url, json={"coordinates": [[round(c[0], 6), round(c[1], 6)] for c in coords]},
                      headers={"Authorization": key, **UA}, timeout=90)
    data = r.json()
    if r.status_code != 200 or not data.get("features"):
        err = data.get("error", data)
        raise RuntimeError(f"ORS 返回错误: {err}")
    feat = data["features"][0]
    full = feat["geometry"]["coordinates"]
    props = feat["properties"]
    wp = props.get("way_points", [0, len(full) - 1])
    segs = props.get("segments", [])
    legs = []
    for i, seg in enumerate(segs):
        part = full[wp[i]: wp[i + 1] + 1]
        legs.append({"coords": [list(c) for c in part],
                     "distance": float(seg["distance"]), "duration": float(seg["duration"])})
    if not legs:
        legs = [{"coords": [list(c) for c in full],
                 "distance": float(props["summary"]["distance"]),
                 "duration": float(props["summary"]["duration"])}]
    return _merge_legs(legs)


def _amap_leg(a, b, mode, key):
    """高德逐段路由(输入输出均为 WGS84)"""
    ga = "%.6f,%.6f" % wgs84_to_gcj02(*a)
    gb = "%.6f,%.6f" % wgs84_to_gcj02(*b)
    if mode == "cycling":
        r = requests.get("https://restapi.amap.com/v4/direction/bicycling",
                         params={"origin": ga, "destination": gb, "key": key}, timeout=60).json()
        if r.get("errcode") != 0:
            raise RuntimeError(f"高德骑行接口错误: {r.get('errmsg')}")
        path = r["data"]["paths"][0]
    else:
        api = "driving" if mode == "driving" else "walking"
        r = requests.get(f"https://restapi.amap.com/v3/direction/{api}",
                         params={"origin": ga, "destination": gb, "key": key,
                                 "extensions": "base"}, timeout=60).json()
        if r.get("status") != "1":
            raise RuntimeError(f"高德{api}接口错误: {r.get('info')} (infocode={r.get('infocode')})")
        path = r["route"]["paths"][0]
    pts = [list(a)]
    for step in path["steps"]:
        for pair in step["polyline"].split(";"):
            glon, glat = map(float, pair.split(","))
            wlon, wlat = gcj02_to_wgs84(glon, glat)
            c = [round(wlon, 6), round(wlat, 6)]
            if pts[-1] != c:
                pts.append(c)
    pts.append(list(b))
    time.sleep(0.35)
    return {"coords": pts, "distance": float(path["distance"]),
            "duration": float(path.get("duration") or path.get("cost", {}).get("duration", 0))}


def route_amap(coords, mode="driving", key=None):
    if not key:
        raise RuntimeError("使用高德路由需要免费 key (lbs.amap.com 注册)")
    legs = [_amap_leg(a, b, mode, key) for a, b in zip(coords[:-1], coords[1:])]
    return _merge_legs(legs)


# ---------- 本地 OSM 路网引擎 (osmnx + Overpass, 免key) ----------
_graph_cache = {}
NETWORK_TYPE = {"driving": "drive", "walking": "walk", "cycling": "bike"}


def _load_graph(coords, mode, log=None):
    import numpy as np
    import osmnx as ox

    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    c_lon, c_lat = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    diag = haversine((min(lons), min(lats)), (max(lons), max(lats)))
    if diag > 120000:
        raise RuntimeError("本地OSM路网引擎适合约 100km 以内的路线; "
                           "更长距离请改用 OpenRouteService / 高德 / OSRM 引擎")
    dist = max(3000, int(diag / 2 + 2000))
    net = NETWORK_TYPE[mode]
    key = (round(c_lon, 3), round(c_lat, 3), dist // 1000, net)
    if key in _graph_cache:
        return _graph_cache[key]

    ox.settings.use_cache = True
    ox.settings.log_console = False
    ox.settings.requests_timeout = 300
    if log:
        log(f"正在从 Overpass 下载 OSM 路网 (半径约 {dist / 1000:.1f} km, 首次约 1~3 分钟)…")
    G = ox.graph_from_point((c_lat, c_lon), dist=dist, network_type=net, simplify=True)
    if log:
        log(f"路网下载完成: {len(G.nodes)} 节点 / {len(G.edges)} 边, 正在计算通行时间…")

    if mode == "driving":
        done = False
        for m in ("routing", "speed"):
            try:
                mod = getattr(ox, m)
                G = mod.add_edge_speeds(G)
                G = mod.add_edge_travel_times(G)
                done = True
                break
            except AttributeError:
                continue
        if not done:
            for _, _, d in G.edges(data=True):
                d["travel_time"] = d.get("length", 1.0) / (40 / 3.6)
    else:
        spd = EST_SPEED[mode] / 3.6
        for _, _, d in G.edges(data=True):
            d["travel_time"] = d.get("length", 1.0) / spd

    node_ids = list(G.nodes)
    xs = np.array([G.nodes[n]["x"] for n in node_ids])
    ys = np.array([G.nodes[n]["y"] for n in node_ids])
    _graph_cache[key] = (G, node_ids, xs, ys, math.cos(math.radians(c_lat)))
    return _graph_cache[key]


def route_local(coords, mode="driving", log=None, alternatives=0):
    try:
        import networkx as nx  # noqa
        import osmnx  # noqa
    except ImportError:
        raise RuntimeError("本地路网引擎需要 osmnx: pip install osmnx")
    import networkx as nx

    G, node_ids, xs, ys, cosl = _load_graph(coords, mode, log)

    def nearest(pt):
        d2 = ((xs - pt[0]) * cosl) ** 2 + (ys - pt[1]) ** 2
        i = int(d2.argmin())
        return node_ids[i], math.sqrt(float(d2[i])) * 111320.0

    def extract(path, a, b, penal):
        """沿路径取几何; 返回 (折点, 距离m, 耗时s, 边id集合)"""
        pts, dist, dur, eids = [list(a)], 0.0, 0.0, set()
        for u, v in zip(path[:-1], path[1:]):
            d = min(G.get_edge_data(u, v).values(),
                    key=lambda e: e.get("travel_time", 1e9) * penal.get(id(e), 1.0))
            if "geometry" in d:
                seg = [[round(x, 6), round(y, 6)] for x, y in d["geometry"].coords]
            else:
                seg = [[round(G.nodes[u]["x"], 6), round(G.nodes[u]["y"], 6)],
                       [round(G.nodes[v]["x"], 6), round(G.nodes[v]["y"], 6)]]
            for c in seg:
                if pts[-1] != c:
                    pts.append(c)
            dist += float(d.get("length", 0.0))
            dur += float(d.get("travel_time", 0.0))
            eids.add(id(d))
        pts.append(list(b))
        return pts, dist, dur, eids

    legs, alts = [], []
    for li, (a, b) in enumerate(zip(coords[:-1], coords[1:])):
        na, da = nearest(a)
        nb, db = nearest(b)
        off = max(da, db)
        if off > 1500:
            raise RuntimeError(f"输入点 ({a if da >= db else b}) 距离最近道路约 {off:.0f} m, "
                               "疑似在路网范围外, 请检查坐标")
        penal = {}

        def wfn(u, v, d):
            # MultiDiGraph 的 weight 回调收到的是平行边容器 {key: attrs}
            return min(a.get("travel_time", 1.0) * penal.get(id(a), 1.0)
                       for a in d.values())

        found = []
        want = alternatives + 1
        tries = 0
        while len(found) < want and tries < want * 3:
            tries += 1
            try:
                path = nx.shortest_path(G, na, nb, weight=wfn)
            except nx.NetworkXNoPath:
                if not found:
                    raise RuntimeError("两点间在当前路网内不连通 "
                                       "(可尝试更换出行方式, 或改用其他路由引擎)")
                break
            pts, dist, dur, eids = extract(path, a, b, penal)
            for eid in eids:  # 惩罚已用过的路段, 迫使下一次绕行
                penal[eid] = penal.get(eid, 1.0) * 2.2
            dup = any(len(eids & f[3]) / max(min(len(eids), len(f[3])), 1) > 0.85
                      for f in found)
            if dup:
                continue
            found.append((pts, dist, dur, eids))
        legs.append({"coords": found[0][0], "distance": found[0][1],
                     "duration": found[0][2]})
        for ai, f in enumerate(found[1:]):
            alts.append({"leg": li + 1, "alt": ai + 1, "coords": f[0],
                         "distance": f[1], "duration": f[2]})
    out = _merge_legs(legs)
    out["alts"] = alts
    return out


ENGINES = {
    "auto": "自动(OSRM在线优先)",
    "osrm": "OSRM在线",
    "local": "本地OSM路网(免key)",
    "ors": "OpenRouteService",
    "amap": "高德API",
    "straight": "直线连接",
}


def build_route(coords, engine="auto", mode="driving",
                ors_key=None, amap_key=None, osrm_url=None, log=None, alternatives=0):
    coords = [(float(c[0]), float(c[1])) for c in coords]
    if len(coords) < 2:
        raise RuntimeError("至少需要 2 个点才能规划路线")
    if engine == "auto":
        try:
            return route_osrm(coords, mode, osrm_url, timeout=30, alternatives=alternatives)
        except Exception as e:
            if log:
                log(f"OSRM 在线服务不可用({type(e).__name__}), 自动切换到本地OSM路网…")
            return route_local(coords, mode, log, alternatives=alternatives)
    if engine == "local":
        return route_local(coords, mode, log, alternatives=alternatives)
    if engine == "ors":
        return route_ors(coords, mode, ors_key, alternatives=alternatives)
    if engine == "amap":
        if alternatives > 0 and log:
            log("高德引擎暂不支持备选路线, 仅生成主路线")
        return route_amap(coords, mode, amap_key)
    if engine == "osrm":
        return route_osrm(coords, mode, osrm_url, alternatives=alternatives)
    if engine == "straight":
        return route_straight(coords, mode)
    raise RuntimeError(f"未知路由引擎: {engine}")


# ============================================================
# 输入解析与规整
# ============================================================
def _tofloat(s):
    try:
        f = float(s)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def parse_text_input(text):
    """每行三种格式: `地名` / `名称,lon,lat` / `lon,lat` (逗号/中文逗号/Tab/空格分隔)"""
    pts = []
    for i, line in enumerate(l.strip() for l in text.splitlines() if l.strip()):
        parts = [p for p in re.split(r"[,，\t ]+", line) if p]
        f = [_tofloat(p) for p in parts]
        if len(parts) >= 3 and f[-2] is not None and f[-1] is not None:
            pts.append({"name": " ".join(parts[:-2]), "lon": f[-2], "lat": f[-1]})
        elif len(parts) == 2 and f[0] is not None and f[1] is not None:
            pts.append({"name": f"P{i + 1}", "lon": f[0], "lat": f[1]})
        else:
            pts.append({"name": line, "lon": None, "lat": None})
    for p in pts:
        p.setdefault("group", "线路1")
    return pts


COL_ALIAS = {
    "name": ["name", "名称", "地名", "点名", "站点", "站名", "title"],
    "lon": ["lon", "lng", "longitude", "x", "经度"],
    "lat": ["lat", "latitude", "y", "纬度"],
    "group": ["group", "route", "line", "分组", "线路", "路线", "组"],
}


def read_table(file_or_path, filename=None):
    """CSV/XLSX 批量导入, 自动识别列名; 返回 (points, colmap)"""
    import pandas as pd
    fname = (filename or str(file_or_path)).lower()
    if fname.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_or_path)
    else:
        try:
            df = pd.read_csv(file_or_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            if hasattr(file_or_path, "seek"):
                file_or_path.seek(0)
            df = pd.read_csv(file_or_path, encoding="gbk")
    cols = {str(c).strip().lower(): c for c in df.columns}
    colmap = {}
    for std, aliases in COL_ALIAS.items():
        for a in aliases:
            if a in cols:
                colmap[std] = cols[a]
                break
    if "name" not in colmap and "lon" not in colmap:
        raise RuntimeError(f"无法识别列名, 现有列: {list(df.columns)}; "
                           f"需要 名称/地名 或 经度+纬度 列")
    pts = []
    for i, row in df.iterrows():
        name = str(row[colmap["name"]]).strip() if "name" in colmap and pd.notna(row.get(colmap["name"])) else f"P{i + 1}"
        lon = _tofloat(row[colmap["lon"]]) if "lon" in colmap else None
        lat = _tofloat(row[colmap["lat"]]) if "lat" in colmap else None
        grp = str(row[colmap["group"]]).strip() if "group" in colmap and pd.notna(row.get(colmap["group"])) else "线路1"
        if name in ("", "nan") and lon is None:
            continue
        pts.append({"name": name, "lon": lon, "lat": lat, "group": grp})
    return pts, colmap


def read_input_file(path):
    """CLI 用: 按扩展名读取 csv/xlsx/txt"""
    if str(path).lower().endswith((".csv", ".xlsx", ".xls")):
        pts, _ = read_table(path)
        return pts
    with open(path, "r", encoding="utf-8-sig") as fh:
        return parse_text_input(fh.read())


def normalize_points(pts, coord_sys="wgs84", geocoder="nominatim", amap_key=None,
                     city=None, china_only=True, progress=None):
    """补全缺失坐标(地理编码) + 坐标系转换 + 经纬度对调纠错; 返回 (points, failed)"""
    out, failed = [], []
    for i, p in enumerate(pts):
        if progress:
            progress(i, len(pts), p.get("name", ""))
        lon, lat = _tofloat(p.get("lon")), _tofloat(p.get("lat"))
        src, disp = "输入坐标", ""
        if lon is None or lat is None:
            res = geocode(p["name"], geocoder=geocoder, amap_key=amap_key,
                          city=city, china_only=china_only)
            if not res:
                failed.append(p["name"])
                continue
            lon, lat, src, disp = res["lon"], res["lat"], res["source"], res.get("display", "")
        else:
            if abs(lat) > 90 >= abs(lon):
                lon, lat = lat, lon  # 明显的经纬度对调
            elif china_only and 3 < lon < 54 and 73 < lat < 136:
                lon, lat = lat, lon  # 中国范围内的对调
            if coord_sys == "gcj02":
                lon, lat = gcj02_to_wgs84(lon, lat)
        q = dict(p)
        q.update(lon=round(float(lon), 6), lat=round(float(lat), 6),
                 source=src, display=disp)
        out.append(q)
    # 组内顺序编号
    seq = {}
    for p in out:
        g = p.get("group", "线路1")
        seq[g] = seq.get(g, 0) + 1
        p["seq"] = seq[g]
    return out, failed


def plan_routes(points, engine="auto", mode="driving",
                ors_key=None, amap_key=None, osrm_url=None, log=None, alternatives=0):
    """按 group 分组规划路线; 返回 (routes: {group: route}, errors: [str])"""
    groups = {}
    for p in points:
        groups.setdefault(p.get("group", "线路1"), []).append(p)
    routes, errors = {}, []
    for g, gpts in groups.items():
        if len(gpts) < 2:
            errors.append(f"线路[{g}] 只有 {len(gpts)} 个有效点, 至少需要 2 个, 已跳过")
            continue
        coords = [(p["lon"], p["lat"]) for p in gpts]
        try:
            rt = build_route(coords, engine=engine, mode=mode, ors_key=ors_key,
                             amap_key=amap_key, osrm_url=osrm_url, log=log,
                             alternatives=alternatives)
            rt.setdefault("alts", [])
            rt["mode"], rt["engine"] = mode, engine
            rt["point_names"] = [p["name"] for p in gpts]
            routes[g] = rt
        except Exception as e:
            errors.append(f"线路[{g}] 规划失败: {e}")
    return routes, errors


# ============================================================
# 导出 SHP / GeoJSON / 地图
# ============================================================
def _write_aux(base):
    with open(base + ".prj", "w", encoding="ascii") as f:
        f.write(WGS84_PRJ)
    with open(base + ".cpg", "w", encoding="ascii") as f:
        f.write("UTF-8")


def export_shp(points, routes, out_dir, prefix="navi"):
    """导出 3 个 shapefile: 点 / 全程线(每条线路一个要素) / 分段线(每段一个要素)"""
    import shapefile
    os.makedirs(out_dir, exist_ok=True)
    made = []

    base = os.path.join(out_dir, f"{prefix}_points")
    w = shapefile.Writer(base, shapeType=shapefile.POINT, encoding="utf-8")
    w.field("seq", "N", 10, 0)
    w.field("name", "C", 120)
    w.field("group", "C", 60)
    w.field("lon", "N", 18, 8)
    w.field("lat", "N", 18, 8)
    w.field("source", "C", 20)
    for p in points:
        w.point(p["lon"], p["lat"])
        w.record(p.get("seq", 0), p["name"], str(p.get("group", "")),
                 p["lon"], p["lat"], p.get("source", ""))
    w.close()
    _write_aux(base)
    made.append(base)

    base = os.path.join(out_dir, f"{prefix}_routes")
    w = shapefile.Writer(base, shapeType=shapefile.POLYLINE, encoding="utf-8")
    w.field("group", "C", 60)
    w.field("mode", "C", 12)
    w.field("engine", "C", 12)
    w.field("dist_km", "N", 18, 3)
    w.field("dur_min", "N", 18, 1)
    w.field("start", "C", 120)
    w.field("end", "C", 120)
    for g, rt in routes.items():
        w.line([rt["coords"]])
        names = rt.get("point_names", ["", ""])
        w.record(str(g), rt.get("mode", ""), rt.get("engine", ""),
                 rt["distance"] / 1000.0, rt["duration"] / 60.0, names[0], names[-1])
    w.close()
    _write_aux(base)
    made.append(base)

    base = os.path.join(out_dir, f"{prefix}_legs")
    w = shapefile.Writer(base, shapeType=shapefile.POLYLINE, encoding="utf-8")
    w.field("group", "C", 60)
    w.field("leg", "N", 10, 0)
    w.field("alt", "N", 10, 0)
    w.field("from_pt", "C", 120)
    w.field("to_pt", "C", 120)
    w.field("dist_km", "N", 18, 3)
    w.field("dur_min", "N", 18, 1)
    for g, rt in routes.items():
        names = rt.get("point_names", [])
        for i, leg in enumerate(rt["legs"]):
            w.line([leg["coords"]])
            w.record(str(g), i + 1, 0,
                     names[i] if i < len(names) else "",
                     names[i + 1] if i + 1 < len(names) else "",
                     leg["distance"] / 1000.0, leg["duration"] / 60.0)
        for a in rt.get("alts", []):
            li = a["leg"] - 1
            w.line([a["coords"]])
            w.record(str(g), a["leg"], a["alt"],
                     names[li] if li < len(names) else "",
                     names[li + 1] if li + 1 < len(names) else "",
                     a["distance"] / 1000.0, a["duration"] / 60.0)
    w.close()
    _write_aux(base)
    made.append(base)

    files = []
    for b in made:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            if os.path.exists(b + ext):
                files.append(b + ext)
    return files


def export_geojson(points, routes, out_dir, prefix="navi"):
    os.makedirs(out_dir, exist_ok=True)
    pf = [{"type": "Feature",
           "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
           "properties": {"seq": p.get("seq"), "name": p["name"],
                          "group": p.get("group", ""), "source": p.get("source", "")}}
          for p in points]
    rf, lf = [], []
    for g, rt in routes.items():
        names = rt.get("point_names", [])
        rf.append({"type": "Feature",
                   "geometry": {"type": "LineString", "coordinates": rt["coords"]},
                   "properties": {"group": str(g), "mode": rt.get("mode", ""),
                                  "engine": rt.get("engine", ""),
                                  "distance_m": round(rt["distance"], 1),
                                  "duration_s": round(rt["duration"], 1)}})
        for i, leg in enumerate(rt["legs"]):
            lf.append({"type": "Feature",
                       "geometry": {"type": "LineString", "coordinates": leg["coords"]},
                       "properties": {"group": str(g), "leg": i + 1, "alt": 0,
                                      "from": names[i] if i < len(names) else "",
                                      "to": names[i + 1] if i + 1 < len(names) else "",
                                      "distance_m": round(leg["distance"], 1),
                                      "duration_s": round(leg["duration"], 1)}})
        for a in rt.get("alts", []):
            li = a["leg"] - 1
            lf.append({"type": "Feature",
                       "geometry": {"type": "LineString", "coordinates": a["coords"]},
                       "properties": {"group": str(g), "leg": a["leg"], "alt": a["alt"],
                                      "from": names[li] if li < len(names) else "",
                                      "to": names[li + 1] if li + 1 < len(names) else "",
                                      "distance_m": round(a["distance"], 1),
                                      "duration_s": round(a["duration"], 1)}})
    files = []
    for suffix, feats in (("points", pf), ("routes", rf), ("legs", lf)):
        path = os.path.join(out_dir, f"{prefix}_{suffix}.geojson")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": feats}, fh,
                      ensure_ascii=False, indent=1)
        files.append(path)
    return files


def zip_files(paths, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, os.path.basename(p))
    return zip_path


PALETTE = ["#e6194b", "#4363d8", "#3cb44b", "#f58231", "#911eb4",
           "#008080", "#f032e6", "#9a6324", "#800000", "#000075"]


def fmt_km(m):
    return f"{m / 1000:.2f} km" if m >= 1000 else f"{m:.0f} m"


def fmt_dur(s):
    s = int(s)
    return f"{s // 3600}小时{s % 3600 // 60}分" if s >= 3600 else f"{s // 60}分{s % 60}秒"


def make_map(points, routes, out_html=None):
    import folium
    lats = [p["lat"] for p in points] or [35.0]
    lons = [p["lon"] for p in points] or [105.0]
    m = folium.Map(location=[sum(lats) / len(lats), sum(lons) / len(lons)],
                   zoom_start=13, tiles=None, control_scale=True)
    folium.TileLayer("OpenStreetMap", name="OSM 标准").add_to(m)
    folium.TileLayer("CartoDB positron", name="浅色底图").add_to(m)

    for gi, (g, rt) in enumerate(routes.items()):
        color = PALETTE[gi % len(PALETTE)]
        fg = folium.FeatureGroup(name=f"线路 {g}", show=True)
        names = rt.get("point_names", [])
        for a in rt.get("alts", []):
            li = a["leg"] - 1
            frm = names[li] if li < len(names) else ""
            to = names[li + 1] if li + 1 < len(names) else ""
            folium.PolyLine([(c[1], c[0]) for c in a["coords"]],
                            color=color, weight=3, opacity=0.65, dash_array="8,8",
                            tooltip=f"{g} 第{a['leg']}段 备选方案{a['alt']}: {frm} → {to} | "
                                    f"{fmt_km(a['distance'])} / {fmt_dur(a['duration'])}"
                            ).add_to(fg)
        for i, leg in enumerate(rt["legs"]):
            frm = names[i] if i < len(names) else ""
            to = names[i + 1] if i + 1 < len(names) else ""
            folium.PolyLine([(c[1], c[0]) for c in leg["coords"]],
                            color=color, weight=5, opacity=0.85,
                            tooltip=f"{g} 第{i + 1}段: {frm} → {to} | "
                                    f"{fmt_km(leg['distance'])} / {fmt_dur(leg['duration'])}"
                            ).add_to(fg)
        fg.add_to(m)

    grouped = {}
    for p in points:
        grouped.setdefault(p.get("group", "线路1"), []).append(p)
    for gi, (g, gpts) in enumerate(grouped.items()):
        color = PALETTE[gi % len(PALETTE)]
        n = len(gpts)
        for j, p in enumerate(gpts):
            bg = "#2e7d32" if j == 0 else ("#c62828" if j == n - 1 else color)
            folium.Marker(
                [p["lat"], p["lon"]],
                tooltip=f"{p.get('seq', j + 1)}. {p['name']} ({g})",
                popup=folium.Popup(
                    f"<b>{p['name']}</b><br>线路: {g}<br>"
                    f"经度: {p['lon']}<br>纬度: {p['lat']}<br>来源: {p.get('source', '')}",
                    max_width=260),
                icon=folium.DivIcon(html=(
                    f'<div style="background:{bg};color:#fff;border-radius:50%;'
                    f'width:26px;height:26px;line-height:26px;text-align:center;'
                    f'font-size:13px;font-weight:bold;border:2px solid #fff;'
                    f'box-shadow:0 1px 4px rgba(0,0,0,.4)">{p.get("seq", j + 1)}</div>'),
                    icon_size=(26, 26), icon_anchor=(13, 13)),
            ).add_to(m)

    if routes:
        summary = "".join(
            f'<div style="margin:2px 0"><span style="display:inline-block;width:10px;height:10px;'
            f'background:{PALETTE[i % len(PALETTE)]};border-radius:2px;margin-right:6px"></span>'
            f'{g}: {fmt_km(rt["distance"])} / 约{fmt_dur(rt["duration"])}</div>'
            for i, (g, rt) in enumerate(routes.items()))
        m.get_root().html.add_child(folium.Element(
            f'<div style="position:fixed;top:12px;right:12px;z-index:9999;background:#fff;'
            f'padding:10px 14px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.25);'
            f'font-size:13px;font-family:sans-serif"><b>路线概览</b>{summary}</div>'))

    folium.LayerControl(position="topleft", collapsed=True).add_to(m)
    if points:
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(40, 40))
    if out_html:
        m.save(out_html)
    return m


# ---------- 静态出图: OSM底图 + 图例 + 比例尺 + 指北针 ----------
_MERC_R = 6378137.0


def _merc(lon, lat):
    x = math.radians(lon) * _MERC_R
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * _MERC_R
    return x, y


def _merc_lat_inv(y):
    return math.degrees(2 * math.atan(math.exp(y / _MERC_R)) - math.pi / 2)


def make_static_map(points, routes, out_png, title="轨迹导航图"):
    """导出制图版 PNG: OSM 底图、分色路线、点位标注、图例、比例尺、指北针 (300dpi)"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import patheffects
    from matplotlib.lines import Line2D

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                       "Noto Sans CJK SC", "Noto Sans SC",
                                       "WenQuanYi Zen Hei", "Arial Unicode MS",
                                       "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False
    halo = [patheffects.withStroke(linewidth=2.5, foreground="white")]

    fig, ax = plt.subplots(figsize=(11, 8.5))

    # 先确定范围, 后续标注可按位置避让、不出图框
    mx = [_merc(p["lon"], p["lat"])[0] for p in points]
    my = [_merc(p["lon"], p["lat"])[1] for p in points]
    for rt in routes.values():
        for c in rt["coords"]:
            x, y = _merc(c[0], c[1])
            mx.append(x)
            my.append(y)
        for a in rt.get("alts", []):
            for c in a["coords"]:
                x, y = _merc(c[0], c[1])
                mx.append(x)
                my.append(y)
    dxs = max(max(mx) - min(mx), 500.0)
    dys = max(max(my) - min(my), 500.0)
    pad = max(dxs, dys) * 0.15
    ax.set_xlim(min(mx) - pad, max(mx) + pad)
    ax.set_ylim(min(my) - pad, max(my) + pad)
    ax.set_aspect("equal")
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()

    handles = []
    has_alts = any(rt.get("alts") for rt in routes.values())
    for gi, (g, rt) in enumerate(routes.items()):
        color = PALETTE[gi % len(PALETTE)]
        for a in rt.get("alts", []):
            xs, ys = zip(*(_merc(c[0], c[1]) for c in a["coords"]))
            ax.plot(xs, ys, color=color, lw=1.8, zorder=2.5, alpha=0.75,
                    linestyle=(0, (5, 4)))
        for leg in rt["legs"]:
            xs, ys = zip(*(_merc(c[0], c[1]) for c in leg["coords"]))
            ax.plot(xs, ys, color=color, lw=3, zorder=3,
                    solid_capstyle="round", solid_joinstyle="round", alpha=0.9)
        handles.append(Line2D([], [], color=color, lw=3,
                              label=f"{g}（{fmt_km(rt['distance'])} / 约{fmt_dur(rt['duration'])}）"))
    if has_alts:
        handles.append(Line2D([], [], color="#666666", lw=1.8, linestyle=(0, (5, 4)),
                              label="备选路线"))

    grouped = {}
    for p in points:
        grouped.setdefault(p.get("group", "线路1"), []).append(p)
    for g, gpts in grouped.items():
        n = len(gpts)
        for j, p in enumerate(gpts):
            x, y = _merc(p["lon"], p["lat"])
            fc = "#2e7d32" if j == 0 else ("#c62828" if j == n - 1 else "white")
            ax.scatter(x, y, s=90, c=fc, edgecolors="black", linewidths=1.2, zorder=5)
            fx = (x - x0) / (x1 - x0)
            fy = (y - y0) / (y1 - y0)
            ha = "right" if fx > 0.72 else "left"
            va = "top" if fy > 0.82 else "bottom"
            ax.annotate(f"{p.get('seq', j + 1)}.{p['name']}", (x, y),
                        textcoords="offset points",
                        xytext=(-7 if ha == "right" else 7,
                                -9 if va == "top" else 7),
                        ha=ha, va=va,
                        fontsize=9, zorder=6, path_effects=halo)
    handles += [
        Line2D([], [], marker="o", linestyle="none", markerfacecolor="#2e7d32",
               markeredgecolor="black", markersize=9, label="起点"),
        Line2D([], [], marker="o", linestyle="none", markerfacecolor="#c62828",
               markeredgecolor="black", markersize=9, label="终点"),
        Line2D([], [], marker="o", linestyle="none", markerfacecolor="white",
               markeredgecolor="black", markersize=9, label="途经点"),
    ]

    try:
        import contextily as ctx
        ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik,
                        attribution=False)
    except Exception:
        ax.set_facecolor("#eef2f5")  # 底图拉取失败时仍出图

    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_linewidth(1.2)
    ax.set_title(title, fontsize=16, fontweight="bold", pad=12)

    leg = ax.legend(handles=handles, loc="best", fontsize=9,
                    title="图  例", title_fontsize=10, framealpha=0.92,
                    borderpad=0.8, labelspacing=0.6)
    leg.set_zorder(10)

    # 比例尺 (按地图中心纬度校正 Web Mercator 变形)
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    m_per_unit = math.cos(math.radians(_merc_lat_inv((y0 + y1) / 2)))
    span_m = (x1 - x0) * m_per_unit
    target = span_m / 5
    nice = min([100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000],
               key=lambda v: abs(v - target))
    bar = nice / m_per_unit
    bx, by = x0 + (x1 - x0) * 0.05, y0 + (y1 - y0) * 0.06
    tick = (y1 - y0) * 0.012
    ax.plot([bx, bx + bar], [by, by], color="black", lw=2.5,
            solid_capstyle="butt", zorder=10)
    for t in (bx, bx + bar / 2, bx + bar):
        ax.plot([t, t], [by, by + tick], color="black", lw=1.5, zorder=10)

    def _bar_label(v):
        return f"{v / 1000:g} km" if nice >= 1000 else f"{v:g} m"

    for frac, v in ((0, 0), (0.5, nice / 2), (1, nice)):
        ax.annotate(_bar_label(v) if frac else "0", (bx + bar * frac, by + tick),
                    ha="center", va="bottom", fontsize=9, zorder=10,
                    path_effects=halo)

    # 指北针
    nx_, ny_ = x0 + (x1 - x0) * 0.055, y1 - (y1 - y0) * 0.14
    ah = (y1 - y0) * 0.065
    ax.annotate("", xy=(nx_, ny_ + ah), xytext=(nx_, ny_), zorder=10,
                arrowprops=dict(arrowstyle="-|>", color="black", lw=2.5,
                                mutation_scale=22))
    ax.annotate("N", (nx_, ny_ + ah), ha="center", va="bottom", fontsize=15,
                fontweight="bold", zorder=10, path_effects=halo)

    fig.savefig(out_png, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png
