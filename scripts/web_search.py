#!/usr/bin/env python3
"""lumin-job-criterion（LJC）· 全网语义搜索通道：用 Tavily 在开放互联网上按任务找技能。

为什么需要这一路：
  本地索引只覆盖「已装的」，GitHub 只覆盖「开源公开的」，SkillHub/SkillsMP 是特定市场。
  用户一句「帮我找个能 XXX 的技能」里，XXX 可能对应一个还没进任何目录、刚发布在
  GitHub 或个人博客上的新技能——只有全网语义搜索能捞到。

Tavily 是 AI 原生语义搜索引擎：服务端已经把查询和网页做语义匹配并排好序，
返回的结果自带 relevance score，所以我们**不必自己再跑向量**（和 find.py 内置
TF-IDF 词袋是互补的两回事）。配合 SKILL.md「语义意译扩展」里宿主模型的意译，
中文任务也能捞到英文技能。

用法：
  python3 web_search.py "法律文书 信息抽取"
  python3 web_search.py "pdf to word" --limit 8
  python3 web_search.py "contract review" --json
  python3 web_search.py --check            # 只检查 TAVILY_API_KEY 在不在

环境变量：
  TAVILY_API_KEY   必填。没有就明说并给出注册地址，不假装搜过
  LJC_CACHE_DIR    结果缓存位置（默认脚本旁 .cache/，TTL 6 小时，省配额）
  LJC_WEB_DEPTH    basic|advanced（默认 advanced，更准更慢、2 credits/次）

设计纪律（与 github_search.py 一致）：
  - 纯标准库（urllib），clone 即用
  - 网络不可达 / 没 key / 超限 → 明确报错并给路，不假装搜过
  - 无 key 时该通道自动跳过，不影响本地 + GitHub 两路
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TAVILY_ENDPOINT = "https://api.tavily.com/search"
TIMEOUT = 25
UA = "lumin-job-criterion"
CACHE_TTL = 6 * 3600  # 6 小时：网页结果新鲜度优先，但省 Tavily 免费额度


def api_key():
    return os.environ.get("TAVILY_API_KEY") or os.environ.get("TAVILY_KEY") or ""


def cache_dir():
    d = os.environ.get("LJC_CACHE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
    os.makedirs(d, exist_ok=True)
    return d


# ── 轻量缓存（省配额；网页新鲜度优先故 TTL 短）──
def _cache_path(task):
    h = hashlib.sha1(task.encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir(), "web_" + h + ".json")


def _load_cache(task, refresh):
    if refresh:
        return None
    p = _cache_path(task)
    try:
        if os.path.isfile(p):
            meta = json.load(open(p, encoding="utf-8"))
            if time.time() - meta.get("fetched_at", 0) < CACHE_TTL:
                return meta.get("candidates")
    except Exception:
        pass
    return None


def _save_cache(task, candidates):
    try:
        json.dump({"fetched_at": time.time(), "candidates": candidates},
                  open(_cache_path(task), "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass  # 缓存写失败不致命


def tavily_search(query, key, max_results=10, search_depth="advanced", include_domains=None):
    """调 Tavily /search。返回 (ok, list_of_raw_results, error_str)。纯函数可测。"""
    body = {
        "query": query,
        "search_depth": search_depth,
        "max_results": min(max_results, 20),
        "topic": "general",
        "include_answer": False,
    }
    if include_domains:
        body["include_domains"] = include_domains
        body["include_domains_mode"] = "prefer"  # 优先但不屏蔽全网
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_ENDPOINT, data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        if e.code == 401:
            return False, [], "Tavily 鉴权失败（401）：TAVILY_API_KEY 无效或缺失。"
        if e.code == 429:
            return False, [], "Tavily 触发限额（429）：免费档每月额度有限，稍后再试或升级。"
        return False, [], f"Tavily HTTP {e.code}: {msg}"
    except Exception as e:
        return False, [], f"网络错误: {e}"
    return True, (payload.get("results") or []), ""


def build_queries(task):
    """把任务变成 1–3 条面向「找技能」的自然语言查询。
    复用 find.expand_query 的术语映射把中文翻成英文；Tavily 是语义引擎，
    给自然语言句子比给关键词列表效果更好。纯函数可测。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from find import expand_query
        terms = [t for t in expand_query(task) if len(t) >= 2]
    except Exception:
        terms = [task]
    en_terms = [t for t in terms if t.isascii() and " " not in t][:3]
    queries = [f"{task} agent skill"]
    if en_terms:
        queries.append(" ".join(en_terms) + " claude code skill")
    # 去重保序
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def classify_url(url):
    """返回 (category, owner_repo_or_None, display_name)。纯函数可测。"""
    m = re.search(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", url)
    if m:
        repo = m.group(1).split("/tree/")[0].split("/blob/")[0].split("/pull/")[0].rstrip("/")
        name = repo.split("/")[-1]
        return "GitHub 仓库", repo, name
    host = (re.search(r"https?://([^/]+)/", url) or [None, ""])[1].lower()
    if any(host == d or host.endswith("." + d) for d in
           ("skillsmp.com", "lightmake.site", "anthropic.com", "claude.com")):
        return "技能市场", None, host
    return "网页", None, host


def extract_candidates(raw_results):
    """把 Tavily 返回的 results 规整成与 github_search 同构的候选。纯函数可测。"""
    out = []
    for r in raw_results:
        url = r.get("url", "")
        title = (r.get("title") or "").strip()
        content = re.sub(r"\s+", " ", (r.get("content") or "")).strip()
        score = float(r.get("score") or 0.0)
        category, repo, name = classify_url(url)
        disp = name or title or url
        desc = title
        if content:
            snippet = content[:160]
            desc = (title + " — " + snippet) if title else snippet
        out.append({
            "name": disp, "url": url, "desc": desc, "category": category,
            "repo": repo, "score": score,
        })
    return out


def search_web(task, limit=10, search_depth=None, refresh=False, as_json=False,
               include_domains=None):
    """全网语义搜索主流程：检查 key → 多路查询 → 合并去重排序 → 展示/返回。"""
    key = api_key()
    if not key:
        if as_json:
            print(json.dumps({"enabled": False, "reason": "no_api_key", "candidates": []},
                             ensure_ascii=False))
        else:
            print("✗ 未设 TAVILY_API_KEY——全网语义搜索通道跳过。")
            print("  注册免费：https://app.tavily.com  设好环境变量再跑本命令即可启用：")
            print("  export TAVILY_API_KEY=tvly-xxxxxxxx")
        return []
    depth = search_depth or os.environ.get("LJC_WEB_DEPTH") or "advanced"
    cached = _load_cache(task, refresh)
    if cached is not None:
        print(f"LJC · 全网语义搜索（缓存，TTL 6h）「{task}」→ {len(cached)} 条候选\n")
        cands = cached
    else:
        queries = build_queries(task)
        print(f"LJC · 全网语义搜索（Tavily · {depth}）「{task}」→ {len(queries)} 路查询：")
        merged = {}
        ok_any = False
        for q in queries:
            print(f"  · {q}")
            ok, raw, err = tavily_search(q, key, max_results=min(limit, 10),
                                         search_depth=depth, include_domains=include_domains)
            if not ok:
                print(f"    ✗ {err}")
                continue
            ok_any = True
            for c in extract_candidates(raw):
                # 同 URL 取最高分（多路查询可能命中同一页）
                if c["url"] in merged:
                    if c["score"] > merged[c["url"]]["score"]:
                        merged[c["url"]] = c
                else:
                    merged[c["url"]] = c
        if not ok_any:
            print("  （全部查询失败，检查网络 / API key / 配额）")
            return []
        cands = sorted(merged.values(), key=lambda x: -x["score"])
        _save_cache(task, cands)
    cands = cands[:limit]
    if as_json:
        print(json.dumps({"enabled": True, "candidates": cands}, ensure_ascii=False, indent=1))
        return cands
    print(f"合并去重后 {len(cands)} 条候选（按语义相关度排序）：\n")
    for i, c in enumerate(cands, 1):
        desc = c["desc"]
        if len(desc) > 140:
            desc = desc[:140] + "…"
        repo_note = f"  (repo:{c['repo']})" if c["repo"] else ""
        print(f" W{i}. [{c['name']}]  (来源:{c['category']}  语义分:{c['score']:.2f}{repo_note})")
        print(f"     {desc}")
        print(f"     {c['url']}\n")
    return cands


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--check":
        if api_key():
            print("✓ TAVILY_API_KEY 已设置，全网语义搜索可用。")
        else:
            print("✗ 未设置 TAVILY_API_KEY，全网语义搜索将跳过。")
            print("  注册：https://app.tavily.com  然后 export TAVILY_API_KEY=tvly-...")
        return
    limit, depth, refresh, as_json = 10, None, False, False
    domains, query_parts = None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--limit":
            i += 1
            try:
                limit = int(args[i])
            except (IndexError, ValueError):
                pass
        elif a == "--depth":
            i += 1
            depth = args[i] if i < len(args) else None
        elif a == "--domains":
            i += 1
            domains = [x.strip() for x in args[i].split(",")] if i < len(args) else None
        elif a == "--refresh":
            refresh = True
        elif a == "--json":
            as_json = True
        else:
            query_parts.append(a)
        i += 1
    task = " ".join(query_parts).strip()
    if not task:
        print('用法: web_search.py "<任务描述>" [--limit N] [--depth basic|advanced] '
              '[--domains a.com,b.com] [--refresh] [--json]')
        return
    search_web(task, limit=limit, search_depth=depth, refresh=refresh,
               as_json=as_json, include_domains=domains)


if __name__ == "__main__":
    main()
