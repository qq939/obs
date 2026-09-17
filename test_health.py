#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  补齐 /health 健康检查端点，修复 docker-compose healthcheck 404
  导致容器一直显示 unhealthy 的问题。

超时机制: 60秒超时
"""
import os
import re
import sys
import json
import signal
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
COMPOSE_YML = os.path.join(ROOT, "docker-compose.yml")
BASE_URL = "http://127.0.0.1:80"


def timeout(seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            def timeout_handler(signum, frame):
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds}s")
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result
        return wrapper
    return decorator


def test_health_route_defined():
    """[1/3] server.py 定义了 GET /health 路由"""
    print("\n[1/3] server.py 定义 /health 路由...")
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        src = f.read()
    assert re.search(r'@app\.get\("/health"\)', src), "server.py 缺少 @app.get(\"/health\") 路由"
    m = re.search(r'@app\.get\("/health"\)\s*\n(?:async )?def \w+\([^)]*\):', src)
    assert m, "/health 路由后缺少函数定义"
    print("   ✓ GET /health 路由已定义")

    # 路由不能依赖上传目录等重资源，需是纯轻量探针
    body = src[m.end():m.end() + 600]
    assert re.search(r'return\s*\{', body), "/health 未直接返回字典响应"
    print("   ✓ /health 为轻量探针（直接返回字典）")


def test_health_endpoint_live():
    """[2/3] 运行中的服务 GET /health 返回 200 + status ok"""
    print("\n[2/3] 线上 /health 响应...")
    resp = requests.get(BASE_URL + "/health", timeout=5)
    assert resp.status_code == 200, f"/health 返回状态码 {resp.status_code}（修复前为 404）"
    data = resp.json()
    assert isinstance(data, dict), f"/health 响应不是 JSON 对象: {data!r}"
    assert data.get("status") == "ok", f"/health 响应缺少 status=ok: {data!r}"
    print(f"   ✓ /health -> 200 {json.dumps(data, ensure_ascii=False)}")


def test_compose_healthcheck_matches_route():
    """[3/3] docker-compose healthcheck 探测的路径在 server.py 中真实存在"""
    print("\n[3/3] compose healthcheck 与路由一致性...")
    with open(COMPOSE_YML, "r", encoding="utf-8") as f:
        compose = f.read()
    urls = re.findall(r"urlopen\('(http://[^']+)'\)", compose)
    assert urls, "docker-compose healthcheck 中未找到 urlopen 探测地址"
    paths = []
    for u in urls:
        p = re.sub(r"^https?://[^/]+", "", u) or "/"
        paths.append(p)
    print(f"   healthcheck 探测路径: {paths}")

    with open(SERVER_PY, "r", encoding="utf-8") as f:
        src = f.read()
    defined = set(re.findall(r'@app\.get\("([^"]+)"\)', src))
    for p in paths:
        assert p in defined, f"healthcheck 探测 {p}，但 server.py 未定义该路由（永远 unhealthy）"
    print("   ✓ healthcheck 路径均有对应路由定义")

    # 回归：核心页面仍正常，未因新增路由被 /{filename} 抢占
    for path in ["/", "/video"]:
        assert requests.get(BASE_URL + path, timeout=5).status_code == 200, f"{path} 回归失败"
    print("   ✓ 首页 / 视频页回归正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：补齐 /health 健康检查端点")
    print("=" * 60)
    try:
        test_health_route_defined()
        test_health_endpoint_live()
        test_compose_healthcheck_matches_route()
        print("\n" + "=" * 60)
        print("所有测试通过!")
        print("=" * 60)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"测试失败: {e}")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
