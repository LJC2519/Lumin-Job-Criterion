#!/usr/bin/env python3
"""LJC · 市场路：三个技能市场并联检索（SkillHub + skills.sh + ClawHub）。

来源说明（2026-10-29 实测）：
  skillhub   —— 官方 find-skills 的 SkillHub API，**有 description**，可做关键词重叠重排
  skills.sh  —— Vercel/Algolia，find-skills 的 Step2b 兜底源，库存大（OCR 类、中文法律类
                在 SkillHub 完全没有），但**只返回 name + installs**
  clawhub    —— OpenClaw 注册表，搜索结果里嵌 native.skill，**有 summary + topics**
                （上一版误判为「无描述」，2026-10-29 修正）

为什么必须分源分区（而不是混排）：
  只有 skills.sh 真的没有描述文本，关键词重叠只能命中 name，中文查询几乎必然零重叠。
  把它和有描述的源混排，会把它的**源内相关度排序**（Algolia 自己的算法）误判成
  「零重叠噪音」而沉底——那正是 find-skills 能找到、LJC 却看不见的东西。
  所以：有描述的源（SkillHub / ClawHub）走重叠分桶；skills.sh 保留源内次序，单列「外部源」区。

比 find-skills 多做的四件事（2026-10-29，v2.6.0）：
  A. --categories / --category  按分类浏览（find-skills 文档写了但只给 curl）
  B. --hot                      热门榜 /api/skills/top
  C. --detail                   单技能详情：摘要、发布方认证、是否需 API key、
                                **第三方安全报告**（keen / sanbu 的 benign 判定）——可直接喂给安全门
  D. skills.sh 条目也能读原文：经 GitHub tree 定位 SKILL.md → contents API 取正文

修掉的 find-skills 三个坑（2026-10-29）：
  1. score 区分度失效（实测全在 0.04~0.10，"<0.05 丢弃"等于不过滤）→ 重叠优先重排
  2. 高下载无关技能霸榜 → 零重叠降级，不进主卡
  3. 不知道本地装没装 → 已装同名单独标出，指向 check_updates.py
  4. 文档过时：分类实际 13 个（文档写 7 个）、分类键名也不同（dev-programming ≠ developer-tools）；
     Step2b 的 npx 命令在本机 SIGTERM，故直连 REST 端点

纪律不变：**只查不装**。本脚本不发安装请求、不写 skills 目录，只给命令。

用法：
  python3 scripts/market_search.py "OCR 扫描件表格提取"          # 默认三源并联
  python3 scripts/market_search.py "ocr" --source skills.sh      # 只查一个源
  python3 scripts/market_search.py "法条" --source all --top 6
  python3 scripts/market_search.py --categories                  # 列全部分类
  python3 scripts/market_search.py --category professional --top 10 --page 1
  python3 scripts/market_search.py --hot 12                      # 热门榜
  python3 scripts/market_search.py --detail <slug>               # 读技能详情（含安全报告）
  python3 scripts/market_search.py --detail clawhub:<slug>
  python3 scripts/market_search.py --detail skills.sh:<owner>/<repo>/<skill>
  python3 scripts/market_search.py --check                       # 验全部端点可达性
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
# 有描述文本、可做关键词重叠判定的源（ClawHub 有 native.skill.summary，2026-10-29 实测）
DESC_SOURCES = ("skillhub", "clawhub")


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

def _skillhub_row(r):
    """SkillHub 一条记录 → 统一 item。搜索 / 分类浏览 / 热门榜的字段集略有差异，都从这里过。"""
    labels = r.get("labels") or {}
    pub = r.get("publisher") or {}
    subs = r.get("subCategories") or []
    return {
        "src": "skillhub",
        "slug": r.get("slug") or r.get("id") or "",
        "name": r.get("displayName") or r.get("name") or r.get("slug") or "",
        "desc": r.get("description_zh") or r.get("description") or "",
        "desc_en": r.get("description") or "",
        "score": float(r.get("score") or 0.0),
        "downloads": r.get("downloads") or 0,
        "installs": r.get("installs") or 0,
        "stars": r.get("stars") or 0,
        "homepage": r.get("homepage") or "",
        "install_cmd": "",
        "version": r.get("version") or "",
        # 以下三项是 find-skills 不展示、但对「敢不敢用」关键的判据
        "publisher": pub.get("name") or "",
        "publisher_verified": bool(pub.get("verified")),
        "requires_api_key": str(labels.get("requires_api_key")).lower() == "true",
        "category": r.get("category") or "",
        "subcats": [s.get("name") for s in subs if isinstance(s, dict) and s.get("name")],
    }


def parse_skillhub(payload):
    """兼容 results / data / skills 三种外壳（搜索结果与 data.skills 两种信封都实测过）。"""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results") or payload.get("data") or payload.get("skills") or []
    if isinstance(raw, dict):
        raw = raw.get("results") or raw.get("skills") or raw.get("items") or []
    return [_skillhub_row(r) for r in raw if isinstance(r, dict)]


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
    """ClawHub：{results:[{canonicalUrl, displayName, downloads, install:{reference}, native:{skill:{...}}}]}

    **修正（2026-10-29）**：上一版把 ClawHub 当「无描述源」，实际 native.skill 里就有
    summary + topics + categories + stats，还带 isSuspicious 风险标。所以 ClawHub 应当
    与 SkillHub 同列「有描述源」，参与重叠打分——它原本的源内次序不必再单独保护。
    """
    if not isinstance(payload, dict):
        return []
    out = []
    for r in payload.get("results") or []:
        if not isinstance(r, dict):
            continue
        ref = (r.get("install") or {}).get("reference") or r.get("canonicalUrl") or ""
        canon = (r.get("links") or {}).get("canonical") or r.get("canonicalUrl") or ""
        nat = ((r.get("native") or {}).get("skill") or {})
        stats = nat.get("stats") or {}
        topics = nat.get("topics") or []
        summary = nat.get("summary") or r.get("summary") or ""
        out.append({
            "src": "clawhub",
            "slug": ref,
            "name": r.get("displayName") or nat.get("displayName") or ref,
            "desc": summary,
            "desc_en": summary,
            "score": 0.0,
            "downloads": r.get("downloads") or stats.get("downloads") or 0,
            "installs": (r.get("metrics") or {}).get("rolling60DayInstalls") or stats.get("installs") or 0,
            "homepage": ("https://clawhub.com" + canon) if canon.startswith("/") else canon,
            "install_cmd": f"npx clawhub install {ref} --workdir ~ --dir .workbuddy/skills" if ref else "",
            "version": "",
            "topics": topics,
            "categories": nat.get("categories") or [],
            "suspicious": bool(nat.get("isSuspicious")),
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


# ---------- SkillHub 附加端点（find-skills 文档里有、LJC 之前没有）----------

HUB = "https://lightmake.site"


def _meta_cached(key, refresh, fetcher, ttl=CACHE_TTL):
    """非检索类端点的缓存（分类表、分类浏览、热门榜、详情都走这里）。"""
    cp = cache_path("meta", key, 0)
    if not refresh and os.path.exists(cp):
        try:
            if time.time() - os.path.getmtime(cp) < ttl:
                with open(cp, encoding="utf-8") as f:
                    return json.load(f), None, True
        except (OSError, json.JSONDecodeError):
            pass
    data, err = fetcher()
    if err:
        return None, err, False
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass
    return data, None, False


def fetch_categories(refresh=False):
    """GET /api/v1/categories —— 实测 13 个（find-skills 文档写 7 个，已过时）。"""
    def go():
        payload, err = _get_json(f"{HUB}/api/v1/categories")
        if err:
            return None, err
        items = []
        for r in (payload or {}).get("items") or []:
            if isinstance(r, dict) and r.get("key"):
                items.append({"key": r["key"], "name": r.get("name") or r["key"],
                              "name_en": r.get("nameEn") or "", "level": r.get("level") or 1})
        return items, None
    return _meta_cached("categories", refresh, go, ttl=24 * 3600)


def fetch_browse(category, page=1, page_size=10, sort="score", refresh=False):
    """GET /api/skills?category=<key>&sortBy=score&order=desc&page=N&pageSize=N

    实测：分类过滤真实生效（返回条目的 category 全等于所查 key）；
    分页参数生效；返回信封是 {code:0,data:{skills:[]}}。
    """
    def go():
        q = urllib.parse.urlencode({"category": category, "sortBy": sort, "order": "desc",
                                    "page": page, "pageSize": page_size})
        payload, err = _get_json(f"{HUB}/api/skills?{q}")
        if err:
            return None, err
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            return None, f"API 返回 code={payload.get('code')} {payload.get('message') or ''}"
        return parse_skillhub(payload), None
    return _meta_cached(f"browse|{category}|{page}|{page_size}|{sort}", refresh, go)


def fetch_hot(limit=50, refresh=False):
    """GET /api/skills/top —— 实测返回全站热门（一次给 50 条）。"""
    def go():
        payload, err = _get_json(f"{HUB}/api/skills/top")
        if err:
            return None, err
        return parse_skillhub(payload), None
    data, err, cached = _meta_cached("hot", refresh, go)
    if err:
        return None, err, cached
    return (data or [])[:limit], None, cached


# ---------- 单技能详情 ----------

def _brief(text, n=400):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:n] + ("…" if len(t) > n else "")


def detail_skillhub(slug, refresh=False):
    """GET /api/v1/skills/<slug> —— 摘要 + 发布方认证 + 是否需 API key + 第三方安全报告。"""
    def go():
        payload, err = _get_json(f"{HUB}/api/v1/skills/{urllib.parse.quote(slug)}")
        if err:
            return None, err
        if not isinstance(payload, dict) or not payload.get("skill"):
            return None, f"无该技能（slug={slug}）"
        return payload, None
    return _meta_cached(f"detail|skillhub|{slug}", refresh, go)


def detail_clawhub(slug, refresh=False):
    """GET /api/v1/skills/<slug> —— 同名多发布方时 API 返 409，需带 owner（owner/slug）。"""
    def go():
        payload, err = _get_json(f"https://clawhub.com/api/v1/skills/{urllib.parse.quote(slug)}")
        if err:
            return None, err
        if not isinstance(payload, dict) or not payload.get("skill"):
            return None, f"无该技能（slug={slug}）；同名多条时请写 owner/slug"
        return payload, None
    return _meta_cached(f"detail|clawhub|{slug}", refresh, go)


def _gh_tree(repo, branch="main", refresh=False):
    """GitHub tree 一次性拉全仓库路径（实测 382 条 / 110KB，缓存 6h 很划算）。"""
    def go():
        payload, err = _get_json(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1", timeout=25)
        if err:
            return None, err
        if not isinstance(payload, dict) or not payload.get("tree"):
            return None, f"tree 为空（repo={repo} branch={branch}）"
        return [x["path"] for x in payload["tree"] if isinstance(x, dict) and x.get("path")], None
    return _meta_cached(f"ghtree|{repo}|{branch}", refresh, go)


def detail_skillsh(sid, refresh=False):
    """skills.sh 条目读原文：id 形如 owner/repo/skill → 定位 <skill>/SKILL.md → contents API 取正文。

    实测：skills.sh 页面是 SPA 空壳（meta description 是站点通用文案，不含技能内容），
    且无详情 API；但 id 里的 owner/repo 能回 GitHub。raw.githubusercontent 在本机被 SSL
    阻断（000），api.github.com 正常（200），所以走 contents API 而非 raw。
    """
    parts = sid.split("/")
    if len(parts) < 3:
        return None, f"skills.sh id 需形如 owner/repo/skill，收到：{sid}"
    repo, skill = "/".join(parts[:2]), parts[-1]
    paths, err, _ = _gh_tree(repo, refresh=refresh)
    if err:
        return None, f"定位仓库失败：{err}"
    cand = [p for p in paths if p.lower().endswith(f"{skill.lower()}/skill.md")]
    if not cand:
        cand = [p for p in paths if skill.lower() in p.lower() and p.lower().endswith("skill.md")]
    if not cand:
        return None, f"仓库 {repo} 里找不到 {skill}/SKILL.md"
    path = cand[0]

    def go():
        payload, err = _get_json(f"https://api.github.com/repos/{repo}/contents/{path}", timeout=25)
        if err:
            return None, err
        if not isinstance(payload, dict) or "content" not in payload:
            return None, "contents API 无 content 字段"
        import base64
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", "ignore"), None
        except Exception as e:  # noqa: BLE001
            return None, f"解码失败：{e}"
    return _meta_cached(f"detail|skillsh|{repo}|{path}", refresh, go)



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


def _as_src_set(v):
    """把 "skillhub" / ("skillhub","clawhub") / None 统一成集合或 None。"""
    if v is None:
        return None
    return {v} if isinstance(v, str) else set(v)


def split_buckets(items, inst, only_src=None):
    """已装且相关 / 未装·关键词相关 / 未装·仅语义。

    only_src 可以是单个源名或源名集合，只有这些源的条目进 hit/sem——无描述的源
    （skills.sh）重叠不可信，另行分区。已装但零重叠的直接丢弃：既不是新发现也不相关。
    """
    srcs = _as_src_set(only_src)
    have, hit, sem = [], [], []
    for it in items:
        mine = srcs is None or it.get("src") in srcs
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

    不能纯按重叠排——skills.sh 无描述，中文查询几乎必然零重叠；
    也不能纯按源内序——中文查询打 Algolia 会返回热门无关项（实测 lark-sheets 82 万安装
    压在前面）。折中：重叠>0 的先出，零重叠的按源内序跟上，库存一个不丢。
    """
    skip = _as_src_set(skip_src) or set()
    have_keys = {(frozenset(_keys(i)), i.get("src")) for i in have}
    ext = [i for i in items
           if i.get("src") not in skip and (frozenset(_keys(i)), i.get("src")) not in have_keys]
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
    # 敢不敢用的判据：发布方是否认证、是否要 API key、源是否打了可疑标
    if it.get("publisher_verified"):
        bits.append("✓认证发布方")
    if it.get("requires_api_key"):
        bits.append("需API key")
    if it.get("suspicious"):
        bits.append("⚠源标可疑")
    lines = [head, "    " + " · ".join(bits)]
    desc = (it.get("desc") or "").replace("\n", " ")[:110]
    if desc:
        lines.append(f"    {desc}")
    else:
        lines.append("    （该源无描述——须 --detail 或打开链接读原文再判断，不可凭名字推荐）")
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
    have, hit, sem = split_buckets(pool, inst, only_src=DESC_SOURCES)
    hit_strong = [i for i in hit if i["_ov"] >= 2]
    hit_weak = [i for i in hit if i["_ov"] == 1]
    ext = external_items(pool, have, skip_src=DESC_SOURCES)

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
          "skills.sh 无描述，重叠命中的提前、其余保留源内次序。要看原文用 --detail。")

    if have:
        print("\n── 本地已有同名技能（别重复装，要更新跑 check_updates.py）──")
        for i, it in enumerate(have[:top], 1):
            print(fmt_row(i, it, tag="[装]"))
    if hit_strong:
        print("\n── 未装 · 强命中（有描述源，重叠≥2）──")
        for i, it in enumerate(hit_strong[:top], 1):
            print(fmt_row(i, it))
    if ext:
        print("\n── 未装 · 外部源命中（skills.sh，无描述；重叠可能来自扩展词，靠名字自辨）──")
        for i, it in enumerate(ext[:top], 1):
            print(fmt_row(i, it))
    if hit_weak:
        print("\n── 未装 · 弱命中（有描述源，仅 1 个词对上，常是巧合，权重低）──")
        for i, it in enumerate(hit_weak[:min(top, 4)], 1):
            print(fmt_row(i, it))
    if sem:
        print("\n── 未装 · 仅语义候选（有描述源但零关键词重叠，多为下载量霸榜项，可先略过）──")
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


# ---------- 子命令：分类浏览 / 热门榜 / 详情 ----------

def cmd_categories(as_json=False, refresh=False):
    items, err, cached = fetch_categories(refresh)
    if err:
        print(f"✗ 取分类失败：{err}")
        print("  备援：curl -s https://lightmake.site/api/v1/categories")
        return 1
    if as_json:
        print(json.dumps({"ok": True, "cached": cached, "categories": items},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"LJC · SkillHub 分类（{len(items)} 个{'，缓存' if cached else ''}）")
    for c in items:
        en = f" / {c['name_en']}" if c.get("name_en") else ""
        print(f"  {c['key']:<24} {c['name']}{en}")
    print("\n 用法：market_search.py --category <key> --top 10 --page 1")
    return 0


def cmd_browse(category, top=10, page=1, as_json=False, refresh=False):
    items, err, cached = fetch_browse(category, page=page, page_size=top, refresh=refresh)
    if err:
        print(f"✗ 分类「{category}」浏览失败：{err}")
        print("  先跑 --categories 看合法 key（实测 13 个，且与 find-skills 文档所列不同）")
        return 1
    if as_json:
        print(json.dumps({"ok": True, "category": category, "page": page, "cached": cached,
                          "items": items[:top]}, ensure_ascii=False, indent=2))
        return 0
    inst = installed_names()
    print(f"LJC · 分类「{category}」第 {page} 页（{len(items)} 条{'，缓存' if cached else ''}）")
    for i, it in enumerate(items[:top], 1):
        it["_ov"] = 0
        print(fmt_row(i, it, tag="[装]" if _keys(it) & inst else ""))
    return 0


def cmd_hot(top=12, as_json=False, refresh=False):
    items, err, cached = fetch_hot(limit=max(top, 20), refresh=refresh)
    if err:
        print(f"✗ 取热门榜失败：{err}")
        print("  备援：curl -s https://lightmake.site/api/skills/top")
        return 1
    if as_json:
        print(json.dumps({"ok": True, "cached": cached, "items": items[:top]},
                         ensure_ascii=False, indent=2))
        return 0
    inst = installed_names()
    print(f"LJC · SkillHub 热门榜（前 {min(top, len(items))}，{'缓存' if cached else '实时'}）")
    for i, it in enumerate(items[:top], 1):
        it["_ov"] = 0
        print(fmt_row(i, it, tag="[装]" if _keys(it) & inst else ""))
    print("\n 热门 ≠ 对口。榜单按下载量，与你手上任务的匹配度无关——只用来摸底库存。")
    return 0


def _print_security(reports):
    """SkillHub 详情里的第三方安全报告（find-skills 不展示，但对「敢不敢装」很关键）。"""
    if not reports:
        print("  安全报告：无（该技能未送检）")
        return
    for name, r in reports.items():
        if not isinstance(r, dict):
            continue
        print(f"  安全报告·{name}：{r.get('statusText') or r.get('status')} "
              f"（{r.get('status')}）")
        if r.get("reportUrl"):
            print(f"    {r['reportUrl']}")


def parse_detail_spec(spec):
    """把 --detail 的值拆成 (源, 标识)。无前缀默认 SkillHub。"""
    spec = (spec or "").strip()
    src, _, key = spec.partition(":")
    if not key:
        return "skillhub", spec
    src = src.strip().lower()
    alias = {"hub": "skillhub", "claw": "clawhub", "skillsh": "skills.sh"}
    return alias.get(src, src), key.strip()


def cmd_detail(spec, as_json=False, refresh=False):
    """spec: "<slug>"（默认 SkillHub）| "clawhub:<slug>" | "skills.sh:<owner>/<repo>/<skill>"
    """
    src, key = parse_detail_spec(spec)
    if not key:
        print("缺技能标识。例：--detail xiaolv-article-search / --detail clawhub:steipete/weather")
        return 2

    if src in ("skillhub", "hub"):
        payload, err, cached = detail_skillhub(key, refresh)
        if err:
            print(f"✗ SkillHub 详情失败：{err}")
            return 1
        sk = payload.get("skill") or {}
        if as_json:
            print(json.dumps({"ok": True, "src": "skillhub", "cached": cached, "detail": payload},
                             ensure_ascii=False, indent=2))
            return 0
        print(f"LJC · 技能详情 · SkillHub「{sk.get('displayName') or key}」")
        print(f"  slug      {sk.get('slug')}")
        if (payload.get("namespace") or {}).get("canonicalName"):
            print(f"  全名      {payload['namespace']['canonicalName']}")
        print(f"  分类      {sk.get('category') or '-'}"
              + (f"（{', '.join(x.get('name','') for x in sk.get('subCategories') or [] if isinstance(x, dict))}）"
                 if sk.get("subCategories") else ""))
        print(f"  版本      {(payload.get('latestVersion') or {}).get('version') or '-'}")
        pub = payload.get("publisher") or {}
        print(f"  发布方    {pub.get('name') or '-'}"
              + ("  ✓已认证" if pub.get("verified") else "  ⚠未认证")
              + (f"（{pub.get('certifiedName')}）" if pub.get("certifiedName") else ""))
        print(f"  需API key {'是' if str((sk.get('labels') or {}).get('requires_api_key')).lower() == 'true' else '否'}")
        st = sk.get("stats") or {}
        print(f"  数据      下载 {st.get('downloads', 0)} · 安装 {st.get('installs', 0)} ·"
              f" 星标 {st.get('stars', 0)} · 评论 {st.get('comments', 0)}")
        print(f"  摘要      {_brief(sk.get('summary') or sk.get('description'))}")
        if sk.get("homepage") or sk.get("sourceUrl"):
            print(f"  主页      {sk.get('homepage') or sk.get('sourceUrl')}")
        _print_security(payload.get("securityReports"))
        print(f"  安装（需你明确同意，装前先跑 security_check.py）：\n"
              f"    curl -L -o /tmp/ljc-{sk.get('slug')}.zip "
              f"\"https://lightmake.site/api/v1/download?slug={sk.get('slug')}\"")
        return 0

    if src in ("clawhub", "claw"):
        payload, err, cached = detail_clawhub(key, refresh)
        if err:
            print(f"✗ ClawHub 详情失败：{err}")
            return 1
        sk = payload.get("skill") or {}
        if as_json:
            print(json.dumps({"ok": True, "src": "clawhub", "cached": cached, "detail": payload},
                             ensure_ascii=False, indent=2))
            return 0
        print(f"LJC · 技能详情 · ClawHub「{sk.get('displayName') or key}」")
        print(f"  slug      {sk.get('slug')}")
        print(f"  版本      {(payload.get('latestVersion') or {}).get('version') or '-'}")
        print(f"  分类      {', '.join(sk.get('categories') or []) or '-'}")
        print(f"  标签      {', '.join(sk.get('topics') or []) or '-'}")
        st = sk.get("stats") or {}
        print(f"  数据      下载 {st.get('downloads', 0)} · 安装 {st.get('installs', 0)} ·"
              f" 星标 {st.get('stars', 0)}")
        mod = payload.get("moderation") or {}
        if mod:
            verdict = mod.get("verdict") or "?"
            flag = "✓" if verdict == "clean" else "⚠"
            print(f"  {flag}源审查    verdict={verdict}"
                  + ("，已判恶意拦截" if mod.get("isMalwareBlocked") else "")
                  + (f"，引擎 {mod.get('engineVersion')}" if mod.get("engineVersion") else ""))
        if sk.get("isSuspicious"):
            print("  ⚠ 源标为可疑（isSuspicious）——默认不推荐，须先人工读原文")
        print(f"  摘要      {_brief(sk.get('summary'))}")
        owner = (payload.get("owner") or {}).get("handle") or ""
        print(f"  页面      https://clawhub.com/{owner + '/skills/' if owner else ''}{sk.get('slug') or key}")
        print(f"  安装（需你明确同意，装前先跑 security_check.py）：\n"
              f"    npx clawhub install {key} --workdir ~ --dir .workbuddy/skills")
        return 0

    if src in ("skills.sh", "skillsh"):
        text, err, cached = detail_skillsh(key, refresh)
        if err:
            print(f"✗ skills.sh 读原文失败：{err}")
            print(f"  退路：直接开 https://www.skills.sh/s/{key}（页面是 SPA，需人眼读）")
            return 1
        if as_json:
            print(json.dumps({"ok": True, "src": "skills.sh", "id": key,
                              "cached": cached, "content": text}, ensure_ascii=False, indent=2))
            return 0
        body = text.split("---", 2)
        if len(body) == 3 and not body[0].strip():
            fm, main = body[1], body[2]
        else:
            fm, main = "", text
        print(f"LJC · 技能详情 · skills.sh「{key}」（原文来自 GitHub，{len(text)} 字符）")
        if fm:
            for line in fm.strip().split("\n")[:12]:
                if ":" in line and not line.startswith((" ", "-")):
                    k, _, v = line.partition(":")
                    print(f"  {k.strip():<12}{v.strip()[:100]}")
        print("  ── 正文前 900 字符 ──")
        print("  " + _brief(main.replace("\r", ""), 900))
        print(f"\n  安装（需你明确同意）：npx skills add {key} -g -y")
        return 0

    print(f"未知源：{src}（可选 skillhub / clawhub / skills.sh）")
    return 2



def check():
    print("LJC · 市场路端点自检")
    probes = {
        "skillhub": "https://lightmake.site/api/v1/search?q=pdf&limit=1",
        "skills.sh": "https://skills.sh/api/search?q=pdf",
        "clawhub": "https://clawhub.com/api/v1/search?q=pdf&limit=1",
    }
    ok = 0
    print(" 〔检索三源〕")
    for s in SOURCES:
        payload, err = _get_json(probes[s])
        if err:
            print(f"  ✗ {SRC_LABEL[s]}：{err}")
            continue
        n = len(payload.get("skills") or payload.get("results") or payload.get("data") or [])
        print(f"  ✓ {SRC_LABEL[s]}：可达，样本 {n} 条")
        ok += 1

    print(" 〔SkillHub 附加端点〕")
    extra = {
        "分类表 /api/v1/categories": f"{HUB}/api/v1/categories",
        "热门榜 /api/skills/top": f"{HUB}/api/skills/top",
        "分类浏览 /api/skills?category=": f"{HUB}/api/skills?category=office-efficiency&pageSize=1",
    }
    for label, url in extra.items():
        payload, err = _get_json(url)
        if err:
            print(f"  ✗ {label}：{err}")
            continue
        n = len((payload or {}).get("items") or ((payload or {}).get("data") or {}).get("skills") or [])
        print(f"  ✓ {label}：可达，样本 {n} 条")
        ok += 1

    print(" 〔详情链路〕")
    payload, err = _get_json(f"{HUB}/api/v1/skills/dev-god")
    if err:
        print(f"  ✗ SkillHub 详情 /api/v1/skills/<slug>：{err}")
    else:
        print(f"  ✓ SkillHub 详情：可达（含 securityReports）")
        ok += 1
    payload, err = _get_json("https://api.github.com/rate_limit")
    if err:
        print(f"  ✗ GitHub API（skills.sh 读原文依赖它）：{err}")
    else:
        print("  ✓ GitHub API：可达（raw.githubusercontent 本机被 SSL 阻断，故走 contents API）")
        ok += 1

    print(f" 缓存：{CACHE_DIR}（TTL {CACHE_TTL // 3600}h）")
    if ok == 0:
        print("  → 全不可用：改用本地索引 + GitHub 两路，缺口如实写。")
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
    if not argv or "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    as_json = "--json" in argv
    refresh = "--refresh" in argv
    argv = [a for a in argv if a not in ("--json", "--refresh")]

    if "--check" in argv:
        return check()

    def take(flag):
        """取出 flag 及其值。值可选：紧跟的非 -- 开头 token 才算值，缺省返回空串。"""
        if flag not in argv:
            return None
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            val = argv[i + 1]
            del argv[i:i + 2]
            return val
        del argv[i]
        return ""

    def as_int(raw, default):
        return int(raw) if (raw or "").isdigit() else default

    # ---- 子命令优先（互不叠加，避免与检索参数抢 --top）----
    if "--categories" in argv or "--cats" in argv:
        return cmd_categories(as_json=as_json, refresh=refresh)

    cat = take("--category")
    if cat is not None:
        if not cat:
            print("--category 需要一个 key；先跑 --categories 看合法值")
            return 2
        page_raw, top_raw = take("--page"), take("--top")
        return cmd_browse(cat, top=as_int(top_raw, 10), page=as_int(page_raw, 1),
                          as_json=as_json, refresh=refresh)

    hot = take("--hot")
    if hot is not None:
        top_raw = take("--top")
        n = as_int(top_raw, as_int(hot, 12))
        return cmd_hot(top=n, as_json=as_json, refresh=refresh)

    det = take("--detail")
    if det is not None:
        if not det:
            print("--detail 需要技能标识（slug，或 clawhub:<slug> / skills.sh:<owner>/<repo>/<skill>）")
            return 2
        return cmd_detail(det, as_json=as_json, refresh=refresh)

    # ---- 检索 ----
    expand_raw, src_raw, top_raw = take("--expand"), take("--source"), take("--top")
    expand = as_int(expand_raw, 2)
    top = as_int(top_raw, 8)
    sources = SOURCES
    if src_raw:
        chosen = _parse_sources(src_raw)
        if not chosen:
            print(f"未知源：{src_raw}（可选 skillhub / skills.sh / clawhub / all）")
            return 2
        sources = chosen
    query = " ".join(a for a in argv if not a.startswith("--"))
    if not query:
        print("缺查询词。例：python3 scripts/market_search.py \"OCR 表格提取\" --top 8")
        return 2
    return run(query, top=top, expand=expand, refresh=refresh, as_json=as_json, sources=sources)


if __name__ == "__main__":
    sys.exit(main())
