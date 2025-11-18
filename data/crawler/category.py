# 웹 크롤링을 위한 제품 카테고리 수집
import re
import json
import time
import argparse
from typing import Dict, List
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = (
    "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do"
    "?dispCatNo=10000010001&gateCd=Drawer&t_page=드로우_카테고리"
    "&t_click=카테고리탭_대카테고리&t_1st_category_type=대_스킨케어"
)

FIRST_PAT = re.compile(r"t_1st_category_type:\s*['\"](대_[^'\"]+)['\"]")
SECOND_PAT = re.compile(r"t_2nd_category_type:\s*['\"]중_([^'\"]+)['\"]")


def set_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q[key] = value
    new_query = urlencode(q, doseq=True, encoding="utf-8", quote_via=quote)
    return urlunparse(parsed._replace(query=new_query))

def uniq_keep_order(items: List[str]) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def navigate(page, url: str):
    # 페이지 이동 + 네트워크 안정화
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # 짧은 대기(동적 삽입 대기)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeoutError:
        pass

def extract_first_categories(page) -> List[str]:
    anchors = page.eval_on_selector_all(
        'a[href^="javascript:common.link.moveCategoryShop"]',
        "els => els.map(a => ({href: a.getAttribute('href') || ''}))",
    )
    firsts = []
    for a in anchors:
        href = a.get("href", "")
        m = FIRST_PAT.search(href)
        if m:
            firsts.append(m.group(1))
    return uniq_keep_order(firsts)

def extract_second_categories(page, first_key: str) -> List[str]:
    anchors = page.eval_on_selector_all(
        'li > a[href^="javascript:common.link.moveCategory"]',
        "els => els.map(a => ({"
        "  href: a.getAttribute('href') || '',"
        "  text: (a.textContent || '').trim(),"
        "}))",
    )
    mids = []
    first_key_pat = re.compile(r"t_1st_category_type:\s*['\"]" + re.escape(first_key) + r"['\"]")
    for a in anchors:
        href = a.get("href", "")
        if not first_key_pat.search(href):
            continue
        m2 = SECOND_PAT.search(href)
        name = (m2.group(1).strip() if m2 else a.get("text", "").strip())
        if name:
            mids.append(name)
    return uniq_keep_order(mids)

def crawl_all_with_playwright(base_url: str, headless: bool = True, throttle: float = 0.4) -> Dict[str, List[str]]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # 1) 시작 페이지 진입
        navigate(page, base_url)

        # 2) 대분류 키 수집
        first_keys = extract_first_categories(page)

        # 대분류 링크가 없는 경우: URL의 쿼리에서 추출
        if not first_keys:
            m = re.search(r"[?&]t_1st_category_type=([^&]+)", base_url)
            if m:
                only_first = re.sub(r"\+", " ", m.group(1))
                first_keys = [only_first]

        # 3) 각 대분류마다 이동 → 중분류 추출
        result: Dict[str, List[str]] = {}
        for first in first_keys:
            url = set_query_param(base_url, "t_1st_category_type", first)
            navigate(page, url)
            mids = extract_second_categories(page, first)
            result[first] = mids
            time.sleep(throttle)

        context.close()
        browser.close()
        return result

def main():
    url = BASE_URL
    out = "category/categories.json"

    data = crawl_all_with_playwright(url)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved -> {out}")

if __name__ == "__main__":
    main()
