#!/usr/bin/env python3
"""LJC 检索评估集 —— 一条可复跑的尺子。

用法：
    python3 eval/run_eval.py                           # 跑一遍：逐条 + 汇总
    python3 eval/run_eval.py --kind oral               # 只看某一类
    python3 eval/run_eval.py --json                    # 机器可读
    python3 eval/run_eval.py --save baseline.json      # 存基线（改任何东西之前必做）
    python3 eval/run_eval.py --compare baseline.json   # 与基线对比（改完之后必做）

方法论来源：anthropics/skills 官方 skill-creator 的评估章节。
本项目没有安装该技能（它是 Proprietary 许可），只吸收它的方法并保持自己纯标准库零依赖：

  1. 断言要客观可机检；主观的交给人工评审，不硬写断言
  2. 基线必须「改前存、改后比」：with_skill vs old_skill，**同一轮**才有可比性
  3. analyst pass（本脚本 --compare 的「无区分度」一栏）：
     两次都通过的用例不能证明任何改进；两次都失败的才是真欠账
  4. 评估集只能按「人判断的正确答案」填，不能按「当前工具返回什么」填
"""
import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import find  # noqa: E402

QUERIES = os.path.join(HERE, "queries.json")
_TOPK = 10  # 参与 recall 判定的结果条数


def _has_backend_here():
    return any(importlib.util.find_spec(m) for m in
               ("sentence_transformers", "fastembed", "openai"))


def ensure_interpreter():
    """当前解释器没有嵌入库、机器别处有 → 换解释器重跑自己（同 find.py 的机制）。"""
    if os.environ.get("LJC_NO_REEXEC") or os.environ.get("LJC_SEMANTIC") == "off":
        return
    if _has_backend_here():
        return
    py = find.find_semantic_python()
    if py and os.path.isfile(py) and py != sys.executable:
        os.environ["LJC_NO_REEXEC"] = "1"
        try:
            os.execve(py, [py] + sys.argv, os.environ)
        except OSError:
            pass


def run_case(index, case, top=_TOPK):
    """跑一条用例，返回可机检的结果字典。"""
    q = case["query"]
    t0 = time.time()
    terms = find.expand_query(q)
    sem = find.semantic_scores(index["skills"], terms)
    bag = [find.score_entry(e, terms) for e in index["skills"]]
    n_bag = sum(1 for s, _ in bag if s > 0)
    backend = None
    vec = []
    if find.needs_vector(n_bag):
        backend = find.resolve_semantic_backend()
        if backend:
            vec = find.vector_scores(index, find.semantic_query_text(q, terms), backend)
    elapsed = time.time() - t0
    scored, rescued, _hidden = find.partition_results(index, terms, vec, sem=sem)
    ranked = [e["name"] for _, _, e in scored] + [e["name"] for _, _, e in rescued]
    accept = case.get("accept") or []
    must_not = case.get("must_not") or []
    top1 = ranked[0] if ranked else None
    top3 = ranked[:3]
    bad = [n for n in must_not if n in ranked[:_TOPK]]
    return {
        "id": case["id"], "query": q, "kind": case.get("kind", "?"),
        "top1": top1, "top3": top3,
        "hit1": bool(top1 and top1 in accept),
        "hit3": bool(accept and any(n in top3 for n in accept)),
        "recall": bool(accept and any(n in ranked[:_TOPK] for n in accept)),
        "violated": bad,
        "passed": bool(top1 and top1 in accept) and not bad,
        "elapsed_s": round(elapsed, 3),
        "mode": find.retrieval_mode(vec, backend, n_bag),
        "accept": accept,
        "must_not": must_not,
    }


def summarize(rows):
    n = len(rows)
    if not n:
        return {"cases": 0}
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"n": 0, "hit1": 0, "hit3": 0, "recall": 0})
        k["n"] += 1
        k["hit1"] += int(r["hit1"])
        k["hit3"] += int(r["hit3"])
        k["recall"] += int(r["recall"])
    return {
        "cases": n,
        "hit1": sum(r["hit1"] for r in rows),
        "hit3": sum(r["hit3"] for r in rows),
        "recall": sum(r["recall"] for r in rows),
        "passed": sum(r["passed"] for r in rows),
        "violated": sum(1 for r in rows if r["violated"]),
        "avg_s": round(sum(r["elapsed_s"] for r in rows) / n, 3),
        "by_kind": by_kind,
    }


def print_report(rows, title="LJC 检索评估"):
    s = summarize(rows)
    print(f"{title} · {s['cases']} 条用例")
    print(f"{'':>3} {'用例':<34} {'top1':<30} h@1 h@3 耗时")
    print("─" * 84)
    for r in rows:
        q = r["query"][:32] + ("…" if len(r["query"]) > 32 else "")
        t1 = (r["top1"] or "—")[:30]
        m1 = "✓" if r["hit1"] else ("✗" if not r["violated"] else "!")
        m3 = "✓" if r["hit3"] else "·"
        tag = ""
        if r["violated"]:
            tag = f"  ← 命中了不该出现的 {r['violated'][0]}"
        print(f"{r['id']:>3} {q:<34} {t1:<30} {m1:^3} {m3:^3} "
              f"{find.fmt_elapsed(r['elapsed_s'])}{tag}")
    print("─" * 84)
    print(f"top1 命中 {s['hit1']}/{s['cases']}    top3 命中 {s['hit3']}/{s['cases']}    "
          f"recall {s['recall']}/{s['cases']}    平均 {find.fmt_elapsed(s['avg_s'])}")
    print(f"违规（命中已知错误答案）{s['violated']} 条")
    print("\n按类别：")
    for k, v in sorted(s["by_kind"].items()):
        print(f"  {k:<6} {v['n']:>2} 条   h@1 {v['hit1']}/{v['n']}   "
              f"h@3 {v['hit3']}/{v['n']}   recall {v['recall']}/{v['n']}")
    fails = [r for r in rows if not r["passed"]]
    if fails:
        print(f"\n失败/存疑 {len(fails)} 条（这些才是评估集的价值所在）：")
        for r in fails:
            print(f"  #{r['id']} {r['query']}")
            print(f"      期望 {r['accept'] or '（空）'}  实得 top1={r['top1']}")
    return s


def apply_judgments(rows, judgments):
    """宿主 rerank 判分回流（纯函数）：judgments = {"<case_id>": {"skill": 0–1 分}}。
    只对给了判分的用例生效：按判分重排 top1/top3，<0.2（「排除」档）不进 top3。
    宿主只能判脚本递给它的候选——脚本没送对的进来，宿主也救不了，这正是要度量的。"""
    out = []
    for r in rows:
        j = judgments.get(str(r["id"])) or judgments.get(r["id"])
        if not j:
            out.append(r)
            continue
        ranked = [n for n, _s in sorted(j.items(), key=lambda kv: (-kv[1], kv[0]))]
        top1 = ranked[0] if ranked else None
        top1_score = j.get(top1, 0.0) if top1 else 0.0
        top3 = [n for n in ranked[:3] if j[n] >= 0.2]
        accept = r.get("accept") or []
        must_not = r.get("must_not") or []
        # 违规语义（rerank 层）：宿主主动背书（判 ≥0.5）或选为 top1 的已知错误答案
        # 才算误导——被宿主明确压到下层（<0.5）不算，那正是 rerank 在做的事
        bad = [n for n in must_not if j.get(n, 0) >= 0.5 or n == top1]
        nr = dict(r)
        nr.update({
            "top1": top1, "top3": top3,
            "hit1": bool(top1 and top1 in accept and top1_score >= 0.5),
            "hit3": bool(accept and any(n in top3 for n in accept)),
            "recall": bool(accept and any(n in ranked[:_TOPK] for n in accept)),
            "violated": bad,
            "passed": bool(top1 and top1 in accept and top1_score >= 0.5) and not bad,
            "top1_score": top1_score,
            "reranked": True,
        })
        out.append(nr)
    return out


def compare_rows(rows, base_rows):
    """纯函数：把两次结果分成四栏。
    「无区分度」一栏是重点——两次都通过的用例不能证明任何改进，
    把它们算进「改进」就是自欺（skill-creator 的 analyst pass 这一步）。"""
    cur = {r["id"]: r for r in rows}
    base = {r["id"]: r for r in base_rows}
    better = [i for i in cur if cur[i]["passed"] and not base.get(i, {}).get("passed")]
    worse = [i for i in cur if not cur[i]["passed"] and base.get(i, {}).get("passed")]
    both_pass = [i for i in cur if cur[i]["passed"] and base.get(i, {}).get("passed")]
    both_fail = [i for i in cur if not cur[i]["passed"] and not base.get(i, {}).get("passed")]
    return {"better": sorted(better), "worse": sorted(worse),
            "both_pass": sorted(both_pass), "both_fail": sorted(both_fail)}


def print_compare(rows, base_rows):
    cur = {r["id"]: r for r in rows}
    base = {r["id"]: r for r in base_rows}
    res = compare_rows(rows, base_rows)
    better, worse = res["better"], res["worse"]
    both_pass, both_fail = res["both_pass"], res["both_fail"]
    print(f"\n与基线对比（{len(cur)} 条）")
    print(f"  改进 {len(better)} 条：{better}")
    print(f"  退化 {len(worse)} 条：{worse}")
    for i in worse:
        print(f"      #{i} {cur[i]['query']}  基线 top1={base[i]['top1']} → "
              f"现在 top1={cur[i]['top1']}")
    print(f"  无区分度（两次都通过）{len(both_pass)} 条 —— 这些不能证明任何改进")
    print(f"  两次都失败 {len(both_fail)} 条 —— 真欠账：{both_fail}")
    return res


def main():
    ap = argparse.ArgumentParser(description="LJC 检索评估集")
    ap.add_argument("--kind", help="只跑某一类（zh / en / oral）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", metavar="PATH", help="把本次结果存为基线")
    ap.add_argument("--compare", metavar="PATH", help="与基线对比")
    ap.add_argument("--rerank", metavar="FILE",
                    help="宿主判分文件 {case_id: {skill: 0-1}}，回流后单独报告「rerank 层」成绩")
    args = ap.parse_args()

    ensure_interpreter()
    cases = json.load(open(QUERIES, encoding="utf-8"))["cases"]
    if args.kind:
        cases = [c for c in cases if c.get("kind") == args.kind]
        if not cases:
            print(f"没有 kind={args.kind} 的用例")
            return 1
    index = find.load_index()
    rows = [run_case(index, c) for c in cases]

    if args.json:
        print(json.dumps({"summary": summarize(rows), "rows": rows},
                         ensure_ascii=False, indent=1))
    else:
        print_report(rows)

    if args.rerank:
        judgments = json.load(open(args.rerank, encoding="utf-8"))
        rrows = apply_judgments(rows, judgments)
        n_j = sum(1 for r in rrows if r.get("reranked"))
        rs = summarize(rrows)
        print(f"\n── rerank 层（宿主模型读原文判「能力匹配」，{n_j} 条已判）──")
        print(f"top1 {rs['hit1']}/{rs['cases']}（脚本层 {summarize(rows)['hit1']}/{rs['cases']}）   "
              f"recall {rs['recall']}/{rs['cases']}   违规 {rs['violated']}")
        for r in rrows:
            if r.get("reranked"):
                base_hit = "✓" if [x for x in rows if x["id"] == r["id"]][0]["hit1"] else "✗"
                print(f"  #{r['id']} {r['query'][:30]}: 脚本 {base_hit} → rerank "
                      f"{'✓' if r['hit1'] else '✗'}（top1={r['top1']}）")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows},
                      f, ensure_ascii=False, indent=1)
        print(f"\n基线已存：{args.save}（改代码之前存，改完 --compare 才有意义）")
    if args.compare:
        try:
            base = json.load(open(args.compare, encoding="utf-8"))
        except Exception as e:
            print(f"读基线失败：{e}")
            return 1
        print_compare(rows, base["rows"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
