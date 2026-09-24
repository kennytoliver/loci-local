#!/usr/bin/env bash
# 一键跑全套闸门并汇总。任一红都不算做完（规范第八节）。
#
# 用法：
#   bash tools/run_gates.sh                 # 全部（约 3 分钟）
#   bash tools/run_gates.sh fast            # 快速集：跳过多秒级的重型闸门（约 30 秒）
#   bash tools/run_gates.sh browser         # 只跑浏览器类
#   bash tools/run_gates.sh audit_tokens    # 只跑名字含该关键字的
#
# 为什么区分快慢：光看"全绿"没法判断这次改动值不值得等 3 分钟。
# 重型闸门分两类 ——
#   · 浏览器类：每道都要**冷启一次无头 Chrome** 再加载页面（单次 7~28s）
#   · 性能类：故意在几百组数据上跑慢算法做对拍（单次 24~29s）
# 改文案/改样式的日常迭代跑 fast 就够；碰会话管线、性能路径时再跑全量。
#
# 前置：面板已在 8787 运行（浏览器类闸门需要它）。
# Windows 下需要 NODE_PATH 指向装了 puppeteer-core 的 node_modules。
set -u

cd "$(dirname "$0")/.." || exit 1

NODE="${NODE:-C:/Users/user/.workbuddy/binaries/node/versions/22.22.2-3/node.exe}"
PY="${PY:-C:/Users/user/.workbuddy/binaries/python/envs/default/Scripts/python.exe}"
export NODE_PATH="${NODE_PATH:-C:/Users/user/.workbuddy/binaries/node/workspace/node_modules}"
BASE="${BASE:-http://127.0.0.1:8787}"
FILTER="${1:-}"

# 重型闸门：出现名字里的关键字就归为"慢"
HEAVY="audit_tokens measure_layers smoke_panel verify_link_shots verify_link_measure verify_slist_rows verify_pane_guide verify_sel_feedback verify_frames verify_ui_polish verify_shutdown verify_audit_perf verify_extract_perf shot_new_pages verify_content"

# 名称 | 命令  （顺序按规范第八节；越靠前越快）
GATES=(
  "check_js_syntax|$NODE tools/check_js_syntax.js $BASE"
  "audit_tokens|$NODE tools/audit_tokens.js $BASE"
  "check_design|$PY tools/check_design.py panel.py"
  "test_mcp|$PY test_mcp.py"
  "test_panel|$PY test_panel.py"
  "verify_conn_drop|$PY tools/verify_conn_drop.py"
  "verify_session_flow|$PY tools/verify_session_flow.py"
  "verify_session_time|$PY tools/verify_session_time.py"
  "verify_session_dedup|$PY tools/verify_session_dedup.py"
  "verify_scan_sources|$PY tools/verify_scan_sources.py"
  "verify_skills|$PY tools/verify_skills.py"
  "verify_skill_detail|$NODE tools/verify_skill_detail.js $BASE"
  "verify_content|$NODE tools/verify_content.js $BASE"
  "verify_extract_perf|$PY tools/verify_extract_perf.py"
  "verify_rules_migration|$PY tools/verify_rules_migration.py"
  "verify_shutdown|$PY tools/verify_shutdown.py"
  "verify_panel_api|$PY tools/verify_panel_api.py $BASE"
  "verify_csrf|$PY tools/verify_csrf.py $BASE"
  "verify_audit_perf|$PY tools/verify_audit_perf.py"
  "measure_layers|$NODE tools/measure_layers.js $BASE"
  "smoke_panel|$NODE tools/smoke_panel.js $BASE"
  "verify_link_shots|$NODE tools/verify_link_shots.js $BASE"
  "verify_link_measure|$NODE tools/verify_link_measure.js $BASE"
  "verify_slist_rows|$NODE tools/verify_slist_rows.js $BASE"
  "verify_pane_guide|$NODE tools/verify_pane_guide.js $BASE"
  "verify_sel_feedback|$NODE tools/verify_sel_feedback.js $BASE"
  "verify_frames|$NODE tools/verify_frames.js $BASE"
  "verify_ui_polish|$NODE tools/verify_ui_polish.js $BASE"
  "shot_new_pages|$NODE tools/shot_new_pages.js $BASE"
)

is_heavy() {
  local n="$1" k
  for k in $HEAVY; do [ "$n" = "$k" ] && return 0; done
  return 1
}

pass=0; fail=0; skip=0; failed_names=()
declare -a LINES

for entry in "${GATES[@]}"; do
  name="${entry%%|*}"
  cmd="${entry#*|}"
  if [ -n "$FILTER" ]; then
    case "$FILTER" in
      fast)    is_heavy "$name" && { skip=$((skip+1)); continue; } ;;
      heavy)   is_heavy "$name" || { skip=$((skip+1)); continue; } ;;
      *)       [[ "$name" != *"$FILTER"* ]] && continue ;;
    esac
  fi

  printf '── %-22s … ' "$name"
  t0=$SECONDS
  out=$(eval "$cmd" 2>&1)
  code=$?
  dt=$((SECONDS - t0))
  if [ $code -eq 0 ]; then
    printf 'PASS  %ss\n' "$dt"; pass=$((pass+1))
  else
    printf 'FAIL (exit=%d)  %ss\n' "$code" "$dt"; fail=$((fail+1)); failed_names+=("$name")
    echo "$out" | grep -iE 'FAIL|✗|失败|error|Traceback' | head -8 | sed 's/^/      /'
  fi
  LINES+=("$(printf '%-24s %-10s %3ss' "$name" "$([ $code -eq 0 ] && echo PASS || echo "FAIL($code)")" "$dt")")
done

echo
echo "════════════════════════════════════"
for l in "${LINES[@]}"; do echo "  $l"; done
echo "════════════════════════════════════"
echo "  通过 $pass 项，失败 $fail 项$([ $skip -gt 0 ] && echo "，跳过 $skip 项（$FILTER）")"
[ $fail -gt 0 ] && echo "  红：${failed_names[*]}"
exit $((fail > 0))
