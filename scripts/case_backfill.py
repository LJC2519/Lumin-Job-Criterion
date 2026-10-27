#!/usr/bin/env python3
"""case_backfill.py — 路由失败回填：把真实使用中「LJC 没推荐对」的案例追加进评估集。

对应视频量化优化原则：线上问题 → 评测用例 → 防回归。
追加后建议跑 run_eval.py：新用例当前不通过 = 已知欠账；通过 = 防回归护栏。

用法：
  python3 case_backfill.py --query "帮我找个能审合同的" \
      --accept contract-review,contract-review-pro [--kind zh] [--note "真实使用回填"] [--file 覆盖路径]

--file 用于测试（默认写真实评估集 eval/queries.json，写前自动备份）。
"""

import argparse
import datetime
import json
import os
import shutil
import sys

DEFAULT_FILE = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "eval", "queries.json"))


def main():
    ap = argparse.ArgumentParser(description="路由失败案例回填进评估集")
    ap.add_argument("--query", required=True, help="真实用户 query 原文")
    ap.add_argument("--accept", required=True, help="正确答案技能名，逗号分隔")
    ap.add_argument("--kind", default="zh", choices=["zh", "en"])
    ap.add_argument("--note", default="", help="备注（默认自动注明回填来源与日期）")
    ap.add_argument("--file", default="", help="评估集路径（默认 eval/queries.json）")
    args = ap.parse_args()

    path = os.path.abspath(args.file) if args.file else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval", "queries.json"))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases")
    if not isinstance(cases, list):
        sys.exit("评估集格式异常：缺少 cases 数组（{}）".format(path))

    next_id = max((c.get("id", 0) for c in cases if isinstance(c.get("id"), int)), default=0) + 1
    accept = [s.strip() for s in args.accept.split(",") if s.strip()]
    note = args.note or "回填 {}：来源=真实使用路由失败".format(
        datetime.date.today().isoformat())
    case = {"id": next_id, "query": args.query, "kind": args.kind,
            "accept": accept, "note": note}
    cases.append(case)

    backup = path + ".bak"
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("✅ 已回填案例 #{} → {}（备份: {}.bak）".format(next_id, path, path))
    print("   query: {}".format(args.query))
    print("   accept: {}".format(", ".join(accept)))
    print("下一步：python3 {}/eval/run_eval.py 复跑——新用例不通过=已知欠账，通过=防回归护栏。".format(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


if __name__ == "__main__":
    main()
