#!/usr/bin/env python3
"""LJC · 市场路：三个技能市场并联检索（SkillHub + skills.sh + ClawHub）。

来源说明（2026-10-29 实测）：
  skillhub   —— 官方 find-skills 的 SkillHub API，**有 description**，可做关键词重叠重排
  skills.sh  —— Vercel/Algolia，find-skills 的 Step2b 兜底源，库存大（OCR 类、中文法律类
                在 SkillHub 完全没有），但**只返回 name + installs**
  clawhub    —— OpenClaw 注册表，同为 Step2b 源，返回 displayName + downloads，无描述

为什么必须分源分区（而不是混排）：
  skills.sh / clawhub 没有描述文本，关键词重叠只能命中 name，中文查询几乎必然零重叠。
  把它们和有描述的 SkillHub 混排，会把这些源的**源内相关度排序**（Algolia / ClawHub 自己的
  算法）误判成「零重叠噪音」而沉底——那正是 find-skills 能找到、LJC 却看不见的东西。
  所以：SkillHub 走重叠分桶；另两源保留源内次序，单列「外部源」区。

修掉的 find-skills 三个坑（2026-10-29）：
  1. score 区分度失效（实测全在 0.04~0.10，"<0.05 丢弃"等于不过滤）→ 重叠优先重排
  2. 高下载无关技能霸榜 → 零重叠降级，不进主卡
  3. 不知道本地装没装 → 已装同名单独标出，指向 check_updates.py

纪律不变：**只查不装**。本脚本不发安装请求、不写 skills 目录，只给命令。

用法：
  python3 scripts/market_search.py "OCR 扫描件表格提取"          # 默认三源并联
  python3 scripts/market_search.py "ocr" --source skills.sh      # 只查一个源
  python3 scripts/market_search.py "法条" --source all --top 6
  python3 scripts/market_search.py --check                       # 验三源可达性
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
from concurrent.futures import ThreadPoolExecutor

UA = "LJC-market/2.0 (+https://github.com/)"
CACHE_TTL = 6 * 3600
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(SCRIPT_DIR, ".cache", "market")
DEFAULT_SKILLS_DIR = os.path.expanduser(os.environ.get("LJC_SKILLS_DIR", "~/.workbuddy/skills"))

SOURCES = ("skillhub", "skills.sh", "clawhub")
SRC_LABEL = {"skillhub": "SkillHub", "skills.sh": "skills.sh", "clawhub": "ClawHub"}


# ---------- HTTP ----------

def cache_path(source, query, limit):
    key = hashlib.sha1(f"{source}|{query}|{limit}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, key + ".json")


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, f"{type(e).__name__}: {e}"
    except json.JSONDecodeError as e:
        return None, f"返回不是 JSON：{e}"


def _cached(source, query, limit, refresh, fetcher):
    cp = cache_path(source, query, limit)
    if not refresh and os.path.exists(cp):
        try:
            if time.time() - os.path.getmtime(cp) < CACHE_TTL:
                with open(cp, encoding="utf-8") as f:
                    return json.load(f), None, True
        except (OSError, json.JSONDecodeError):
            pass
    items, err = fetcher(query, limit)
    if err:
        return [], err, False
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except OSError:
        pass
    return items, None, False


# ---------- 三源抓取 ----------

def parse_skillhub(payload):
    """兼容 results / data / skills 三种外壳。"""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results") or payload.get("data") or payload.get("skills") or []
    if isinstance(raw, dict):
        raw = raw.get("results") or raw.get("items") or []
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        out.append({
            "src": "skillhub",
            "slug": r.get("slug") or r.get("id") or "",
            "name": r.get("displayName") or r.get("name") or r.get("slug") or "",
            "desc": r.get("description_zh") or r.get("description") or "",
            "desc_en": r.get("description") or "",
            "score": float(r.get("score") or 0.0),
            "downloads": r.get("downloads") or 0,
            "installs": r.get("installs") or 0,
            "homepage": r.get("homepage") or "",
            "install_cmd": "",
            "version": r.get("version") or "",
        })
    return out


def parse_skillsh(payload):
    """skills.sh：{skills:[{id, source, skillId, name, installs}]}，无描述。"""
    if not isinstance(payload, dict):
        return []
    out = []
    for r in payload.get("skills") or []:
        if not isinstance(r, dict):
            continue
        sid = r.get("id") or ""
        src_repo, skill = r.get("source") or "", r.get("skillId") or r.get("name") or ""
        out.append({
            "src": "skills.sh",
            "slug": sid or f"{src_repo}@{skill}",
            "name": r.get("name") or skill,
            "desc": "",
            "desc_en": "",
            "score": 0.0,
            "downloads": 0,
            "installs": r.get("installs") or 0,
            "homepage": f"https://www.skills.sh/s/{sid}" if sid else "",
            "install_cmd": f"npx skills add {src_repo}@{skill} -g -y" if src_repo and skill else "",
            "version": "",
        })
    return out


def parse_clawhub(payload):
    """ClawHub：{results:[{canonicalUrl, displayName, downloads, install:{reference}}]}，无描述。"""
    if not isinstance(payload, dict):
        return []
    out = []
    for r in payload.get("results") or []:
        if not isinstance(r, dict):
            continue
        ref = (r.get("install") or {}).get("reference") or r.get("canonicalUrl") or ""
        canon = (r.get("links") or {}).get("canonical") or r.get("canonicalUrl") or ""
        out.append({
            "src": "clawhub",
            "slug": ref,
            "name": r.get("displayName") or ref,
            "desc": "",
            "desc_en": "",
            "score": 0.0,
            "downloads": r.get("downloads") or 0,
            "installs": (r.get("metrics") or {}).get("rolling60DayInstalls") or 0,
            "homepage": ("https://clawhub.com" + canon) if canon.startswith("/") else canon,
            "install_cmd": f"npx clawhub install {ref} --workdir ~ --dir .workbuddy/skills" if ref else "",
            "version": "",
        })
    return out


def fetch_skillhub(query, limit):
    url = f"https://lightmake.site/api/v1/search?q={urllib.parse.quote(query)}&limit={limit}"
    payload, err = _get_json(url)
    return ([], err) if err else (parse_skillhub(payload), None)


def fetch_skillsh(query, limit):
    url = f"https://skills.sh/api/search?q={urllib.parse.quote(query)}"
    payload, err = _get_json(url)
    if err:
        return [], err
    return parse_skillsh(payload)[:limit], None


def fetch_clawhub(query, limit):
    url = f"https://clawhub.com/api/v1/search?q={urllib.parse.quote(query)}&limit={limit}"
    payload, err = _get_json(url)
    if err:
        return [], err
    return parse_clawhub(payload)[:limit], None


FETCHERS = {"skillhub": fetch_skillhub, "skills.sh": fetch_skillsh, "clawhub": fetch_clawhub}


def search_source(source, query, limit=20, refresh=False):
    return _cached(source, query, limit, refresh, FETCHERS[source])


def search_all(query, sources=SOURCES, limit=20, refresh=False):
    """三源并发，单源失败不影响其他源。返回 (items, errs, cached_any)。"""
    items, errs, cached_any = [], [], False
    ok = 0
    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as ex:
        futures = {s: ex.submit(search_source, s, query, limit, refresh) for s in sources}
        for src, fut in futures.items():
            res, err, cached = fut.result()
            cached_any = cached_any or cached
            if err:
                errs.append(f"{SRC_LABEL[src]} → {err}")
                continue
            ok += 1
            for i, it in enumerate(res):
                it["_ord"] = i
                items.append(it)
    if ok == 0 and errs:
        return [], errs, False
    return items, errs, cached_any


# ---------- 重排 ----------

def query_tokens(query):
    """英文按词、中文按 2 字滑窗切。零依赖。"""
    toks = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{1,}", query):
        toks.add(w.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(run) == 1:
            toks.add(run)
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    return toks


def expand_terms(query):
    """复用 find.py 的中→英术语扩展，失败则退化为空。"""
    try:
        import find  # noqa: WPS433
        return [t for t in find.expand_query(query) if t and t.lower() != query.lower()]
    except Exception:
        return []


def overlap(item, tokens):
    hay = f"{item.get('slug','')} {item.get('name','')} {item.get('desc','')} {item.get('desc_en','')}".lower()
    return sum(1 for t in tokens if t in hay)


def rerank(items, tokens):
    for it in items:
        it["_ov"] = overlap(it, tokens)
    return sorted(items, key=lambda x: (-x["_ov"], -x["score"], -int(x.get("installs") or 0)))


def dedupe(items):
    """先按 (源, slug) 去重，再按 (源, name) 去重——同名保留安装量最高的那个。

    实测：byted-las-pdf-parse-doubao 在 skills.sh 上有 bytedance/… 与
    skills.volces.com/… 两个发布源，slug 不同但 name 相同，不去会占两行。
    """
    seen, out = set(), []
    for it in items:
        key = (it.get("src", ""), (it.get("slug") or it.get("name") or "").lower())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(it)
    best = {}
    for it in out:
        nk = (it.get("src", ""), (it.get("name") or "").lower())
        cur = best.get(nk)
        if cur is None or int(it.get("installs") or 0) > int(cur.get("installs") or 0):
            best[nk] = it
    keep = {id(v) for v in best.values()}
    return [it for it in out if id(it) in keep]


def installed_names():
    names = set()
    idx = os.path.join(SKILL_ROOT, "index.json")
    try:
        with open(idx, encoding="utf-8") as f:
            data = json.load(f)
        for e in data.get("entries", data if isinstance(data, list) else []):
            if isinstance(e, dict):
                for k in ("name", "dir"):
                    if e.get(k):
                        names.add(str(e[k]).lower())
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    try:
        for d in os.listdir(DEFAULT_SKILLS_DIR):
            if not d.startswith("."):
                names.add(d.lower())
    except OSError:
        pass
    return names


def _keys(it):
    return {str(it.get("slug", "")).lower(), str(it.get("name", "")).lower()} - {""}


def split_buckets(items, inst, only_src=None):
    """已装且相关 / 未装·关键词相关 / 未装·仅语义。

    only_src 非空时 hit/sem 只收该源——外部源无描述、重叠不可信，另行分区。
    已装但零重叠的直接丢弃：它既不是新发现也不相关，留着只搅浑主卡。
    """
    have, hit, sem = [], [], []
    for it in items:
        mine = only_src is None or it.get("src") == only_src
        if _keys(it) & inst:
            if it["_ov"] > 0:
                have.append(it)
        elif it["_ov"] > 0:
            if mine:
                hit.append(it)
        elif mine:
            sem.append(it)
    return have, hit, sem


def external_items(items, have, skip_src="skillhub"):
    """外部源（无描述）独立成区：重叠命中的提前，其余保留源内次序。

    不能纯按重叠排——外部源无描述，中文查询几乎必然零重叠；
    也不能纯按源内序——中文查询打 Algolia 会返回热门无关项（实测 lark-sheets 82 万安装
    压在前面）。折中：重叠>0 的先出，零重叠的按源内序跟上，库存一个不丢。
    """
    have_keys = {(frozenset(_keys(i)), i.get("src")) for i in have}
    ext = [i for i in items
           if i.get("src") != skip_src and (frozenset(_keys(i)), i.get("src")) not in have_keys]
    return sorted(ext, key=lambda x: (-int(x.get("_ov", 0)),
                                      -int(x.get("installs") or 0),
                                      -int(x.get("downloads") or 0),
                                      x.get("src", ""), x.get("_ord", 0)))


# ---------- 输出 ----------

def fmt_row(i, it, tag=""):
    name = it.get("name") or it.get("slug")
    head = f" {tag} {i:>2}. {name}"
    bits = [f"源={SRC_LABEL.get(it.get('src'), '?')}"]
    if it.get("score"):
        bits.append(f"score={it['score']:.3f}")
    bits.append(f"重叠{it.get('_ov', 0)}")
    if it.get("installs"):
        bits.append(f"安装{it['installs']}")
    if it.get("downloads"):
        bits.append(f"下载{it['downloads']}")
    if it.get("slug"):
        bits.append(f"slug={it['slug']}")
    lines = [head, "    " + " · ".join(bits)]
    desc = (it.get("desc") or "").replace("\n", " ")[:110]
    if desc:
        lines.append(f"    {desc}")
    else:
        lines.append("    （该源无描述——须打开链接读原文再判断，不可凭名字推荐）")
    if it.get("homepage"):
        lines.append(f"    {it['homepage']}")
    return "\n".join(lines)


def run(query, top=8, expand=0, refresh=False, as_json=False, sources=SOURCES):
    extra = []
    if expand:
        extra = [t for t in expand_terms(query) if re.search(r"[a-zA-Z]", t)][:expand]
    # 扩展词必须同时进 tokens——只拿去搜索、不拿去打分的话，中文查询在
    # 无描述的外部源里永远零重叠，又会被安装量霸榜项顶到前面（实测「法条」→ 52 万安装的无关项）。
    tokens = query_tokens(query) | {t.lower() for t in extra}
    queries = [query]

    pool, errs, cached = [], [], False
    for q in queries + extra:
        items, e, c = search_all(q, sources=sources, limit=20, refresh=refresh)
        cached = cached or c
        errs += e
        for it in items:
            it["_via"] = q
            pool.append(it)

    if errs and not pool:
        msg = "；".join(errs)
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print("LJC · 市场路（三源并联）\n  ✗ 全部通道失败：" + msg)
            print("  备援：curl 手打 SkillHub，或加载官方 find-skills。")
        return 1

    pool = dedupe(pool)
    pool = rerank(pool, tokens)
    inst = installed_names()
    have, hit, sem = split_buckets(pool, inst, only_src="skillhub")
    hit_strong = [i for i in hit if i["_ov"] >= 2]
    hit_weak = [i for i in hit if i["_ov"] == 1]
    ext = external_items(pool, have)

    if as_json:
        print(json.dumps({
            "ok": True, "query": query, "expanded": extra, "sources": list(sources),
            "cached": cached, "errors": errs,
            "counts": {"installed": len(have), "keyword_strong": len(hit_strong),
                       "keyword_weak": len(hit_weak), "external": len(ext),
                       "semantic": len(sem)},
            "installed": have[:top], "keyword_strong": hit_strong[:top],
            "keyword_weak": hit_weak[:top], "external": ext[:top], "semantic": sem[:top],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"LJC · 市场路 · 查询「{query}」")
    if extra:
        print(" 术语扩展：" + ", ".join(extra))
    print(" 源：" + ", ".join(SRC_LABEL[s] for s in sources)
          + " ｜ " + ("本地缓存（6h）" if cached and not refresh else "实时请求"))
    print(f" 去重后 {len(pool)} 个 · 已装 {len(have)} / 强命中 {len(hit_strong)}"
          f" / 弱命中 {len(hit_weak)} / 外部源 {len(ext)} / 仅语义 {len(sem)}")
    if errs:
        print(" ⚠ 部分源失败：" + "；".join(errs))
    print(" 注：SkillHub 的 score 区分度极低（0.04~0.10），排序按关键词重叠重排；"
          "skills.sh / ClawHub 无描述，重叠命中的提前、其余保留源内次序。")

    if have:
        print("\n── 本地已有同名技能（别重复装，要更新跑 check_updates.py）──")
        for i, it in enumerate(have[:top], 1):
            print(fmt_row(i, it, tag="[装]"))
    if hit_strong:
        print("\n── 未装 · 强命中（SkillHub，重叠≥2）──")
        for i, it in enumerate(hit_strong[:top], 1):
            print(fmt_row(i, it))
    if ext:
        print("\n── 未装 · 外部源命中（skills.sh / ClawHub，无描述；重叠可能来自扩展词，靠名字自辨）──")
        for i, it in enumerate(ext[:top], 1):
            print(fmt_row(i, it))
    if hit_weak:
        print("\n── 未装 · 弱命中（SkillHub，仅 1 个词对上，常是巧合，权重低）──")
        for i, it in enumerate(hit_weak[:min(top, 4)], 1):
            print(fmt_row(i, it))
    if sem:
        print("\n── 未装 · 仅语义候选（SkillHub 零关键词重叠，多为下载量霸榜项，可先略过）──")
        for i, it in enumerate(sem[:min(top, 3)], 1):
            print(fmt_row(i, it))
    if not hit and not ext and not sem:
        print("\n  三个源都没有对口的。这是合法结论——别硬凑，缺口照实写进覆盖图。")

    picks = (hit_strong + ext + hit_weak + sem)[:4]
    if picks:
        print("\n 安装（用户明确同意后才执行，LJC 不代劳；装前必须过安全门）——")
        for it in picks:
            if it.get("install_cmd"):
                print("   " + it["install_cmd"])
            elif it.get("slug"):
                print(f"   curl -L -o /tmp/ljc-{it['slug']}.zip "
                      f"\"https://lightmake.site/api/v1/download?slug={it['slug']}\"")
    return 0


def check():
    print("LJC · 市场路三源自检")
    probes = {
        "skillhub": "https://lightmake.site/api/v1/search?q=pdf&limit=1",
        "skills.sh": "https://skills.sh/api/search?q=pdf",
        "clawhub": "https://clawhub.com/api/v1/search?q=pdf&limit=1",
    }
    ok = 0
    for s in SOURCES:
        payload, err = _get_json(probes[s])
        if err:
            print(f"  ✗ {SRC_LABEL[s]}：{err}")
            continue
        n = len(payload.get("skills") or payload.get("results") or payload.get("data") or [])
        print(f"  ✓ {SRC_LABEL[s]}：可达，样本 {n} 条")
        ok += 1
    print(f" 缓存：{CACHE_DIR}（TTL {CACHE_TTL // 3600}h）")
    if ok == 0:
        print("  → 三源全不可用：改用本地索引 + GitHub 两路，缺口如实写。")
        return 1
    return 0


def _parse_sources(val):
    v = (val or "").strip().lower()
    if v in ("all", ""):
        return SOURCES
    for s in SOURCES:
        if v == s or v == SRC_LABEL[s].lower():
            return (s,)
    return ()


def main():
    argv = sys.argv[1:]
    if "--check" in argv:
        return check()
    if "--help" in argv or "-h" in argv or not argv:
        print(__doc__)
        return 0
    as_json = "--json" in argv
    refresh = "--refresh" in argv
    top, expand, sources = 8, 2, SOURCES
    for flag in ("--top", "--expand", "--source"):
        if flag in argv:
            i = argv.index(flag)
            try:
                val = argv[i + 1]
            except IndexError:
                print(f"{flag} 需要一个值")
                return 2
            argv = argv[:i] + argv[i + 2:]
            if flag == "--top":
                top = int(val) if val.isdigit() else 8
            elif flag == "--expand":
                expand = int(val) if val.isdigit() else 0
            else:
                chosen = _parse_sources(val)
                if not chosen:
                    print(f"未知源：{val}（可选 skillhub / skills.sh / clawhub / all）")
                    return 2
                sources = chosen
    query = " ".join(a for a in argv if not a.startswith("--"))
    if not query:
        print("缺查询词。例：python3 scripts/market_search.py \"OCR 表格提取\" --top 8")
        return 2
    return run(query, top=top, expand=expand, refresh=refresh, as_json=as_json, sources=sources)


if __name__ == "__main__":
    sys.exit(main())
