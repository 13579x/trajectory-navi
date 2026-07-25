# -*- coding: utf-8 -*-
"""地图选点交互的端到端测试 (Playwright, 仅本地开发用)"""
import sys
import time

from playwright.sync_api import sync_playwright

URL = "http://localhost:8774"
OK = True


def note(msg, good=True):
    global OK
    print(("✔ " if good else "✘ ") + msg)
    if not good:
        OK = False


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.goto(URL, timeout=60000)
    page.wait_for_selector("text=轨迹导航生成系统", timeout=60000)
    time.sleep(3)

    page.click("text=地图选点")
    time.sleep(6)  # 等 st_folium iframe 加载

    frame_el = page.wait_for_selector("iframe[title='streamlit_folium.st_folium']",
                                      timeout=30000)
    box = frame_el.bounding_box()
    note(f"地图 iframe 已出现, 尺寸 {box['width']:.0f}x{box['height']:.0f}",
         box["height"] > 300)

    # 在地图中央点击第一个点
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(cx, cy)
    time.sleep(5)

    frame_el2 = page.query_selector("iframe[title='streamlit_folium.st_folium']")
    note("点击后地图 iframe 仍存在", frame_el2 is not None
         and frame_el2.bounding_box()["height"] > 300)
    note("右侧出现 '已选坐标'", page.query_selector("text=已选坐标") is not None)
    page.screenshot(path="output/e2e_1_clicked.png", full_page=False)

    # 添加该点
    page.click("text=添加该点")
    time.sleep(5)
    note("添加后地图 iframe 仍存在",
         page.query_selector("iframe[title='streamlit_folium.st_folium']") is not None)
    note("显示 '已添加 1 个轨迹点'", page.query_selector("text=已添加") is not None)

    # 第二个点(偏移位置)
    frame_el3 = page.query_selector("iframe[title='streamlit_folium.st_folium']")
    box3 = frame_el3.bounding_box()
    page.mouse.click(box3["x"] + box3["width"] * 0.62, box3["y"] + box3["height"] * 0.38)
    time.sleep(5)
    note("第二次点击后出现 '已选坐标'", page.query_selector("text=已选坐标") is not None)
    page.click("text=添加该点")
    time.sleep(5)
    body = page.inner_text("body")
    note("显示 '已添加 2 个轨迹点'", "已添加 2 个轨迹点" in body)
    note("仍停留在地图选点界面(未跳回其他输入方式)",
         page.query_selector("iframe[title='streamlit_folium.st_folium']") is not None)
    page.screenshot(path="output/e2e_2_added.png", full_page=True)

    browser.close()

print("\n结果:", "全部通过 ✅" if OK else "存在失败 ❌")
sys.exit(0 if OK else 1)
