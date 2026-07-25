@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    echo [首次运行] 正在创建虚拟环境并安装依赖，可能需要几分钟...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt || .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo 正在启动轨迹导航生成系统，浏览器将自动打开 http://localhost:8501
.venv\Scripts\python.exe -m streamlit run app.py
pause
