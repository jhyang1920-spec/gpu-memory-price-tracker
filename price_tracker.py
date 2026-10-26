#!/usr/bin/env python3
"""
GPU 임대가 · 메모리 가격 수집기 (매일 실행)

가격 페이지를 Playwright(Chromium)로 열어 화면 텍스트를 읽고, 정해진 품목의 가격을 뽑는다.
- 날짜는 '그 값이 어느 날짜 값인가' 기준이다.
    메모리(TrendForce): 표의 Last Update 날짜(대만 GMT+8 표기 그대로).
    GPU (모두 미국 동부시각 기준):
      RunPod        페이지의 "Updated <날짜>"
      GetDeploying  "Updated N minutes ago" → 수집 시각에서 N분을 뺀 날짜
      Lambda·CoreWeave  날짜 표시가 없어 수집 날짜. price_changed로 가격이 바뀐 날을 표시한다.
- 값은 price_history.csv 한 파일에 날짜별로 쌓인다. 원자료가 갱신되지 않으면 같은 날짜 행이
  다시 쓰일 뿐 새 행이 생기지 않는다(매일 돌려도 중복 없음).
- 대시보드용 JSON은 주(그 날짜가 속한 주의 월요일) 단위로, 그 주의 가장 최근 값을 담는다.

출력 (--out-dir, 기본 ./price_data):
  gpu_prices.json     [{week, gpu, source, kind, price}]            kind: list | market_low | market_median
  memory_prices.json  [{week, item, category, price, session_change_pct, source_date}]
  price_history.csv   전체 일별 이력 (한 줄 = 수집일 × 품목)
                      date(값의 기준일), week, group, item, category, source, kind, price, change_pct,
                      source_date, unit, collected_at(수집 시각, KST), date_basis(날짜 근거),
                      price_changed(같은 품목 직전 기록 대비: Y 바뀜 / N 같음 / first 첫 기록)
  run_log.json        마지막 실행 결과 (가져온 개수, 못 가져온 항목, 페이지 오류)

사용법:
  pip install playwright && python -m playwright install --with-deps chromium
  python price_tracker.py                 # 오늘 값 수집 → CSV에 추가, 이번 주 JSON 갱신
  python price_tracker.py --dump-text     # 각 페이지 텍스트를 price_data/_debug/에 저장 (파서 점검용)

원칙:
  - 페이지에서 찾지 못한 값은 행을 만들지 않는다(추정·이월 없음). run_log.json의 missing에 남긴다.
  - 같은 기준일·품목 행이 이미 있으면 새 값으로 바뀐다(중복 없음). 다른 날짜 행은 건드리지 않는다.
  - 예전 형식(메모리 행이 수집일로 적힌 것)은 실행할 때 기준일로 자동 정리된다.
  - 같은 주에 다시 돌리면 JSON은 그 주에 새로 가져온 품목의 행만 교체된다(못 가져온 품목은 기존 값 유지).
  - 값이 상식 범위를 벗어나면(파싱 오류 가능성) 버리고 missing에 남긴다.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))
US_ET = ZoneInfo("America/New_York")  # GPU 사이트(미국 업체) 날짜 기준

# ------------------------------------------------------------------ 수집 대상

NUM = r"([\d,]+(?:\.\d+)?)"

# GPU: (source, kind, gpu, 정규식, 그룹 번호, 나눌 GPU 수 그룹 번호 or None)
GETDEPLOYING = "https://getdeploying.com/reference/cloud-gpu"
LAMBDA = "https://lambda.ai/pricing"
RUNPOD = "https://www.runpod.io/pricing"
COREWEAVE = "https://www.coreweave.com/pricing"


def gd(name_re: str) -> str:
    # "Nvidia H100 Released 2022 80 GB HBM3 Hopper $1.60 /GPU/hr Lium $3.63 /GPU/hr +52 providers"
    return (rf"Nvidia {name_re}\s+Released\s+\d{{4}}[\s\S]{{0,80}}?\$\s*{NUM}\s*/\s*GPU\s*/\s*hr"
            rf"[\s\S]{{0,80}}?\$\s*{NUM}\s*/\s*GPU\s*/\s*hr")


def cw(name: str) -> str:
    # "NVIDIA HGX H100 \n 8 \n 80 \n 128 \n 2,048 \n 61.44 \n $49.24"  (GPU 수, VRAM, vCPU, RAM, 스토리지, 가격)
    return rf"{name}\s+(\d+)\s+{NUM}\s+{NUM}\s+{NUM}\s+{NUM}\s+\$\s*{NUM}"


GPU_TARGETS = {
    GETDEPLOYING: [
        ("H100", gd(r"H100"), "gd"),
        ("H200", gd(r"H200"), "gd"),
        ("B200", gd(r"B200"), "gd"),
        ("A100 80GB", gd(r"A100(?=\s+Released\s+\d{4}\s+80\s*GB)"), "gd"),
    ],
    LAMBDA: [
        ("B200", rf"NVIDIA B200 SXM\d?\s+180\s*GB[\s\S]{{0,120}}?\$\s*{NUM}", "one"),
        ("H200?", rf"NVIDIA H200 SXM\d?\s+141\s*GB[\s\S]{{0,120}}?\$\s*{NUM}", "one"),  # 미공시가 정상 → missing에 안 남김
        ("H100", rf"NVIDIA H100 SXM\d?\s+80\s*GB[\s\S]{{0,120}}?\$\s*{NUM}", "one"),
        ("A100 80GB", rf"NVIDIA A100 SXM\d?\s+80\s*GB[\s\S]{{0,120}}?\$\s*{NUM}", "one"),
    ],
    RUNPOD: [
        ("B200", rf"(?m)^\s*B200\s*\n\s*180\s*GB VRAM[\s\S]{{0,80}}?\$\s*{NUM}\s*/\s*hr", "one"),
        ("H200", rf"(?m)^\s*H200(?: SXM)?\s*\n\s*141\s*GB VRAM[\s\S]{{0,80}}?\$\s*{NUM}\s*/\s*hr", "one"),
        ("H100", rf"(?m)^\s*H100 SXM\s*\n\s*80\s*GB VRAM[\s\S]{{0,80}}?\$\s*{NUM}\s*/\s*hr", "one"),
        ("A100 80GB", rf"(?m)^\s*A100 SXM\s*\n\s*80\s*GB VRAM[\s\S]{{0,80}}?\$\s*{NUM}\s*/\s*hr", "one"),
    ],
    COREWEAVE: [
        ("B200", cw(r"NVIDIA HGX B200"), "cw"),
        ("H200", cw(r"NVIDIA HGX H200"), "cw"),
        ("H100", cw(r"NVIDIA HGX H100"), "cw"),
        ("A100 80GB", cw(r"NVIDIA A100(?: 80GB)?(?=\s+\d+\s+80\s)"), "cw"),
    ],
}
SOURCE_NAME = {GETDEPLOYING: "GetDeploying", LAMBDA: "Lambda", RUNPOD: "RunPod", COREWEAVE: "CoreWeave"}
GPU_RANGE = {"B200": (1.0, 40.0), "H200": (0.8, 30.0), "H100": (0.3, 25.0), "A100 80GB": (0.2, 15.0)}

# 메모리: TrendForce
TF = "https://www.trendforce.com/price/"
MEM_TARGETS = {  # TrendForce는 표마다 페이지가 따로 있다
    TF + "dram/dram_spot": [
        ("DDR5 16Gb (2Gx8) 4800/5600", "DRAM 현물"),
        ("DDR4 16Gb (2Gx8) 3200", "DRAM 현물"),
    ],
    TF + "dram/module_spot": [("DDR5 RDIMM 32GB 4800/5600", "모듈 현물")],
    TF + "dram/gddr_spot": [("GDDR5 8Gb", "GDDR 현물")],
    TF + "dram/dram_contract": [
        ("DDR5 8GB SO-DIMM", "DRAM 고정가"),
        ("DDR4 16GB SO-DIMM", "DRAM 고정가"),
    ],
    TF + "flash/flash_spot": [
        ("SLC 2Gb 256MBx8", "NAND 현물"),
        ("MLC 64Gb 8GBx8", "NAND 현물"),
    ],
    TF + "flash/wafer_spot": [("512Gb TLC", "웨이퍼 현물")],
}

# ------------------------------------------------------------------ 파서 (텍스트 → 값)


def monday(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return (d - timedelta(days=d.weekday())).isoformat()


def to_num(s: str) -> float:
    return float(s.replace(",", ""))


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
REL_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}


def gpu_source_date(url: str, text: str, now_et: datetime) -> tuple[str, str]:
    """GPU 페이지 값의 기준일(미국 동부 날짜)과 그 근거."""
    if url == RUNPOD:
        m = re.search(r"Updated\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
        if m and m.group(1).lower()[:3] in [k[:3] for k in MONTHS]:
            mo = next(v for k, v in MONTHS.items() if k[:3] == m.group(1).lower()[:3])
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}", "page_updated"
    if url == GETDEPLOYING:
        m = re.search(r"Updated\s+(?:(\d+|an?|one)\s+(second|minute|hour|day|week)s?\s+ago|(just now))", text, re.I)
        if m:
            if m.group(3):
                secs = 0
            else:
                n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
                secs = n * REL_UNITS[m.group(2).lower()]
            return (now_et - timedelta(seconds=secs)).date().isoformat(), "page_updated_relative"
    return now_et.date().isoformat(), "collected_us_et"


def parse_gpu(url: str, text: str, week: str, missing: list, now_et: datetime | None = None) -> list:
    rows = []
    src = SOURCE_NAME[url]
    now_et = now_et or datetime.now(US_ET)
    sdate, basis = gpu_source_date(url, text, now_et)
    week = monday(sdate)
    for gpu, pat, mode in GPU_TARGETS[url]:
        optional = gpu.endswith("?")
        gpu = gpu.rstrip("?")
        m = re.search(pat, text)
        if not m:
            if not optional:
                missing.append(f"{src} {gpu}")
            continue
        lo, hi = GPU_RANGE[gpu]
        if mode == "gd":
            pairs = [("market_low", to_num(m.group(1))), ("market_median", to_num(m.group(2)))]
        elif mode == "cw":
            n = int(m.group(1))
            pairs = [("list", round(to_num(m.group(6)) / n + 1e-9, 2))] if n > 0 else []
        else:
            pairs = [("list", to_num(m.group(1)))]
        for kind, price in pairs:
            if lo <= price <= hi:
                rows.append({"week": week, "gpu": gpu, "source": src, "kind": kind, "price": price,
                             "source_date": sdate, "date_basis": basis})
            else:
                missing.append(f"{src} {gpu} {kind} (범위 밖 값 {price})")
    return rows


LAST_UPDATE = re.compile(r"Last\s*Update\s*[:：]?\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", re.I)
PCT = re.compile(r"([▲▼↑↓+\-−—–]?)\s*(-?\d+(?:\.\d+)?)\s*%")


def parse_memory(url: str, text: str, week: str, missing: list) -> list:
    """품목명 바로 뒤의 숫자 열을 읽는다.
    price = 첫 % 앞의 마지막 숫자 (현물: Session Average, 고정가: Average)
    change = 첫 % 값 (▲ +, ▼ -)
    source_date = 품목 앞쪽에서 가장 가까운 'Last Update' 날짜
    """
    rows = []
    for item, cat in MEM_TARGETS[url]:
        m = re.search(re.escape(item) + r"(?=\s+[▲▼—\d$])", text)
        if not m:
            missing.append(f"TrendForce {item}")
            continue
        tail = text[m.end(): m.end() + 300]
        pm = PCT.search(tail)
        if not pm:
            missing.append(f"TrendForce {item} (변동률 없음)")
            continue
        nums = re.findall(r"(?<![\d.])[\d,]+\.\d+|(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d.])|(?<![\d.,])\d+(?![\d.,%])", tail[: pm.start()])
        if not nums:
            missing.append(f"TrendForce {item} (가격 없음)")
            continue
        price = to_num(nums[-1])
        sign = -1 if pm.group(1) in ("▼", "↓", "-", "−") or pm.group(2).startswith("-") else 1
        change = sign * abs(float(pm.group(2)))
        dates = list(LAST_UPDATE.finditer(text, 0, m.start()))
        if not dates:
            missing.append(f"TrendForce {item} (갱신일 없음)")
            continue
        y, mo, d = dates[-1].groups()
        if price <= 0:
            missing.append(f"TrendForce {item} (범위 밖 값 {price})")
            continue
        sdate = f"{y}-{int(mo):02d}-{int(d):02d}"
        rows.append({"week": monday(sdate), "item": item, "category": cat, "price": price,
                     "session_change_pct": change, "source_date": sdate})
    return rows


# ------------------------------------------------------------------ 페이지 읽기


# 숨겨진 탭 안의 표까지 문서 순서대로 읽는다: 표의 각 행은 칸을 탭으로 이어 한 줄로,
# 'Last Update'가 적힌 말단 요소는 그 글자 그대로 한 줄로 낸다. (페이지 안 숨은 요소까지 포함)
TABLES_JS = r"""
() => {
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = w.nextNode())) {
    if (n.tagName === 'TR') {
      out.push([...n.children].map((c) => c.textContent.replace(/\s+/g, ' ').trim()).join('\t'));
    } else if (!n.closest('tr') && n.children.length === 0 && /Last\s*Update/i.test(n.textContent)) {
      // 날짜가 옆 요소에 따로 있으면 부모 글자까지 함께 낸다
      const t = /\d{4}[-/.]\d{1,2}[-/.]\d{1,2}/.test(n.textContent) ? n.textContent : n.parentElement.textContent;
      out.push(t.replace(/\s+/g, ' ').trim());
    }
  }
  return out.join('\n');
}
"""

CHALLENGE = re.compile(r"Performing security verification|Just a moment|Verifying you are human|Checking your browser", re.I)


def fetch_texts(urls: list[str]) -> tuple[dict, dict]:
    from playwright.sync_api import sync_playwright

    texts, errors = {}, {}
    with sync_playwright() as p:
        # 화면 있는 브라우저(Actions에서는 xvfb 가상 화면)가 Cloudflare 확인을 더 잘 통과한다
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            locale="en-US", timezone_id="America/New_York", viewport={"width": 1440, "height": 2400})
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        for url in urls:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Cloudflare 확인 화면이면 최대 45초 기다린다
                for _ in range(15):
                    if not CHALLENGE.search(page.inner_text("body")[:2000]):
                        break
                    page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                for _ in range(8):  # 지연 로딩 대비 끝까지 스크롤
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(400)
                body = page.inner_text("body")
                if CHALLENGE.search(body[:2000]):
                    errors[url] = "Cloudflare 봇 확인 화면을 통과하지 못함"
                try:  # 화면 텍스트 + 표 구조 텍스트(숨은 요소 포함)를 함께 둔다
                    body = body + "\n\n" + page.evaluate(TABLES_JS)
                except Exception:
                    pass
                texts[url] = body
            except Exception as e:  # noqa: BLE001
                errors[url] = f"{type(e).__name__}: {e}"[:300]
            finally:
                page.close()
        browser.close()
    return texts, errors


# ------------------------------------------------------------------ 저장


def merge(path: Path, new_rows: list, keys: tuple) -> int:
    """같은 키(주 + 품목)의 행만 새 값으로 바꾼다. 이번 실행에서 못 가져온 품목은 기존 행을 그대로 둔다."""
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not new_rows:
        return 0
    k = lambda r: tuple(r.get(x) for x in keys)  # noqa: E731
    migrated = []
    for r in old:  # 예전 형식 정리
        if "gpu" in r and not r.get("source_date"):
            # 기준일 도입 전 GPU 행(2026-10-26 KST 11시 수집 = 미국 동부 10-25). RunPod는 페이지 날짜 행으로 대체되므로 버린다.
            if r.get("source") == "RunPod":
                continue
            r["source_date"] = (datetime.strptime(r["week"], "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
            r["date_basis"] = "collected_us_et"
        if r.get("source_date"):
            r["week"] = monday(r["source_date"])
        migrated.append(r)
    old = migrated
    latest = {}
    for r in old + new_rows:  # 같은 주·품목은 기준일이 가장 늦은 값(같으면 나중 것)만 남긴다
        cur = latest.get(k(r))
        if cur is None or (r.get("source_date") or "") >= (cur.get("source_date") or ""):
            latest[k(r)] = r
    rows = sorted(latest.values(), key=lambda r: r.get("week", ""))
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    return len(new_rows)


CSV_COLS = ["date", "week", "group", "item", "category", "source", "kind", "price", "change_pct", "source_date", "unit",
            "collected_at", "date_basis", "price_changed"]


def append_history(path: Path, day: str, stamp: str, gpu_rows: list, mem_rows: list) -> int:
    """수집한 값을 전체 이력 CSV에 붙인다. date = 값의 기준일. 같은 기준일·품목 행이 있으면 새 값으로 바꾼다."""
    new = []
    for r in gpu_rows:
        new.append({"date": r.get("source_date") or day, "week": r["week"], "group": "GPU", "item": r["gpu"],
                    "category": "GPU 임대가", "source": r["source"], "kind": r["kind"], "price": r["price"],
                    "change_pct": "", "source_date": r.get("source_date", ""), "unit": "USD/GPU-hr",
                    "collected_at": stamp, "date_basis": r.get("date_basis", "")})
    for r in mem_rows:
        new.append({"date": r["source_date"], "week": r["week"], "group": "Memory", "item": r["item"], "category": r["category"],
                    "source": "TrendForce", "kind": "contract" if "고정가" in r["category"] else "spot",
                    "price": r["price"], "change_pct": r["session_change_pct"], "source_date": r["source_date"],
                    "unit": "USD", "collected_at": stamp, "date_basis": "page_last_update"})
    if not new:
        return 0
    key = lambda r: (r["date"], r["group"], r["item"], r["source"], r["kind"])  # noqa: E731
    old = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            old = list(csv.DictReader(f))
    for r in old:  # 예전 형식 정리: 메모리 행의 date를 기준일로, 수집 시각이 없으면 예전 date로
        if not r.get("collected_at"):
            r["collected_at"] = r.get("date", "")
        if not r.get("date_basis"):
            if r.get("group") == "Memory":
                r["date_basis"] = "page_last_update"
            else:  # 기준일 도입 전 GPU 행: KST 수집일 → 미국 동부 날짜(전날). RunPod는 페이지 날짜 행으로 대체
                r["date_basis"] = "drop" if r.get("source") == "RunPod" else "collected_us_et"
                if r["date_basis"] != "drop":
                    d = (datetime.strptime(r["date"], "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
                    r["date"], r["source_date"], r["week"] = d, d, monday(d)
        if r.get("group") == "Memory" and r.get("source_date"):
            r["date"] = r["source_date"]
            r["week"] = monday(r["source_date"])
    old = [r for r in old if r.get("date_basis") != "drop"]
    dedup = {}
    for r in old:
        dedup[key(r)] = r
    old = list(dedup.values())
    fresh = {key(r) for r in new}
    rows = [r for r in old if key(r) not in fresh] + new
    # 같은 품목의 직전 기준일 기록과 가격 비교
    ident = lambda r: (r["group"], r["item"], r["source"], r["kind"])  # noqa: E731
    by_item = {}
    for r in sorted(rows, key=lambda r: r["date"]):
        prev = by_item.get(ident(r))
        if prev is None:
            r["price_changed"] = "first"
        else:
            r["price_changed"] = "N" if abs(float(prev["price"]) - float(r["price"])) < 1e-9 else "Y"
        by_item[ident(r)] = r
    rows.sort(key=lambda r: (r["date"], r["group"], r["category"], r["item"], r["source"], r["kind"]))
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: 엑셀에서 한글이 안 깨지게
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="price_data")
    ap.add_argument("--dump-text", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(KST)
    week = (now.date() - timedelta(days=now.weekday())).isoformat()

    urls = list(GPU_TARGETS) + list(MEM_TARGETS)
    texts, errors = fetch_texts(urls)
    if args.dump_text:
        dbg = out / "_debug"
        dbg.mkdir(exist_ok=True)
        for u, t in texts.items():
            (dbg / (re.sub(r"\W+", "_", u)[:80] + ".txt")).write_text(t, encoding="utf-8")

    missing: list[str] = []
    gpu_rows, mem_rows = [], []
    for u in GPU_TARGETS:
        if u in texts:
            gpu_rows += parse_gpu(u, texts[u], week, missing, datetime.now(US_ET))
        else:
            missing.append(f"{SOURCE_NAME[u]} 페이지 전체")
    for u in MEM_TARGETS:
        if u in texts:
            mem_rows += parse_memory(u, texts[u], week, missing)
        else:
            missing.append(f"TrendForce 페이지 전체 ({u})")

    n_gpu = merge(out / "gpu_prices.json", gpu_rows, ("week", "gpu", "source", "kind"))
    n_mem = merge(out / "memory_prices.json", mem_rows, ("week", "item"))
    n_csv = append_history(out / "price_history.csv", now.date().isoformat(), now.isoformat(timespec="seconds"),
                           gpu_rows, mem_rows)
    log = {"run_at": now.isoformat(timespec="seconds"), "week": week,
           "gpu_rows": n_gpu, "memory_rows": n_mem, "history_rows_today": n_csv, "missing": missing, "page_errors": errors}
    (out / "run_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=1))
    # 아무것도 못 가져오면 실패로 표시 (Actions에서 빨간불)
    return 0 if (n_gpu or n_mem) else 1


if __name__ == "__main__":
    sys.exit(main())
