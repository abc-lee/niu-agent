#!/bin/bash
# 监控 niu_api 进程创建
# 每秒检查一次，记录所有新增的 python niu_api 进程

echo "=== 开始监控 niu_api 进程 ==="
echo "时间: $(date)"
echo ""

while true; do
    # 获取所有 niu_api 进程
    current=$(ps -eo pid,ppid,lstart,args | grep "niu_api" | grep -v grep | grep -v monitor)
    if [ -n "$current" ]; then
        # 检查是否有新进程（与上次记录对比）
        while IFS= read -r line; do
            pid=$(echo "$line" | awk '{print $1}')
            if ! grep -q "$pid" /tmp/niu_api_pids.txt 2>/dev/null; then
                echo "[$(date '+%H:%M:%S')] 新进程出现:"
                echo "  $line"
                echo "$pid" >> /tmp/niu_api_pids.txt
                # 记录父进程信息
                ppid=$(echo "$line" | awk '{print $2}')
                if [ "$ppid" != "1" ]; then
                    echo "  父进程:"
                    ps -p "$ppid" -o pid,ppid,command= 2>/dev/null
                fi
            fi
        done <<< "$current"
    fi
    sleep 2
done