#!/bin/bash
# LightRAG 修复能力手动测试脚本
# 用法：./scripts/test_lightrag_repair_manual.sh <scenario>
# 场景列表：
#   1  - vdb_entities.json 截断（critical）
#   2  - vdb_relationships.json 截断（critical）
#   3  - vdb_chunks.json 截断（critical）
#   4  - graphml 删 5 节点（1328 孤儿 edge，major）
#   5  - entity_chunks 悬空 key（major）
#   6  - relation_chunks 悬空 key（major）
#   7  - text_chunks fake chunk（major）
#   8  - doc_status 大写污染（无检测器，但应用应正常启动）
#   9  - 组合损坏（场景 1 + 场景 4）
#   backup    - 备份当前真实数据
#   restore   - 恢复真实数据
#   status    - 查看当前状态
#   clean     - 杀残留进程

set -e

STORAGE_DIR=~/.niu/lightrag_storage
BACKUP_DIR=~/.niu/lightrag_storage_manual_test_backup
API_PORT=9876

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn(){ echo -e "${YELLOW}[WARN]${NC} $1"; }
err() { echo -e "${RED}[ERR]${NC} $1"; }

# 检查无残留进程
check_clean() {
    local pids=$(ps aux | grep -E "niu-launcher|niu_api|python.*-m niu_api" | grep -v grep | awk '{print $2}')
    if [ -n "$pids" ]; then
        err "发现残留 niu 进程：$pids"
        err "请先运行：$0 clean"
        exit 1
    fi
    local port=$(lsof -i :$API_PORT 2>/dev/null | tail -n +2)
    if [ -n "$port" ]; then
        err "端口 $API_PORT 被占用：$port"
        err "请先运行：$0 clean"
        exit 1
    fi
}

# 优雅停止 niu
stop_niu() {
    log "优雅停止 niu..."
    curl -s --max-time 5 -X POST http://127.0.0.1:$API_PORT/api/shutdown 2>/dev/null || true
    sleep 4
    # kill 残留 niu-launcher
    local pids=$(ps aux | grep -E "niu-launcher|^.*\./niu" | grep -v grep | awk '{print $2}')
    for pid in $pids; do
        kill -TERM $pid 2>/dev/null || true
    done
    # kill 残留 Electron（非 Helper）
    local epids=$(ps aux | grep -E "Electron\.app/Contents/MacOS/Electron " | grep -v "Helper" | awk '{print $2}')
    for pid in $epids; do
        kill -TERM $pid 2>/dev/null || true
    done
    sleep 2
}

# 等 API ready
wait_api() {
    log "等待 API ready..."
    for i in $(seq 1 30); do
        if curl -s --max-time 2 http://127.0.0.1:$API_PORT/health 2>/dev/null | grep -q '"status":"ok"'; then
            ok "API ready (${i}s)"
            return 0
        fi
        sleep 1
    done
    err "API 30 秒内未启动"
    return 1
}

# 启动 niu（后台）
start_niu() {
    log "启动 ./niu..."
    nohup ./niu > /tmp/niu_stdout.log 2>&1 &
    echo $! > /tmp/niu_pid
    disown
}

# 调用修复 API 并打印结果
call_repair() {
    log "调用修复 API..."
    local resp=$(curl -s --max-time 600 -X POST "http://127.0.0.1:$API_PORT/api/kg/lightrag/repair?target=all")
    echo "$resp" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
r = data.get('result', data)
check = r.get('check_result', {})
repair = r.get('repair_result', {})
print('  check_ok:        {}'.format(check.get('ok', '?')))
print('  critical_errors: {}'.format(check.get('critical_errors', '?')))
print('  major_errors:    {}'.format(check.get('major_errors', '?')))
print('  minor_errors:    {}'.format(check.get('minor_errors', '?')))
print('  repaired:        {}'.format(r.get('repaired', '?')))
skipped = repair.get('_skipped', [])
ran = [k for k in repair.keys() if not k.startswith('_')]
print('  repair_ran:      {}'.format(ran if ran else '[]'))
print('  repair_skipped:  {} 项'.format(len(skipped)))
"
}

# ============ 场景函数 ============

scenario_1() {
    log "场景 1: vdb_entities.json 截断（critical）"
    cp "$STORAGE_DIR/vdb_entities.json" /tmp/vdb_entities_backup.json
    echo '{"abc123' > "$STORAGE_DIR/vdb_entities.json"
    log "已截断 vdb_entities.json (备份在 /tmp/vdb_entities_backup.json)"
}

scenario_2() {
    log "场景 2: vdb_relationships.json 截断（critical）"
    cp "$STORAGE_DIR/vdb_relationships.json" /tmp/vdb_relationships_backup.json
    echo '{"abc123' > "$STORAGE_DIR/vdb_relationships.json"
    log "已截断 vdb_relationships.json"
}

scenario_3() {
    log "场景 3: vdb_chunks.json 截断（critical）"
    cp "$STORAGE_DIR/vdb_chunks.json" /tmp/vdb_chunks_backup.json
    echo '{"abc123' > "$STORAGE_DIR/vdb_chunks.json"
    log "已截断 vdb_chunks.json"
}

scenario_4() {
    log "场景 4: graphml 删 5 节点（产生孤儿 edge，major）"
    cp "$STORAGE_DIR/graph_chunk_entity_relation.graphml" /tmp/graphml_backup.graphml
    python3 -c "
import xml.etree.ElementTree as ET
p = '$STORAGE_DIR/graph_chunk_entity_relation.graphml'
tree = ET.parse(p)
root = tree.getroot()
ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
# 找到所有父元素中的 node
def find_parents(elem, target_tag):
    parents = []
    for parent in elem.iter():
        for child in parent:
            if child.tag == target_tag:
                parents.append(parent)
    return parents
nodes_parents = find_parents(root, '{http://graphml.graphdrawing.org/xmlns}node')
removed = 0
for parent in nodes_parents[:1]:
    nodes = [c for c in parent if c.tag == '{http://graphml.graphdrawing.org/xmlns}node']
    for n in nodes[:5]:
        parent.remove(n)
        removed += 1
tree.write(p, xml_declaration=True, encoding='utf-8')
print(f'  删除 {removed} 个 node')
"
    log "已删 5 节点 (备份在 /tmp/graphml_backup.graphml)"
}

scenario_5() {
    log "场景 5: entity_chunks 加悬空 key（major）"
    cp "$STORAGE_DIR/kv_store_entity_chunks.json" /tmp/entity_chunks_backup.json
    python3 -c "
import json
p = '$STORAGE_DIR/kv_store_entity_chunks.json'
data = json.loads(open(p).read())
data['NONEXISTENT_ENTITY_FOR_TEST'] = {'chunk_ids': ['NONEXISTENT_CHUNK_12345'], 'count': 1}
open(p, 'w').write(json.dumps(data, ensure_ascii=False))
print(f'  加了悬空 key NONEXISTENT_ENTITY_FOR_TEST')
"
}

scenario_6() {
    log "场景 6: relation_chunks 加悬空 key（major）"
    cp "$STORAGE_DIR/kv_store_relation_chunks.json" /tmp/relation_chunks_backup.json
    python3 -c "
import json
p = '$STORAGE_DIR/kv_store_relation_chunks.json'
data = json.loads(open(p).read())
data['NONEXISTENT_SRC\x1fNONEXISTENT_TGT'] = ['NONEXISTENT_CHUNK_12345']
open(p, 'w').write(json.dumps(data, ensure_ascii=False))
print(f'  加了悬空 key NONEXISTENT_SRC<SEP>NONEXISTENT_TGT')
"
}

scenario_7() {
    log "场景 7: text_chunks 加 fake chunk（触发 vdb_chunks_missing，major）"
    cp "$STORAGE_DIR/kv_store_text_chunks.json" /tmp/text_chunks_backup.json
    python3 -c "
import json
p = '$STORAGE_DIR/kv_store_text_chunks.json'
data = json.loads(open(p).read())
data['FAKE_CHUNK_FOR_TEST_12345'] = {
    'content': 'fake content for test',
    'full_doc_id': 'doc-FAKE_DOC_12345',
    'chunk_id': 'FAKE_CHUNK_FOR_TEST_12345',
    'tokens': 10,
    'chunk_order_index': 0,
    'status': 'processed'
}
open(p, 'w').write(json.dumps(data, ensure_ascii=False))
print(f'  加了 fake chunk')
"
}

scenario_8() {
    log "场景 8: doc_status 大写污染（验证修复不再写入大写）"
    cp "$STORAGE_DIR/kv_store_doc_status.json" /tmp/doc_status_backup.json
    python3 -c "
import json
p = '$STORAGE_DIR/kv_store_doc_status.json'
data = json.loads(open(p).read())
count = 0
for k, v in list(data.items())[:5]:
    if isinstance(v, dict):
        v['status'] = 'PROCESSED'  # 大写污染
        count += 1
open(p, 'w').write(json.dumps(data, ensure_ascii=False))
print(f'  污染了 {count} 条为 PROCESSED 大写')
"
}

scenario_9() {
    log "场景 9: 组合损坏（场景 1 + 场景 4）"
    scenario_1
    scenario_4
}

# ============ 备份/恢复 ============

do_backup() {
    log "备份真实数据到 $BACKUP_DIR ..."
    rm -rf "$BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    cp -a "$STORAGE_DIR"/* "$BACKUP_DIR"/
    ok "备份完成：$BACKUP_DIR"
    ls "$BACKUP_DIR" | wc -l | xargs echo "  文件数:"
}

do_restore() {
    if [ ! -d "$BACKUP_DIR" ]; then
        err "备份目录不存在：$BACKUP_DIR"
        err "请先运行：$0 backup"
        exit 1
    fi
    log "恢复真实数据从 $BACKUP_DIR ..."
    rm -rf "$STORAGE_DIR"
    mkdir -p "$STORAGE_DIR"
    cp -a "$BACKUP_DIR"/* "$STORAGE_DIR"/
    ok "恢复完成"
    # 清理 /tmp 临时备份
    rm -f /tmp/vdb_entities_backup.json /tmp/vdb_relationships_backup.json \
          /tmp/vdb_chunks_backup.json /tmp/graphml_backup.graphml \
          /tmp/entity_chunks_backup.json /tmp/relation_chunks_backup.json \
          /tmp/text_chunks_backup.json /tmp/doc_status_backup.json
    log "已清理 /tmp 临时备份"
}

do_status() {
    log "当前状态："
    if [ -d "$BACKUP_DIR" ]; then
        ok "备份存在：$BACKUP_DIR ($(ls $BACKUP_DIR | wc -l | tr -d ' ') 个文件)"
    else
        warn "无备份"
    fi
    log "doc_status 状态："
    python3 -c "
import json
from pathlib import Path
p = Path('$STORAGE_DIR/kv_store_doc_status.json')
if p.exists():
    data = json.loads(p.read_text())
    statuses = {}
    for k, v in data.items():
        s = v.get('status', 'MISSING') if isinstance(v, dict) else 'NON_DICT'
        statuses[s] = statuses.get(s, 0) + 1
    for s, c in sorted(statuses.items()):
        print(f'  {s!r}: {c}')
" 2>/dev/null || warn "无法读取"
    log "进程状态："
    ps aux | grep -E "niu-launcher|niu_api|python.*-m niu_api" | grep -v grep | head -5 || ok "无 niu 进程"
}

do_clean() {
    log "清理残留进程..."
    stop_niu
    sleep 2
    ok "清理完成"
}

# ============ 主流程 ============

main() {
    local scenario=${1:-help}

    case "$scenario" in
        backup)
            do_backup
            ;;
        restore)
            do_restore
            ;;
        status)
            do_status
            ;;
        clean)
            do_clean
            ;;
        1|2|3|4|5|6|7|8|9)
            check_clean
            # 确保有备份
            if [ ! -d "$BACKUP_DIR" ]; then
                warn "首次运行，自动备份..."
                do_backup
            fi
            # 制造损坏
            "scenario_$scenario"
            echo ""
            log "损坏制造完成。现在你可以："
            echo "  1. 启动 ./niu，观察启动行为（可能弹修复对话框或自动修复）"
            echo "  2. 或调用 HTTP 修复 API：curl -X POST http://127.0.0.1:$API_PORT/api/kg/lightrag/repair?target=all"
            echo "  3. 测试完毕后恢复：$0 restore"
            echo ""
            log "是否要自动启动 ./niu 并调用修复 API？(y/N)"
            read -r answer
            if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
                start_niu
                if wait_api; then
                    call_repair
                    echo ""
                    log "测试完成。运行 '$0 stop' 停止，'$0 restore' 恢复数据"
                else
                    err "API 启动失败，查看日志：tail -50 /tmp/niu_stdout.log"
                fi
            fi
            ;;
        stop)
            stop_niu
            ok "已停止"
            ;;
        help|*)
            echo "LightRAG 修复能力手动测试脚本"
            echo ""
            echo "用法：$0 <command>"
            echo ""
            echo "命令："
            echo "  backup    备份真实数据到 $BACKUP_DIR"
            echo "  restore   从备份恢复真实数据"
            echo "  status    查看当前状态（备份/数据/进程）"
            echo "  clean     清理残留 niu 进程"
            echo "  stop      优雅停止 niu"
            echo "  1-9       制造对应损坏场景"
            echo ""
            echo "典型流程："
            echo "  $0 backup           # 先备份"
            echo "  $0 1                # 制造场景 1"
            echo "  ./niu               # 启动程序（自动检测修复）"
            echo "  $0 stop             # 停止"
            echo "  $0 restore          # 恢复数据"
            ;;
    esac
}

main "$@"
