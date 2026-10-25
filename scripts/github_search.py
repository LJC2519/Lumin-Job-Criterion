#!/usr/bin/env python3
"""lumin-job-criterion（LJC）· GitHub 通道：在 GitHub 上按任务检索技能。

两种模式：
  repo 模式（默认）：GitHub 仓库搜索 API——按关键词扫技能仓库（雷达式扫库）
  catalog 模式：抓取已知技能目录（awesome 列表）→ 本地缓存 → 全文检索

用法：
  python3 github_search.py "法律文书 信息抽取"
  python3 github_search.py "pdf" --catalog
  python3 github_search.py "diagram" --mode both --limit 8

环境变量：
  GITHUB_TOKEN    设置了就用（未认证搜索 10 次/分、contents 60 次/时；
                   认证后 30 次/分、5000 次/时）
  LJC_CACHE_DIR   目录缓存位置（默认脚本旁 .cache/，TTL 7 天）

设计纪律：
  - 纯标准库，任何人 clone 即可跑
  - 网络不可达 / 被限额 → 明确报错并给出路，不假装搜过
  - raw.githubusercontent.com 在部分网络环境不可达，故走
    api.github.com contents 端点（base64）而非 raw
"""
import base64
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20
CACHE_TTL = 7 * 86400
UA = "lumin-job-criterion"

CATALOGS = [
    {"name": "awesome-claude-skills", "repo": "ComposioHQ/awesome-claude-skills", "branch": "master"},
    {"name": "awesome-openclaw-skills", "repo": "VoltAgent/awesome-openclaw-skills", "branch": "main"},
    # 树形目录（官方仓库）：git/trees 一次拿全量路径，再逐个取 SKILL.md frontmatter。
    # 首次抓取有请求数成本（未认证 core 限额 60 次/时），已缓存 7 天；
    # 建议设 GITHUB_TOKEN。--refresh 可强制刷新。
    {"name": "anthropics-skills", "repo": "anthropics/skills", "branch": "main",
     "type": "tree", "skill_glob": "skills/*/SKILL.md", "max_skills": 25},
    {"name": "claude-plugins-official", "repo": "anthropics/claude-plugins-official", "branch": "main",
     "type": "tree", "skill_glob": "**/SKILL.md", "max_skills": 30},
]


def token():
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def cache_dir():
    d = os.environ.get("LJC_CACHE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
    os.makedirs(d, exist_ok=True)
    return d


def api_get(url):
    """GET GitHub API。返回 (ok, data_dict_or_错误信息, 附加信息dict)。"""
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return True, json.loads(resp.read().decode("utf-8")), {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        extra = {}
        if e.code == 403 and "rate limit" in body.lower():
            reset = e.headers.get("X-RateLimit-Reset")
            if reset:
                wait = max(0, int(reset) - int(time.time()))
                extra["retry_after"] = wait
        return False, f"HTTP {e.code}: {body}", extra
    except Exception as e:
        return False, f"网络错误: {e}", {}


def expand_terms(query):
    """复用 find.py 的中英术语映射（中文查询也能搜 GitHub）；导入失败则原样用。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from find import expand_query
        terms = [t for t in expand_query(query) if len(t) >= 2]
        return terms[:6]
    except Exception:
        return [query]


def search_repos(query, limit=8):
    terms = expand_terms(query)
    # GitHub 对中文检索效果差，优先用扩展出的英文词；最多取 3 个 + skill
    en_terms = [t for t in terms if t.isascii() and " " not in t][:3] or terms[:2]
    if en_terms and all(not t.isascii() for t in en_terms):
        print("  ⚠ 检索词非 ASCII——GitHub 搜索对中文效果很差，建议先想好英文关键词")
    q = " ".join(en_terms) + " skill"
    url = ("https://api.github.com/search/repositories?q="
           + urllib.parse.quote(q)
           + f"&sort=stars&order=desc&per_page={limit}")
    print(f"GitHub 仓库搜索「{q}」…")
    ok, data, extra = api_get(url)
    if not ok:
        if extra.get("retry_after") is not None:
            print(f"  ✗ 触发 GitHub 未认证搜索限额（10 次/分），约 {extra['retry_after']} 秒后恢复。")
            print("  → 设置 GITHUB_TOKEN 环境变量可大幅提高限额。")
        else:
            print(f"  ✗ 搜索失败：{data}")
        return []
    items = data.get("items", [])
    if not items:
        print("  （无仓库命中）")
        return []
    print(f"  → 命中 {len(items)} 个仓库（按星排序）：\n")
    for i, r in enumerate(items, 1):
        desc = (r.get("description") or "（无描述）")
        if len(desc) > 110:
            desc = desc[:110] + "…"
        print(f" {i:2d}. [{r['full_name']}]  ★{r.get('stargazers_count', 0)}")
        print(f"     {desc}")
        print(f"     {r.get('html_url')}\n")
    return items


def fetch_catalog(cat, refresh=False):
    """抓取目录 README（contents API + base64），带 7 天 TTL 本地缓存。"""
    cd = cache_dir()
    cache_md = os.path.join(cd, cat["name"] + ".md")
    cache_meta = os.path.join(cd, cat["name"] + ".json")
    if not refresh and os.path.isfile(cache_md) and os.path.isfile(cache_meta):
        try:
            meta = json.load(open(cache_meta, encoding="utf-8"))
            if time.time() - meta.get("fetched_at", 0) < CACHE_TTL:
                return open(cache_md, encoding="utf-8").read(), "cache"
        except Exception:
            pass
    url = (f"https://api.github.com/repos/{cat['repo']}/contents/README.md?ref={cat['branch']}")
    ok, data, extra = api_get(url)
    if not ok or "content" not in data:
        # 网络失败但有旧缓存 → 用旧的并声明
        if os.path.isfile(cache_md):
            return open(cache_md, encoding="utf-8").read(), "stale-cache"
        reason = "GitHub 限额" if extra.get("retry_after") is not None else str(data)[:120]
        return None, reason
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "ignore")
    except Exception:
        return None, "base64 解码失败"
    try:
        with open(cache_md, "w", encoding="utf-8") as f:
            f.write(text)
        json.dump({"fetched_at": time.time(), "repo": cat["repo"], "branch": cat["branch"]},
                  open(cache_meta, "w", encoding="utf-8"))
    except OSError:
        pass  # 缓存写失败不致命
    return text, "fresh"


def parse_frontmatter_lite(text):
    """极简 frontmatter：只取 name/description（树形目录的 fetcher 用）。"""
    if not text.startswith("---"):
        return {"name": "", "description": ""}
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else text[3:2000]
    name = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc = re.search(r"^description:\s*(.+)$", fm, re.M)
    return {"name": (name.group(1).strip().strip("\"'") if name else ""),
            "description": (desc.group(1).strip().strip("\"'") if desc else "")}


def build_tree_entries(paths, fetcher, repo=None, branch="main"):
    """树形目录（官方仓库结构）：SKILL.md 路径 + 注入的 fetcher → 条目列表。
    fetcher(path) -> dict(name=, description=) 或 None。纯函数，可测试。"""
    entries = []
    for p in paths:
        if not p.endswith("SKILL.md"):
            continue
        parts = p.split("/")
        if any(seg.startswith(".") for seg in parts[:-1]):
            continue  # .github 等非技能路径
        meta = fetcher(p)
        if not meta:
            continue
        name = meta.get("name") or parts[-2]
        desc = meta.get("description", "")
        # 分类规则：取最近一层 "skills" 目录的上一级（插件名）；
        # skills 在仓库根（i==0）→ 官方技能；无 skills 层 → 用父目录名。
        if "skills" in parts[:-1]:
            i = len(parts) - 1 - parts[::-1].index("skills")
            category = parts[i - 1] if i >= 1 else "官方技能"
        elif len(parts) >= 3:
            category = parts[-2]
        else:
            category = "（未分类）"
        url = (f"https://github.com/{repo}/tree/{branch}/{'/'.join(parts[:-1])}"
               if repo else "")
        entries.append({"name": name, "url": url, "desc": desc, "category": category})
    return entries


def fetch_tree_catalog(cat, refresh=False):
    """树形目录抓取：git/trees 一次全量路径 → 逐个取 SKILL.md frontmatter（封顶）→ 条目。
    缓存为 entries JSON（7 天 TTL）；抓取失败有旧缓存用旧的并声明。"""
    cd = cache_dir()
    cache_file = os.path.join(cd, cat["name"] + ".entries.json")

    def load_stale():
        try:
            return json.load(open(cache_file, encoding="utf-8"))["entries"]
        except Exception:
            return None

    if not refresh and os.path.isfile(cache_file):
        try:
            meta = json.load(open(cache_file, encoding="utf-8"))
            if time.time() - meta.get("fetched_at", 0) < CACHE_TTL:
                return meta["entries"], "cache"
        except Exception:
            pass
    repo, branch = cat["repo"], cat.get("branch", "main")
    ok, tree, _extra = api_get(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1")
    if not ok or "tree" not in tree:
        stale = load_stale()
        if stale is not None:
            return stale, "stale-cache"
        return None, str(tree)[:100]
    paths = sorted(
        t["path"] for t in tree["tree"]
        if t["type"] == "blob" and fnmatch.fnmatch(t["path"], cat.get("skill_glob", "**/SKILL.md"))
    )
    cap = cat.get("max_skills", 25)
    truncated = len(paths) > cap
    paths = paths[:cap]

    def fetcher(path):
        ok2, c, _extra = api_get(f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}")
        if not ok2 or "content" not in c:
            return None
        try:
            text = base64.b64decode(c["content"]).decode("utf-8", "ignore")
        except Exception:
            return None
        return parse_frontmatter_lite(text)

    entries = build_tree_entries(paths, fetcher, repo=repo, branch=branch)
    try:
        json.dump({"fetched_at": time.time(), "entries": entries, "truncated": truncated},
                  open(cache_file, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass  # 缓存写失败不致命
    return entries, ("fresh-truncated" if truncated else "fresh")


def parse_entries(text):
    """解析 awesome 列表：### 标题=分类，'- [名称](链接) - 描述'=条目。"""
    import re
    entries = []
    category = ""
    toc_headings = {"table of contents", "contents", "目录", "目次",
                    "security notice", "license", "contributing"}
    pat = re.compile(r"^\s*[-*]\s+\[([^\]]+)\]\(([^)]+)\)(?:\s*[-–—:]\s*(.*))?$")
    for line in text.splitlines():
        if line.startswith("#"):
            h = line.lstrip("#").strip()
            if h.lower() not in toc_headings:
                category = h
            continue
        m = pat.match(line)
        if m:
            name, url, desc = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
            if url.startswith("#"):
                continue  # 页内锚点（目录），不是条目
            entries.append({"name": name, "url": url, "desc": desc,
                            "category": category or "（未分类）"})
    return entries


def search_catalog(query, limit=10, refresh=False):
    """四路目录检索：markdown 列表（2 个 awesome）+ 树形目录（2 个官方仓库）。"""
    terms = [t.lower() for t in expand_terms(query) if len(t) >= 2]
    if not terms:
        terms = [query.lower()]
    all_hits = []
    how_notes = {"fresh": "刚抓取", "cache": "缓存(7天内)", "stale-cache": "旧缓存(抓取失败)",
                 "fresh-truncated": "刚抓取(已截断至上限)"}
    for cat in CATALOGS:
        if cat.get("type") == "tree":
            entries, how = fetch_tree_catalog(cat, refresh=refresh)
            if entries is None:
                print(f"── 目录 {cat['name']}：抓取失败（{how}），跳过")
                continue
        else:
            text, how = fetch_catalog(cat, refresh=refresh)
            if text is None:
                print(f"── 目录 {cat['name']}：抓取失败（{how}），跳过")
                continue
            entries = parse_entries(text)
        for e in entries:
            name_l, desc_l, cat_l = e["name"].lower(), e["desc"].lower(), e["category"].lower()
            score = 0
            for t in terms:
                if t in name_l:
                    score += 40
                elif t in desc_l:
                    score += 15
                elif t in cat_l:
                    score += 20
            if score > 0:
                all_hits.append((score, cat["name"], e))
        print(f"── 目录 {cat['name']}（{how_notes.get(how, how)}）：{len(entries)} 个条目，本轮命中累计 {len(all_hits)}")
    if not all_hits:
        print("  （四个技能目录均无命中）")
        return []
    all_hits.sort(key=lambda x: -x[0])
    print(f"\n技能目录检索「{query}」→ 命中 {len(all_hits)} 条（按相关度排序）：\n")
    for i, (score, catname, e) in enumerate(all_hits[:limit], 1):
        desc = e["desc"] or "（无描述）"
        if len(desc) > 100:
            desc = desc[:100] + "…"
        print(f" C{i}. [{e['name']}]  (目录:{catname}  分类:{e['category']}  得分:{score})")
        print(f"     {desc}")
        print(f"     {e['url']}\n")
    return all_hits


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    mode = "repo"
    limit = 8
    refresh = False
    query_parts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--catalog":
            mode = "catalog"
        elif a == "--refresh":
            refresh = True
        elif a == "--mode":
            i += 1
            mode = args[i] if i < len(args) else "repo"
        elif a == "--limit":
            i += 1
            try:
                limit = int(args[i])
            except (IndexError, ValueError):
                pass
        else:
            query_parts.append(a)
        i += 1
    query = " ".join(query_parts).strip()
    if not query:
        print("用法: github_search.py \"关键词\" [--catalog|--mode repo|catalog|both] [--limit N]")
        return
    tok_note = "已用 GITHUB_TOKEN（高限额）" if token() else "未认证（限额较低，可设 GITHUB_TOKEN）"
    print(f"LJC · GitHub 通道（{tok_note}）\n")
    if mode in ("repo", "both"):
        search_repos(query, limit)
        if mode == "both":
            print()
    if mode in ("catalog", "both"):
        search_catalog(query, limit, refresh)


if __name__ == "__main__":
    main()
