#!/usr/bin/env python3
"""
GPU 임대가 · 메모리 가격 주간 수집기

가격 페이지를 Playwright(Chromium)로 열어 화면 텍스트를 읽고, 정해진 품목의 가격을 뽑아
주간(월요일 기준, Asia/Seoul) 행으로 누적 저장한다.

출력 (--out-dir, 기본 ./price_data):
  gpu_prices.json     [{week, gpu, source, kind, price}]            kind: list | market_low | market_median
  memory_prices.json  [{week, item, category, price, session_change_pct, source_date}]
  run_log.json        마지막 실행 결과 (가져온 개수, 못 가져온 항목, 페이지 오류)

사용법:
  pip install playwright && python -m playwright install --with-deps chromium
  python price_tracker.py                 # 이번 주 행을 새로 받아 덮어쓰기
  python price_tracker.py --dump-text     # 각 페이지 텍스트를 price_data/_debug/에 저장 (파서 점검용)

원칙:
  - 페이지에서 찾지 못한 값은 행을 만들지 않는다(추정·이월 없음). run_log.json의 missing에 남긴다.
  - 같은 주에 다시 돌리면 그 주의 행만 교체된다.
  - 값이 상식 범위를 벗어나면(파싱 오류 가능성) 버리고 missing에 남긴다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))

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
        ("H200", rf"NVIDIA H200 SXM\d?\s+141\s*GB[\s\S]{{0,120}}?\$\s*{NUM}", "one"),
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
TF_DRAM = "https://www.trendforce.com/price/dram/dram_spot"
TF_FLASH = "https://www.trendforce.com/price/flash/flash_spot"
MEM_TARGETS = {
    TF_DRAM: [
        ("DDR5 16Gb (2Gx8) 4800/5600", "DRAM 현물"),
        ("DDR4 16Gb (2Gx8) 3200", "DRAM 현물"),
        ("DDR5 RDIMM 32GB 4800/5600", "모듈 현물"),
        ("GDDR5 8Gb", "GDDR 현물"),
        ("DDR5 8GB SO-DIMM", "DRAM 고정가"),
        ("DDR4 16GB SO-DIMM", "DRAM 고정가"),
    ],
    TF_FLASH: [
        ("SLC 2Gb 256MBx8", "NAND 현물"),
        ("MLC 64Gb 8GBx8", "NAND 현물"),
        ("512Gb TLC", "웨이퍼 현물"),
    ],
}

# ------------------------------------------------------------------ 파서 (텍스트 → 값)


def to_num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_gpu(url: str, text: str, week: str, missing: list) -> list:
    rows = []
    src = SOURCE_NAME[url]
    for gpu, pat, mode in GPU_TARGETS[url]:
        m = re.search(pat, text)
        if not m:
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
                rows.append({"week": week, "gpu": gpu, "source": src, "kind": kind, "price": price})
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
        rows.append({"week": week, "item": item, "category": cat, "price": price,
                     "session_change_pct": change, "source_date": f"{y}-{int(mo):02d}-{int(d):02d}"})
    return rows


# ------------------------------------------------------------------ 페이지 읽기


def fetch_texts(urls: list[str]) -> tuple[dict, dict]:
    from playwright.sync_api import sync_playwright

    texts, errors = {}, {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
            locale="en-US", viewport={"width": 1440, "height": 2400})
        for url in urls:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                # 지연 로딩 대비 끝까지 스크롤
                for _ in range(8):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(400)
                texts[url] = page.inner_text("body")
            except Exception as e:  # noqa: BLE001
                errors[url] = f"{type(e).__name__}: {e}"[:300]
            finally:
                page.close()
        browser.close()
    return texts, errors


# ------------------------------------------------------------------ 저장


def merge(path: Path, new_rows: list, week: str) -> int:
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not new_rows:
        return 0
    kept = [r for r in old if r.get("week") != week]
    path.write_text(json.dumps(kept + new_rows, ensure_ascii=False, indent=0), encoding="utf-8")
    return len(new_rows)


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
            gpu_rows += parse_gpu(u, texts[u], week, missing)
        else:
            missing.append(f"{SOURCE_NAME[u]} 페이지 전체")
    for u in MEM_TARGETS:
        if u in texts:
            mem_rows += parse_memory(u, texts[u], week, missing)
        else:
            missing.append(f"TrendForce 페이지 전체 ({u})")

    n_gpu = merge(out / "gpu_prices.json", gpu_rows, week)
    n_mem = merge(out / "memory_prices.json", mem_rows, week)
    log = {"run_at": now.isoformat(timespec="seconds"), "week": week,
           "gpu_rows": n_gpu, "memory_rows": n_mem, "missing": missing, "page_errors": errors}
    (out / "run_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=1))
    # 아무것도 못 가져오면 실패로 표시 (Actions에서 빨간불)
    return 0 if (n_gpu or n_mem) else 1


if __name__ == "__main__":
    sys.exit(main())
