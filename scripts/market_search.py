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

三层自动扩展（v2.7.0）—— 目的是「脚本自己想到术语表外的词」，像 find-skills 那样：
  1 表    expand_terms()      静态中英术语表，0 网络成本，准但不全
  2 译    expand_by_translate 整句机翻（MyMemory，免 key）。这是解决「表外词」的根本：
          实测「投资准入负面清单」「多语言翻译」在术语表里扩出空集，强命中直接塌成 0
  3 反馈  expand_by_feedback  伪相关反馈 PRF：从首轮结果的 name/slug 里挖「库存自己用的说法」
  级联策略：首轮跑「表」，并发拿翻译；只有前 5 名里重叠≥2 的不足 3 个时才追加第二轮，
  常见查询不为冷门场景付延迟代价。三层的词都会并入 tokens 参与重叠打分
  （只有搜、不参与打分的话，外部源永远零重叠，又被安装量霸榜项顶到前面）。

纪律不变：**只查不装**。本脚本不发安装请求、不写 skills 目录，只给命令。

用法：
  python3 scripts/market_search.py "OCR 扫描件表格提取"          # 默认三源 + 自动三层扩展
  python3 scripts/market_search.py "法条" --no-expand            # 只打原句，不扩词（排错用）
  python3 scripts/market_search.py "法条" --expand 4             # 每层最多 4 个词；0 = 关闭
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


# 每源单次请求超时（秒）。实测 2026-10-29：SkillHub ~0.5s、skills.sh ~1.0s，
# ClawHub 2.5~6.8s 且对中文查询常返 0 条 —— 它是唯一延迟瓶颈，单独收紧到 5s。
# 超时即降级为该源无结果，其余源照常（三源本就并联、互不依赖）。
SRC_TIMEOUT = {"skillhub": 10, "skills.sh": 10, "clawhub": 4}


def fetch_skillhub(query, limit):
    url = f"https://lightmake.site/api/v1/search?q={urllib.parse.quote(query)}&limit={limit}"
    payload, err = _get_json(url, timeout=SRC_TIMEOUT["skillhub"])
    if err:
        return [], err
    return parse_skillhub(payload)[:limit], None


def fetch_skillsh(query, limit):
    url = f"https://skills.sh/api/search?q={urllib.parse.quote(query)}"
    payload, err = _get_json(url, timeout=SRC_TIMEOUT["skills.sh"])
    if err:
        return [], err
    return parse_skillsh(payload)[:limit], None


def fetch_clawhub(query, limit):
    url = f"https://clawhub.com/api/v1/search?q={urllib.parse.quote(query)}&limit={limit}"
    payload, err = _get_json(url, timeout=SRC_TIMEOUT["clawhub"])
    if err:
        return [], f"{err}（该源超时降级，结果不含 ClawHub）"
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


def detail_clawhub(spec, refresh=False):
    """GET /api/v1/skills/<slug>[?owner=<handle>]

    实测三件事（2026-10-29）：
      ① 裸 slug 同名多条时返回 **409 AMBIGUOUS_SKILL_SLUG**（例：weather 有多个发布方）
      ② 指定 owner 的正确写法是**查询参数** `?owner=steipete`；
         `/skills/<owner>/<slug>` 与 `/skills/<owner>%2F<slug>` 都是 404
      ③ 不歧义时裸 slug 直接 200
    """
    owner, bare = "", spec
    if "/" in spec:
        owner, _, bare = spec.rpartition("/")

    def go():
        url = f"https://clawhub.com/api/v1/skills/{urllib.parse.quote(bare)}"
        if owner:
            url += "?" + urllib.parse.urlencode({"owner": owner})
        payload, err = _get_json(url)
        if err:
            if "409" in err and not owner:
                return None, (f"slug「{bare}」有多个发布方，须写明 owner：\n"
                              f"    --detail clawhub:<owner>/{bare}")
            return None, err
        if not isinstance(payload, dict) or not payload.get("skill"):
            return None, f"无该技能（{spec}）"
        return payload, None
    return _meta_cached(f"detail|clawhub|{spec}", refresh, go)


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
    """第 1 层扩展：复用 find.py 的中→英术语表，失败则退化为空。"""
    try:
        import find  # noqa: WPS433
        return [t for t in find.expand_query(query) if t and t.lower() != query.lower()]
    except Exception:
        return []


# ---------------------------------------------------------------- 自动扩展
# 问题：静态术语表永远有表外的词。2026-10-29 实测——「投资准入负面清单」与
# 「多语言翻译」扩出空集，导致这两类查询的强命中直接塌成 0。find-skills 靠
# 模型当场想英文词，不受表限制；要追平，脚本必须在运行时自己产词。
# 故扩成三层，逐层兜底：
#   1 表    —— 静态术语表，0 成本，准但不全
#   2 译    —— 机器翻译整句，解决「表外词」这一根本问题
#   3 反馈  —— 伪相关反馈（PRF）：从首轮结果的描述里挖出「库存自己用的说法」
# 三层的词全部并入 tokens 参与重叠打分（只搜不算 = 外部源零重叠被安装量霸榜）。

EN_STOP = set("""
a an the of for and or to in on with by from is are was were as at into that this it its
be been being do does did has have had will would can could should may might must not no
all any each some such than then there their our your you we they he she who whom what how
why via per vs etc also more most less new make made get got set see look like just only
very much many few own same other another both either neither
skill skills tool tools plugin plugins agent agents use used using usage support supports
supporting based enable enabled allow allows help helps provide provides including include
includes file files folder project projects data text content work works working way ways
""".split())


def has_cn(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


# 泛词：可以扩召回，但不能进重叠打分。它们在任何描述里都出现，
# 进了 tokens 就会把完全无关的高下载技能顶成「强命中」。
GENERIC_EN = set("""
access list lists data file files text content document documents page pages
system systems service services tool tools skill skills platform feature features
function functions option options value values item items result results
""".split())


def en_words(text, min_len=4):
    """抽英文词：小写、去停用词、去短词。

    min_len=4 是刻意的：overlap() 用子串匹配，3 字母词（pdf/doc/api）误命中太狠，
    会把无关技能顶上来。宁可少扩几个词，也不能把主卡搅浑。
    """
    out = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#]{2,}", text or ""):
        w = w.lower()
        if len(w) >= min_len and w not in EN_STOP:
            out.add(w)
    return out


def expand_by_translate(query, refresh=False):
    """第 2 层：整句机翻中→英（MyMemory，免 key、免额度申请）。

    实测（2026-10-29，本机网络）：
      google-gtx 超时 / libretranslate SSL 握手超时 / bing-edge 502 —— 均不可用
      mymemory 200，1.3~2.2s，「投资准入负面清单 → Negative list for investment
      access」「语音转文字 → Speech To Text」，8 条测试全对
    译文缓存 30 天：术语稳定，且能省匿名免费额度。
    返回 (词列表, 整句译文, 状态串)。任何失败都只降级、不抛错。
    """
    if not has_cn(query):
        return [], "", "skip（查询无中文，无需翻译）"

    def go():
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=zh-CN|en"
               % urllib.parse.quote(query))
        payload, err = _get_json(url, timeout=8)
        if err:
            return None, err
        if not isinstance(payload, dict):
            return None, "返回非 JSON"
        if payload.get("quotaFinished"):
            return None, "免费额度用尽"
        text = ((payload.get("responseData") or {}).get("translatedText") or "").strip()
        if not text or "MYMEMORY WARNING" in text.upper():
            return None, "译文为空或限流提示"
        return {"text": text}, None

    payload, err, _ = _meta_cached("tr|" + query, refresh, go, ttl=30 * 86400)
    if err or not payload:
        return [], "", f"不可用（{err}）"
    text = payload["text"]
    # min_len=3：检索词袋必须留住 ocr/pdf/asr 这类核心缩写。
    # 打分侧另有 ≥6 与 GENERIC_EN 两道闸，不会因为这里放宽而污染排序。
    return sorted(en_words(text, min_len=3)), text, "ok"


def expand_by_feedback(items, tokens, top_n=20, min_df=2, cap=6):
    """第 3 层：伪相关反馈 PRF —— 从首轮结果里挖「库存自己用的说法」。

    这层不依赖任何外部服务，而且比翻译更贴近库存：翻译给的是「正确的英文」，
    PRF 给的是「这批技能实际写的英文」，后者才可能命中标题。

    三个过滤（都是 2026-10-29 实测撞出来的）：
      1 df 侧 min_len=4 —— 3 字母以下的杂音太多；但 ocr/asr/pdf 这类缩写必须
        靠标题信号救回来（见第 3 条），它们是核心技术词，砍掉就白扩了
      2 df ≤ top_n*0.45 —— 出现太多次的是套话（"when to use this skill"），没有区分度
      3 **必须在 ≥2 条结果的 name/slug 里出现过** —— 最狠也最有效的一条：
        描述可以套模板，标题不会。模板词（find/when/token）永远不会出现在标题里，
        而 ocr/asr 会。所以这里放行 min_len=3。
    """
    df, in_title = {}, {}
    for it in items[:top_n]:
        blob = " ".join([it.get("name") or "", it.get("desc") or "",
                         it.get("desc_en") or "", it.get("slug") or ""])
        for w in en_words(blob, min_len=4):
            df[w] = df.get(w, 0) + 1
        for w in en_words(f"{it.get('name') or ''} {it.get('slug') or ''}", min_len=3):
            in_title[w] = in_title.get(w, 0) + 1
    known = {t.lower() for t in tokens}
    ceil = max(min_df, int(top_n * 0.45))
    cand = {w: c for w, c in df.items()
            if min_df <= c <= ceil and w not in known and in_title.get(w, 0) >= 2}
    # 3 字母缩写（ocr / asr / pdf）过不了 df 的 min_len=4，但它们同时出现在多条
    # 标题里就是最强的主题信号 —— 直接补进候选，否则「OCR 扫描件表格提取」这类
    # 查询永远学不到 ocr 这个词（实测：词袋里丢了 ocr，召回全是 syncfusion 之类）。
    for w, c in in_title.items():
        if c >= 2 and w not in known and w not in cand:
            cand[w] = c
    ordered = sorted(cand.items(), key=lambda x: (-x[1], x[0]))
    return [w for w, _ in ordered[:cap]]


def rescue_needed(pool, min_good=3):
    """前 5 名里有几个真相关（重叠≥2）？不足 min_good 就该补救。

    实测校准：法条检索 5/5、ppt 演示文稿 4/5 → 不必补救；
    OCR 扫描件表格提取 0/5、投资准入负面清单 0/5 → 必须补救。
    """
    head = pool[:5]
    return sum(1 for i in head if i.get("_ov", 0) >= 2) < min_good


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


def run(query, top=8, expand=None, refresh=False, as_json=False, sources=SOURCES):
    """expand=None → 自动三层扩展；expand=0 → 关闭；expand=N → 每层最多 N 个词。

    扩展词必须同时进 tokens——只拿去搜索、不拿去打分的话，中文查询在无描述的
    外部源里永远零重叠，又会被安装量霸榜项顶到前面（实测「法条」→ 52 万安装的无关项）。
    """
    cap = 8 if expand is None else max(0, expand)
    auto = cap > 0
    tokens = query_tokens(query)
    pool, errs, cached = [], [], False
    layers = {"table": [], "translate": [], "feedback": []}
    tr_text, tr_words, tr_stat = "", [], ("off" if not auto else "未发起")
    rescued = False

    def absorb(items, via):
        for it in items:
            it["_via"] = via
            pool.append(it)

    # 第 1 层：静态术语表（纯本地，0 网络成本）
    if auto:
        layers["table"] = [t for t in expand_terms(query) if re.search(r"[a-zA-Z]", t)][:cap]
    tokens |= {w.lower() for w in layers["table"]}
    table_bag = " ".join(layers["table"])

    # ── 首轮三路并发：原句检索 / 表词袋检索 / 整句翻译。翻译与检索互不依赖，
    #    串行会把每次查询的延迟凭空拉长 1.5s。
    with ThreadPoolExecutor(max_workers=3) as ex:
        jobs = {"main": ex.submit(search_all, query, sources, 20, refresh)}
        if table_bag:
            # 词袋只打英文池（skills.sh / ClawHub）：中文原句在 SkillHub 的语义检索
            # 本来就比一串拼起来的英文词准，重复打一次既慢又拉低质量。
            bag_src = tuple(s for s in sources if s != "skillhub") or sources
            jobs["bag"] = ex.submit(search_all, table_bag, bag_src, 20, refresh)
        if auto:
            jobs["tr"] = ex.submit(expand_by_translate, query, refresh)
        for name, fut in jobs.items():
            res = fut.result()
            if name == "tr":
                tr_words, tr_text, tr_stat = res
                continue
            items, e, cached_any = res
            cached = cached or cached_any
            errs += e
            absorb(items, query if name == "main" else table_bag)

    if errs and not pool:
        msg = "；".join(errs)
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print("LJC · 市场路（三源并联）\n  ✗ 全部通道失败：" + msg)
            print("  备援：curl 手打 SkillHub，或加载官方 find-skills。")
        return 1

    pool = rerank(dedupe(pool), tokens)

    # ── 第二轮（按需补救）：首轮前 5 名里真相关不足 3 个，说明术语表没覆盖住，
    #    用「整句译文 + 译文词 + PRF 词」再打一轮。库存够用时不多花一分钱。
    if auto and rescue_needed(pool):
        layers["translate"] = [w for w in tr_words if w not in {t.lower() for t in tokens}][:cap]
        # PRF 必须「有种子才扩散」：首轮全是噪音时，从噪音里学词只会更偏
        # （实测「投资准入负面清单」首轮混进 WPS Office，PRF 学出 generator/office/suite，
        #   再检索就把整批 Office 技能拉回来 —— 经典 query drift）。
        seeds = [i for i in pool if i.get("_ov", 0) >= 2]
        layers["feedback"] = (expand_by_feedback(seeds, tokens, top_n=10, min_df=2, cap=cap)
                              if len(seeds) >= 2 else [])
        # 只有译文里的「长实词」参与打分；短词与 PRF 词一律只扩召回、不打分。
        # 实测教训：「投资准入负面清单」译出 access/list/investment/negative，其中
        # access 与 list 太泛，进了 tokens 就把「全球文献知识库」「去AI味降重」顶成强命中。
        tokens |= {w.lower() for w in layers["translate"]
                   if len(w) >= 6 and w.lower() not in GENERIC_EN}
        # 原句里的英文词（ocr / asr / pdf 这类缩写）必须留在词袋里。译文会把
        # 「OCR 扫描件表格提取」翻成 "OCR Scan Form Extraction"，但下游抽词若只剩
        # form/scan，打 skills.sh 就召回不到标题里直接写 ocr 的那批技能。
        orig_en = [w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#]{1,}", query)]
        bag2 = " ".join(dict.fromkeys(
            orig_en + layers["translate"] + layers["feedback"]))[:400]
        round2 = [q for q in (tr_text, bag2) if q]
        if round2:
            # 并发：两轮串行会把单次查询拖到 17s（实测），并发的上限只取决于最慢那个源。
            with ThreadPoolExecutor(max_workers=len(round2)) as ex:
                jobs2 = [(q, ex.submit(search_all, q, sources, 20, refresh)) for q in round2]
                for q, fut in jobs2:
                    items, e, c = fut.result()
                    cached = cached or c
                    errs += e
                    absorb(items, q)
            pool = rerank(dedupe(pool), tokens)
            rescued = True

    inst = installed_names()
    have, hit, sem = split_buckets(pool, inst, only_src=DESC_SOURCES)
    hit_strong = [i for i in hit if i["_ov"] >= 2]
    hit_weak = [i for i in hit if i["_ov"] == 1]
    ext = external_items(pool, have, skip_src=DESC_SOURCES)

    if as_json:
        print(json.dumps({
            "ok": True, "query": query, "sources": list(sources),
            "cached": cached, "errors": errs, "rescued": rescued,
            "expanded": {"table": layers["table"], "translate": layers["translate"],
                         "feedback": layers["feedback"], "translate_sentence": tr_text,
                         "translate_status": tr_stat},
            "counts": {"installed": len(have), "keyword_strong": len(hit_strong),
                       "keyword_weak": len(hit_weak), "external": len(ext),
                       "semantic": len(sem)},
            "installed": have[:top], "keyword_strong": hit_strong[:top],
            "keyword_weak": hit_weak[:top], "external": ext[:top], "semantic": sem[:top],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"LJC · 市场路 · 查询「{query}」")
    if auto:
        parts = []
        if layers["table"]:
            parts.append("表{" + ", ".join(layers["table"][:6]) + "}")
        if layers["translate"]:
            parts.append("译{" + ", ".join(layers["translate"][:6]) + "}")
        if layers["feedback"]:
            parts.append("库存反馈{" + ", ".join(layers["feedback"][:6]) + "}")
        print(" 自动扩展：" + ("  ".join(parts) if parts else "无（术语表与翻译均未产出词，按原句检索）"))
        if tr_text:
            tail = "　※ 首轮召回不足，已用它追加检索" if rescued else "　（首轮召回够用，未追加）"
            print(f"   ↳ 整句译文「{tr_text}」{tail}")
        if tr_stat not in ("ok", "off", "skip（查询无中文，无需翻译）"):
            print(f"   ↳ 翻译层：{tr_stat}（已降级，不影响检索）")
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

    print(" 〔自动扩展〕")
    payload, err = _get_json(
        "https://api.mymemory.translated.net/get?q=%E6%B3%95%E6%9D%A1&langpair=zh-CN|en", timeout=8)
    if err:
        print(f"  ✗ 翻译层（MyMemory）：{err} —— 第 2 层扩词不可用，"
              f"仅剩术语表与 PRF，表外词会漏")
    else:
        txt = ((payload or {}).get("responseData") or {}).get("translatedText") or ""
        print(f"  ✓ 翻译层：可达（法条 → {txt}）")
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
    no_expand = "--no-expand" in argv
    argv = [a for a in argv if a != "--no-expand"]
    expand_raw, src_raw, top_raw = take("--expand"), take("--source"), take("--top")
    if no_expand:
        expand = 0
    elif expand_raw is None:
        expand = None          # 默认自动三层（表 → 翻译 → 库存反馈）
    else:
        expand = as_int(expand_raw, 8)   # 显式给定：每层最多 N 个词，0 = 关闭
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
