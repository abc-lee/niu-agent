#!/usr/bin/env bash
# 制造 vdb_entities.json 截断损坏现场，用于端到端测试启动阻塞 + repair 结果弹窗 + 截断修复。
#
# 安全策略：
# 1. 先备份用户真实 vdb_entities.json 到 vdb_entities.json.pre-corrupt-test.bak
# 2. 制造截断现场（head -c NNN 截断在 vector base64 中间）
# 3. 启动 ./niu，用户看到弹窗 → 点修复 → 看修复结果 → 程序退出
# 4. 测完恢复：mv vdb_entities.json.pre-corrupt-test.bak vdb_entities.json
#
# 用法：
#   ./scripts/make_vdb_corrupt_test_env.sh create   # 制造损坏现场
#   ./scripts/make_vdb_corrupt_test_env.sh restore  # 恢复真实 vdb
#   ./scripts/make_vdb_corrupt_test_env.sh status   # 查看当前状态

set -euo pipefail

STORAGE_DIR="${HOME}/.niu/lightrag_storage"
VDB_FILE="${STORAGE_DIR}/vdb_entities.json"
BACKUP_FILE="${STORAGE_DIR}/vdb_entities.json.pre-corrupt-test.bak"

if [[ ! -d "${STORAGE_DIR}" ]]; then
    echo "ERROR: lightrag_storage 目录不存在: ${STORAGE_DIR}"
    echo "       请先正常运行过程序一次，让 LightRAG 创建 storage 目录"
    exit 1
fi

if [[ ! -f "${VDB_FILE}" ]]; then
    echo "ERROR: vdb_entities.json 不存在: ${VDB_FILE}"
    echo "       请先正常运行过程序一次，让 LightRAG 写入 vdb"
    exit 1
fi

cmd="${1:-status}"
case "${cmd}" in
    create)
        # 1. 备份真实 vdb（如果备份已存在，不覆盖——避免覆盖前一次测试的备份）
        if [[ -f "${BACKUP_FILE}" ]]; then
            echo "WARN: 备份文件已存在: ${BACKUP_FILE}"
            echo "      可能上次测试未恢复。请先运行: $0 restore"
            read -p "      是否覆盖备份继续？(y/N) " confirm
            if [[ "${confirm:-N}" != "y" ]]; then
                echo "ABORTED"
                exit 1
            fi
        fi
        cp "${VDB_FILE}" "${BACKUP_FILE}"
        echo "BACKED UP: ${VDB_FILE} -> ${BACKUP_FILE}"

        # 2. 制造截断现场
        #    vdb_entities.json 格式：{"embedding_dim":N,"data":[{...},{...},...,"matrix":"base64..."}
        #    截断在 data 数组第二个 entity 的 vector 字段中间（base64 字符中间）
        #    注意：nano_vectordb 用 json.dump(storage, f, ensure_ascii=False) 保存，
        #    默认 separators 是 (', ', ': ')，所以输出是 "vector": "..."（冒号后有空格）。
        #    不能用 '"vector":"' 这个 marker（无空格），text.find 会返回 -1，导致
        #    TRUNCATE_POS=0 脚本报错退出。用正则 r'"vector"\s*:\s*"' 兼容有无空格。
        ORIG_SIZE=$(wc -c < "${VDB_FILE}")
        # 找第二个 entity 的 vector 字段位置，截断在它之后 50 字符
        # 用 python 正则匹配（shell 不好处理 JSON）
        # 文件路径通过 argv 传入，避免路径含空格或特殊字符时 shell 注入
        TRUNCATE_POS=$(python3 - "$VDB_FILE" <<'PYEOF'
import re
import sys

vdb_path = sys.argv[1]
with open(vdb_path, 'rb') as f:
    raw = f.read()
text = raw.decode('utf-8', errors='replace')
# 用正则匹配 '"vector"\s*:"'，兼容冒号后有无空格
# nano_vectordb save() 用默认 separators 输出 '"vector": "'，但兼容无空格更健壮
marker_re = re.compile(r'"vector"\s*:\s*"')
matches = list(marker_re.finditer(text))
if not matches:
    print(0)
    sys.exit(0)
if len(matches) == 1:
    # 只有一个 entity，截断在第一个 vector 中间
    truncate_at = matches[0].end() + 50
else:
    # 截断在第二个 vector 字段中间
    truncate_at = matches[1].end() + 50
print(truncate_at)
PYEOF
)
        if [[ "${TRUNCATE_POS}" == "0" ]]; then
            echo "ERROR: 无法找到 vector 字段，vdb 格式异常"
            mv "${BACKUP_FILE}" "${VDB_FILE}"
            exit 1
        fi

        # 用 head -c 截断（从备份截断到当前 vdb，不动备份）
        head -c "${TRUNCATE_POS}" "${BACKUP_FILE}" > "${VDB_FILE}"
        NEW_SIZE=$(wc -c < "${VDB_FILE}")
        echo "TRUNCATED: ${VDB_FILE} (${ORIG_SIZE} bytes -> ${NEW_SIZE} bytes, cut at ${TRUNCATE_POS})"
        echo ""
        echo "现在可以启动程序测试："
        echo "  cd REDACTED_USER_PATH/tools/ai-bot && ./niu"
        echo ""
        echo "预期："
        echo "  1. splash 启动 → 检测到 vdb 损坏 → 弹'LightRAG 数据异常'对话框"
        echo "  2. 点'是-尝试修复'"
        echo "  3. splash 显示'正在修复'"
        echo "  4. 修复完成后弹'修复结果'对话框，列出每个 vdb 的 status"
        echo "  5. 点'确定' → 程序退出"
        echo ""
        echo "测完恢复："
        echo "  $0 restore"
        ;;

    restore)
        if [[ ! -f "${BACKUP_FILE}" ]]; then
            echo "ERROR: 备份文件不存在: ${BACKUP_FILE}"
            echo "       无需恢复（可能从未执行 create）"
            exit 1
        fi
        mv "${BACKUP_FILE}" "${VDB_FILE}"
        echo "RESTORED: ${BACKUP_FILE} -> ${VDB_FILE}"
        echo "现在可以正常启动程序"
        ;;

    status)
        if [[ -f "${BACKUP_FILE}" ]]; then
            echo "STATUS: 测试模式（备份存在）"
            echo "  原始备份: ${BACKUP_FILE} ($(wc -c < "${BACKUP_FILE}") bytes)"
            echo "  当前 vdb: ${VDB_FILE} ($(wc -c < "${VDB_FILE}") bytes)"
            echo "  恢复命令: $0 restore"
        else
            echo "STATUS: 正常模式（无备份）"
            echo "  当前 vdb: ${VDB_FILE} ($(wc -c < "${VDB_FILE}") bytes)"
            echo "  制造损坏: $0 create"
        fi
        ;;

    *)
        echo "Usage: $0 {create|restore|status}"
        exit 1
        ;;
esac
