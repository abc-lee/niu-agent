#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import requests
import json

# 测试主 Agent 调用
response = requests.post(
    "http://localhost:9876/chat",
    json={
        "session_id": "test",
        "message": "用浏览器打开百度首页，返回标题"
    },
    timeout=60
)

print(f"Status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    print(f"Reply: {data.get('reply', '')[:500]}")
else:
    print(f"Error: {response.text[:200]}")
