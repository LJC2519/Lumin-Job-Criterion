#!/usr/bin/env python3
"""recommend_outcome.py — 推荐结果回收与采纳率统计。

对应视频「无效意见率」原则：知道推荐质量，才能两条曲线一起看。
LJC 推荐后，把真实结果记一行（accepted=用户采纳 / rejected=用户明确拒绝 /
unused=推荐了没下文）；stats 汇总采纳率与按技能拆分。

用法：
  python3 recommend_outcome.py record --query "..." --recommended skill-x --outcome accepted [--note "..."] [--file 覆盖路径]
  python3 recommend_outcome.py stats [--last 50] [--file 覆盖路径]
"""

import argparse
import datetime
import json
import os
import sys
from collections import Counter

DEFAULT_FILE = os.path.expanduser(
    "~/.workbuddy/skills/lumin-job-criterion/eval/outcomes.jsonl")
VALID = ("accepted", "rejected", "unused")


def record(args):
    if args.outcome not in VALID:
        sys.exit("outcome 只能是 {}".format("／".join(VALID)))
    row = {"date": datetime.datetime.now().isoformat(timespec="seconds"),
           "query": args.query, "recommended": args.recommended,
           "outcome": args.outcome}
    if args.note:
        row["note"] = args.note
    with open(args.file, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("✅ 已记录：{} → {}（{}）".format(args.query[:40], args.recommended, args.outcome))


def stats(args):
    rows = []
    if os.path.exists(args.file):
        with open(args.file, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    if args.last:
        rows = rows[-args.last:]
    if not rows:
        print("暂无数据——推荐后在真实使用里记一笔：record --query ... --recommended ... --outcome accepted")
        return 0
    n = len(rows)
    acc = sum(1 for r in rows if r["outcome"] == "accepted")
    rej = sum(1 for r in rows if r["outcome"] == "rejected")
    unu = sum(1 for r in rows if r["outcome"] == "unused")
    invalid = rej + unu
    print("== 推荐结果统计（最近 {} 条）==".format(n))
    print("采纳 {}（{:.0%}）｜拒绝 {}（{:.0%}）｜未用 {}（{:.0%}）".format(
        acc, acc / n, rej, rej / n, unu, unu / n))
    print("**无效推荐率（拒绝+未用）：{:.0%}**".format(invalid / n))
    by_skill = {}
    for r in rows:
        d = by_skill.setdefault(r.get("recommended", "?"), Counter())
        d[r["outcome"]] += 1
    print("\n按技能拆分：")
    print("| 技能 | 采纳 | 拒绝 | 未用 | 采纳率 |")
    print("|---|---|---|---|---|")
    for skill, c in sorted(by_skill.items(), key=lambda kv: -sum(kv[1].values())):
        t = c["accepted"] + c["rejected"] + c["unused"]
        print("| {} | {} | {} | {} | {:.0%} |".format(
            skill, c["accepted"], c["rejected"], c["unused"], c["accepted"] / t))
    print("\n读法（视频原则）：采纳率是主曲线，无效推荐率是副曲线——"
          "调整路由（术语映射/权重/停用词）后两条都要看；"
          "推荐质量的改动必须过 run_eval.py 评估集。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="推荐结果回收与采纳率统计")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("record", help="记录一条推荐结果")
    p1.add_argument("--query", required=True)
    p1.add_argument("--recommended", required=True)
    p1.add_argument("--outcome", required=True, help="accepted/rejected/unused")
    p1.add_argument("--note", default="")
    p1.add_argument("--file", default=DEFAULT_FILE)
    p1.set_defaults(func=record)
    p2 = sub.add_parser("stats", help="采纳率统计")
    p2.add_argument("--last", type=int, default=0, help="只统计最近 N 条")
    p2.add_argument("--file", default=DEFAULT_FILE)
    p2.set_defaults(func=stats)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
