# 제품 리스트 수집 (t_number 기반 중복 제거 + 디버깅 강화 + 누락 상품 재시도)
import json
import os
import time
import argparse
import re
import difflib
from typing import Dict, List, Any
from urllib.parse import urlencode, quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# -------- 설정값 --------
BASE_CATEGORY_URL = (
    "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do"
    "?dispCatNo=10000010001&gateCd=Drawer&t_page=드로우_카테고리"
    "&t_click=카테고리탭_대카테고리&t_1st_category_type=대_스킨케어"
)

PRODUCT_LIST_BASE = "https://www.oliveyoung.co.kr/store/display/getMCategoryList.do"

FIRST_PAT = re.compile(r"t_1st_category_type:\s*['\"](대_[^'\"]+)['\"]")
SECOND_PAT = re.compile(r"t_2nd_category_type:\s*['\"]중_([^'\"]+)['\"]")
DISP_PAT = re.compile(r"moveCategory\('(\d{5,})'")  # href 내부 dispCatNo 추정 백업패턴

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sanitize(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", name)

def extract_t_number_from_url(url: str) -> str:
    """detailUrl에서 t_number 추출 (고유 ID로 사용)"""
    if not url:
        return ""
    match = re.search(r"t_number=(\d+)", url)
    return match.group(1) if match else ""

def navigate(page, url: str):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeoutError:
        pass
    # 동적 삽입 유도: 스크롤 트리거
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.3)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.2)
    except Exception:
        pass

def eval_all(page, selector: str, script: str):
    return page.eval_on_selector_all(selector, script)

def uniq_keep_order(items: List[Any]) -> List[Any]:
    seen, out = set(), []
    for x in items:
        key = json.dumps(x, ensure_ascii=False, sort_keys=True) if isinstance(x, dict) else x
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def norm(name: str) -> str:
    """카테고리명 비교용 정규화: 공백/기호/대소문자 차이 완화."""
    if not name:
        return ""
    s = name.strip()
    s = s.replace(" ", "").replace("_", "").replace("-", "")
    s = s.replace("/", "").replace("\\", "")
    s = s.replace("·", "").replace("・", "")
    return s.casefold()

def choose_disp_for_target(mid_rows, target_mid_name: str, fuzzy_threshold: float = 0.72):
    """정규화 일치 → 원문 일치 → 퍼지 매칭 순으로 dispCatNo 선택."""
    target_n = norm(target_mid_name)

    # 1) 정규화 일치
    for r in mid_rows:
        if norm(r["mid_name"]) == target_n:
            return r["dispCatNo"], "exact_norm"

    # 2) 원문 완전일치
    for r in mid_rows:
        if r["mid_name"] == target_mid_name:
            return r["dispCatNo"], "exact_raw"

    # 3) 퍼지 매칭
    candidates = [r["mid_name"] for r in mid_rows]
    if candidates:
        best = difflib.get_close_matches(target_mid_name, candidates, n=1, cutoff=fuzzy_threshold)
        if best:
            best_name = best[0]
            for r in mid_rows:
                if r["mid_name"] == best_name:
                    return r["dispCatNo"], "fuzzy"

    return None, "none"

# -------- 중분류 앵커 수집(강화) --------
def get_mid_category_rows(page, first_key: str) -> List[Dict[str, str]]:
    """
    대분류 페이지에서 중분류 앵커를 최대한 견고하게 수집.
    - 로딩 대기 + 스크롤 + 최대 3회 재시도
    - 선택자 확장: moveCategory + data-ref-dispcatno
    - href 백업 파싱으로 dispCatNo 확보
    - 로그/샘플 출력
    """
    fk_pat = re.compile(r"t_1st_category_type:\\s*['\"]" + re.escape(first_key) + r"['\"]")

    def collect() -> List[Dict[str, str]]:
        anchors = eval_all(
            page,
            'li a[href^="javascript:common.link.moveCategory"], a[data-ref-dispcatno]',
            "els => els.map(a => ({"
            "  href: a.getAttribute('href') || '',"
            "  text: (a.textContent || '').trim(),"
            "  disp: a.getAttribute('data-ref-dispcatno') || ''"
            "}))"
        )
        out = []
        for a in anchors:
            href = a.get("href", "")
            text = a.get("text") or ""

            # 대분류 키 체크: href에 명시되어 있으면 검사
            if href and "t_1st_category_type" in href and not fk_pat.search(href):
                continue

            m2 = SECOND_PAT.search(href or "")
            mid_name = (m2.group(1).strip() if m2 else text)

            disp = a.get("disp") or ""
            if not disp:
                # href에서 숫자 백업 파싱
                mdisp = re.search(r"moveCategory\\(['\\\"](\\d{5,})['\\\"]", href or "")
                if not mdisp:
                    mdisp = re.search(r"moveCategoryShop\\(['\\\"](\\d{5,})['\\\"]", href or "")
                if mdisp:
                    disp = mdisp.group(1)

            if mid_name and disp:
                out.append({"mid_name": mid_name, "dispCatNo": disp, "href": href})
        return uniq_keep_order(out)

    rows = []
    for attempt in range(3):
        try:
            page.wait_for_selector('li a[href^="javascript:common.link.moveCategory"], a[data-ref-dispcatno]', timeout=4000)
        except PWTimeoutError:
            pass

        # 동적 삽입 유도 스크롤
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.2)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.2)
        except Exception:
            pass

        rows = collect()
        if rows:
            break

    print(f"[INFO] mid anchors found for {first_key}: {len(rows)}")
    if rows:
        sample = rows[:3]
        print("[INFO] sample mids:", [(r['mid_name'], r['dispCatNo']) for r in sample])
    return rows

# -------- 상품 목록 파싱 --------
def build_product_list_url(first_key: str, mid_name: str, page_idx: int, disp_cat_no: str = "") -> str:
    """실제 올리브영 URL 파라미터 구조"""
    q = {
        "dispCatNo": disp_cat_no,  # 필수!
        "fltDispCatNo": "",
        "prdSort": "01",
        "pageIdx": str(page_idx),
        "rowsPerPage": "24",
        "searchTypeSort": "btn_thumb",
        "plusButtonFlag": "N",
        "isLoginCnt": "0",
        "aShowCnt": "0",
        "bShowCnt": "0", 
        "cShowCnt": "0",
        "trackingCd": f"Cat{disp_cat_no}_Small",
        "t_page": "카테고리관",
        "t_click": "카테고리상세_중카테고리",
        "t_1st_category_type": first_key,
        "t_2nd_category_type": f"중_{mid_name}",
    }
    return PRODUCT_LIST_BASE + "?" + urlencode(q, encoding="utf-8", quote_via=quote)

def extract_product_details(page, product_url: str, max_retries: int = 3) -> Dict[str, str]:
    """
    상품 상세 페이지에서 구매정보 추출
    - 내용물의 용량 또는 중량
    - 제품 주요 사양
    - 사용방법
    - 화장품법에 따라 기재해야 하는 모든 성분
    - 사용할 때의 주의사항
    """
    details = {
        "volume": "",
        "spec": "",
        "usage": "",
        "ingredients": "",
        "caution": ""
    }
    
    for attempt in range(max_retries):
        try:
            # 상세 페이지로 이동
            page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.5)
            
            # "구매정보" 버튼 찾아서 클릭
            buy_info_button = page.query_selector('li#buyInfo a.goods_buyinfo, a.goods_buyinfo')
            if buy_info_button:
                buy_info_button.click()
                time.sleep(0.8)  # 탭 전환 대기
            else:
                # 버튼이 없으면 직접 스크롤해서 찾기
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                time.sleep(0.5)
            
            # detail_info_list에서 정보 추출
            info_lists = page.query_selector_all('dl.detail_info_list')
            
            for dl in info_lists:
                dt = dl.query_selector('dt')
                dd = dl.query_selector('dd')
                
                if not dt or not dd:
                    continue
                
                key = dt.text_content().strip()
                value = dd.text_content().strip()
                
                # 매핑
                if "내용물의 용량" in key or "중량" in key:
                    details["volume"] = value
                elif "제품 주요 사양" in key:
                    details["spec"] = value
                elif "사용방법" in key:
                    details["usage"] = value
                elif "성분" in key and "화장품법" in key:
                    details["ingredients"] = value
                elif "주의사항" in key:
                    details["caution"] = value
            
            # 성공했으면 루프 종료
            if any(details.values()):
                break
                
        except Exception as e:
            print(f"      [WARN] Detail extraction failed (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1.0)
            continue
    
    return details

def parse_products_from_fragment(page) -> List[Dict[str, Any]]:
    """
    HTML fragment에서 각 상품의 .prd_info 블록만 대상으로 추출.
    """
    items = page.eval_on_selector_all(
        ".prd_info",
        """
        els => els.map(root => {
          // 썸네일 a, img
          const aThumb = root.querySelector('a.prd_thumb') || root.querySelector('a[name$="_Small"]');
          const img = aThumb ? aThumb.querySelector('img') : root.querySelector('img');

          // 이름/브랜드 - prd_name 안의 a 태그에서 href 추출
          const nameWrap = root.querySelector('.prd_name');
          const nameLink = nameWrap ? nameWrap.querySelector('a') : null;
          const brand = nameWrap ? (nameWrap.querySelector('.tx_brand')?.textContent || '').trim() : '';
          const name  = nameWrap ? (nameWrap.querySelector('.tx_name')?.textContent || '').trim() : '';
          const detailUrl = nameLink ? (nameLink.getAttribute('href') || '').trim() : '';

          // 가격
          const priceWrap = root.querySelector('.prd_price');
          const orgNode = priceWrap ? priceWrap.querySelector('.tx_org .tx_num') : null;
          const curNode = priceWrap ? priceWrap.querySelector('.tx_cur .tx_num') : null;

          // 기타 식별
          const goodsNo = (aThumb || nameLink) ? ((aThumb ? aThumb.getAttribute('data-ref-goodsno') : '') || (nameLink ? nameLink.getAttribute('data-ref-goodsno') : '')) : '';
          const dispCat = (aThumb || nameLink) ? ((aThumb ? aThumb.getAttribute('data-ref-dispcatno') : '') || (nameLink ? nameLink.getAttribute('data-ref-dispcatno') : '')) : '';

          return {
            goodsNo,
            dispCatNo: dispCat,
            image: img ? (img.getAttribute('src') || '').trim() : '',
            brand,
            name,
            detailUrl,  // 상세 페이지 URL 추가
            price_org_raw: orgNode ? orgNode.textContent : '',
            price_cur_raw: curNode ? curNode.textContent : ''
          };
        })
        """
    )

    # 파이썬에서 가격 정제 + t_number 추출
    out: List[Dict[str, Any]] = []
    for it in items:
        price_org = re.sub(r"[^\d]", "", it.get("price_org_raw", "") or "")
        price_cur = re.sub(r"[^\d]", "", it.get("price_cur_raw", "") or "")
        
        # 원가가 없으면 현재가를 원가로 세팅(비세일 케이스)
        if not price_org and price_cur:
            price_org = price_cur

        # t_number 추출 (가장 중요한 고유 ID)
        t_number = extract_t_number_from_url(it.get("detailUrl", ""))

        out.append({
            "goodsNo": it.get("goodsNo", ""),
            "dispCatNo": it.get("dispCatNo", ""),
            "image": it.get("image", ""),
            "brand": it.get("brand", ""),
            "name": it.get("name", ""),
            "detailUrl": it.get("detailUrl", ""),  # URL 포함
            "t_number": t_number,  # 고유 ID 추가
            "price_org": price_org,
            "price_cur": price_cur,
        })
    return out

def crawl_one_mid(page, first_key: str, mid_name: str, disp_cat_no: str, throttle: float = 0.5) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    page_idx = 1
    seen_goods = set()

    while True:
        url = build_product_list_url(disp_cat_no, first_key, mid_name, page_idx)
        navigate(page, url)

        # fragment에 상품 li가 없으면 종료
        count = page.locator('li[criteo-goods]').count()
        if count == 0:
            # 디버그 덤프 (해당 페이지에 정말 없었는지 체크)
            break

        items = parse_products_from_fragment(page)
        new = 0
        for it in items:
            # t_number 기반 고유 ID 사용
            t_num = it.get("t_number", "")
            gid = f"t_{t_num}" if t_num else (it.get("goodsNo") or f"{disp_cat_no}:{page_idx}:{it.get('detailUrl','')}")
            
            if gid in seen_goods:
                continue
            seen_goods.add(gid)
            it["first_category"] = first_key
            it["mid_category"] = mid_name
            it["page_idx"] = page_idx
            all_items.append(it)
            new += 1

        # 더 이상 새 아이템이 없으면 종료(안전장치)
        if new == 0:
            break

        page_idx += 1
        time.sleep(throttle)

    return all_items

def navigate_with_referer(page, url: str, referer: str = None):
    """Referer 헤더와 함께 네비게이션"""
    extra_headers = {}
    if referer:
        extra_headers["Referer"] = referer
    
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeoutError:
        pass
    
    # 동적 로딩 대기
    time.sleep(0.5)
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.3)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.2)
    except Exception:
        pass


def find_missing_t_numbers(json_path: str, expected_max: int = 879) -> List[int]:
    """
    products.json 파일에서 수집된 t_number를 확인하고 누락된 번호를 찾습니다.
    
    Args:
        json_path: products.json 파일 경로
        expected_max: 예상되는 최대 t_number (기본값: 879)
    
    Returns:
        누락된 t_number 리스트
    """
    print(f"\n[ANALYSIS] Checking for missing t_numbers in {json_path}...")
    
    # JSON 파일 로드
    with open(json_path, "r", encoding="utf-8") as f:
        products = json.load(f)
    
    # detailUrl에서 t_number 추출
    collected_numbers = set()
    
    for product in products:
        t_num = product.get("t_number", "")
        if t_num:
            collected_numbers.add(int(t_num))
    
    # 1부터 expected_max까지 중 누락된 번호 찾기
    expected_numbers = set(range(1, expected_max + 1))
    missing_numbers = sorted(expected_numbers - collected_numbers)
    
    print(f"[ANALYSIS] Total products: {len(products)}")
    print(f"[ANALYSIS] Collected t_numbers: {len(collected_numbers)}")
    print(f"[ANALYSIS] Expected range: 1-{expected_max}")
    print(f"[ANALYSIS] Missing t_numbers: {len(missing_numbers)}")
    
    if missing_numbers:
        # 처음 20개와 마지막 20개만 출력
        if len(missing_numbers) <= 40:
            print(f"[ANALYSIS] Missing list: {missing_numbers}")
        else:
            print(f"[ANALYSIS] First 20 missing: {missing_numbers[:20]}")
            print(f"[ANALYSIS] Last 20 missing: {missing_numbers[-20:]}")
    
    return missing_numbers


def crawl_missing_products_by_t_number(
    missing_t_numbers: List[int],
    categories: Dict[str, List[str]],
    existing_json_path: str,
    output_path: str = "products_recovered.json",
    headless: bool = True,
    throttle: float = 1.0,
    fetch_details: bool = True
):
    """
    누락된 t_number에 해당하는 상품들을 재크롤링합니다.
    
    Args:
        missing_t_numbers: 누락된 t_number 리스트
        categories: 카테고리 딕셔너리
        existing_json_path: 기존 products.json 파일 경로
        output_path: 복구된 상품을 저장할 파일 경로
        headless: 헤드리스 모드 여부
        throttle: 크롤링 간격 (초)
        fetch_details: 상세 정보 크롤링 여부
    """
    if not missing_t_numbers:
        print("[INFO] No missing t_numbers to recover!")
        return
    
    print(f"\n[RECOVERY] Starting recovery for {len(missing_t_numbers)} missing products...")
    
    recovered_products = []
    failed_t_numbers = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080},
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
        )
        
        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        # 각 카테고리의 상품 목록 페이지를 순회하며 누락된 t_number 찾기
        for primary, mids in categories.items():
            print(f"\n[RECOVERY PRIMARY] {primary}")
            
            for mid_name in mids:
                print(f"  [RECOVERY MID] {mid_name}")
                
                # 카테고리 페이지 방문
                category_url = (
                    "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do?"
                    + urlencode({
                        "dispCatNo": "10000010001",
                        "gateCd": "Drawer",
                        "t_page": "드로우_카테고리",
                        "t_click": "카테고리탭_대카테고리",
                        "t_1st_category_type": primary
                    }, encoding="utf-8", quote_via=quote)
                )
                
                page.goto(category_url, wait_until="domcontentloaded")
                time.sleep(1.5)
                
                # dispCatNo 찾기
                mid_links = page.query_selector_all('a[href*="moveCategory"]')
                disp_cat_no = None
                
                for link in mid_links:
                    text = link.text_content().strip()
                    href = link.get_attribute('href') or ''
                    
                    if any(kw in text for kw in ['원', '세일', 'ml', 'ML', '기획']):
                        continue
                    
                    if norm(text) == norm(mid_name) or text == mid_name:
                        match = re.search(r"moveCategory\('(\d+)'", href)
                        if match:
                            disp_cat_no = match.group(1)
                            break
                
                if not disp_cat_no:
                    continue
                
                # 상품 목록 크롤링하면서 누락된 t_number 찾기
                page_idx = 1
                found_in_this_category = []
                
                while True:
                    product_url = build_product_list_url(primary, mid_name, page_idx, disp_cat_no)
                    
                    page.goto(product_url, wait_until="domcontentloaded")
                    time.sleep(1.5)
                    
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.3)
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(0.2)
                    
                    count = page.locator('.prd_info').count()
                    if count == 0:
                        break
                    
                    items = parse_products_from_fragment(page)
                    
                    # 각 상품의 t_number 확인
                    for idx, it in enumerate(items):
                        t_num_str = it.get("t_number", "")
                        if t_num_str:
                            t_num = int(t_num_str)
                            
                            # 누락된 번호이면 크롤링
                            if t_num in missing_t_numbers:
                                print(f"    [FOUND] t_number={t_num} at position {idx+1}")
                                
                                # 기본 정보 세팅
                                it["first_category"] = primary
                                it["mid_category"] = mid_name
                                it["page_idx"] = page_idx
                                
                                # 상세 정보 크롤링
                                if fetch_details and it.get("detailUrl"):
                                    detail_url = it["detailUrl"]
                                    if not detail_url.startswith("http"):
                                        detail_url = "https://www.oliveyoung.co.kr" + detail_url
                                    
                                    try:
                                        print(f"      [DETAIL] {it['name'][:30]}...")
                                        details = extract_product_details(page, detail_url, max_retries=2)
                                        
                                        it.update({
                                            "volume": details.get("volume", ""),
                                            "spec": details.get("spec", ""),
                                            "usage": details.get("usage", ""),
                                            "ingredients": details.get("ingredients", ""),
                                            "caution": details.get("caution", "")
                                        })
                                        
                                        # 목록 페이지로 돌아가기
                                        page.goto(product_url, wait_until="domcontentloaded")
                                        time.sleep(0.8)
                                    except Exception as e:
                                        print(f"      [ERROR] {e}")
                                        failed_t_numbers.append(t_num)
                                        try:
                                            page.goto(product_url, wait_until="domcontentloaded")
                                            time.sleep(0.8)
                                        except:
                                            pass
                                
                                recovered_products.append(it)
                                found_in_this_category.append(t_num)
                                missing_t_numbers.remove(t_num)  # 리스트에서 제거
                    
                    # 더 이상 누락된 번호가 없으면 종료
                    if not missing_t_numbers:
                        print(f"    [SUCCESS] All missing products recovered!")
                        break
                    
                    page_idx += 1
                    if page_idx > 100:  # 안전장치
                        break
                    
                    time.sleep(throttle)
                
                if found_in_this_category:
                    print(f"  [CATEGORY RESULT] Recovered {len(found_in_this_category)} products: {found_in_this_category[:10]}{'...' if len(found_in_this_category) > 10 else ''}")
                
                # 모든 누락 상품을 찾았으면 종료
                if not missing_t_numbers:
                    break
            
            if not missing_t_numbers:
                break
        
        context.close()
        browser.close()
    
    # 결과 저장
    if recovered_products:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(recovered_products, f, ensure_ascii=False, indent=2)
        print(f"\n[RECOVERY DONE] {len(recovered_products)} products saved to {output_path}")
    
    if failed_t_numbers:
        print(f"[RECOVERY FAILED] {len(failed_t_numbers)} products failed: {failed_t_numbers}")
    
    if missing_t_numbers:
        print(f"[RECOVERY INCOMPLETE] {len(missing_t_numbers)} products still missing: {missing_t_numbers[:20]}{'...' if len(missing_t_numbers) > 20 else ''}")
    
    return recovered_products, failed_t_numbers, list(missing_t_numbers)


def merge_json_files(original_path: str, recovered_path: str, output_path: str = "products_complete.json"):
    """
    기존 products.json과 복구된 products_recovered.json을 t_number 순서대로 병합합니다.
    빠진 t_number 자리에 복구된 상품을 삽입합니다.
    
    Args:
        original_path: 기존 products.json 경로
        recovered_path: 복구된 products_recovered.json 경로
        output_path: 병합 결과 저장 경로
    """
    print(f"\n[MERGE] Merging {original_path} and {recovered_path} by t_number...")
    
    # 파일 로드
    with open(original_path, "r", encoding="utf-8") as f:
        original_products = json.load(f)
    
    try:
        with open(recovered_path, "r", encoding="utf-8") as f:
            recovered_products = json.load(f)
    except FileNotFoundError:
        print(f"[MERGE] {recovered_path} not found, using only original file")
        recovered_products = []
    
    # t_number를 키로 하는 딕셔너리 생성
    products_by_tnumber = {}
    
    # 1. 원본 상품들을 t_number로 인덱싱
    print(f"[MERGE] Indexing original products by t_number...")
    original_count = 0
    for product in original_products:
        t_num = product.get("t_number", "")
        if t_num:
            products_by_tnumber[int(t_num)] = product
            original_count += 1
    
    print(f"[MERGE] Original products indexed: {original_count}")
    
    # 2. 복구된 상품들을 t_number로 추가/교체
    print(f"[MERGE] Adding recovered products...")
    inserted_count = 0
    replaced_count = 0
    
    for product in recovered_products:
        t_num = product.get("t_number", "")
        if t_num:
            t_num_int = int(t_num)
            if t_num_int in products_by_tnumber:
                print(f"  [REPLACE] t_number={t_num}: {product.get('name', 'Unknown')[:40]}")
                replaced_count += 1
            else:
                print(f"  [INSERT] t_number={t_num}: {product.get('name', 'Unknown')[:40]}")
                inserted_count += 1
            products_by_tnumber[t_num_int] = product
    
    # 3. t_number 순서대로 정렬
    print(f"[MERGE] Sorting by t_number...")
    sorted_t_numbers = sorted(products_by_tnumber.keys())
    merged_products = [products_by_tnumber[t_num] for t_num in sorted_t_numbers]
    
    # 4. 누락 확인
    if sorted_t_numbers:
        min_t = sorted_t_numbers[0]
        max_t = sorted_t_numbers[-1]
        expected_range = set(range(min_t, max_t + 1))
        actual_range = set(sorted_t_numbers)
        still_missing = sorted(expected_range - actual_range)
        
        if still_missing:
            print(f"\n[MERGE WARNING] Still missing t_numbers in range {min_t}-{max_t}:")
            if len(still_missing) <= 20:
                print(f"  Missing: {still_missing}")
            else:
                print(f"  First 10: {still_missing[:10]}")
                print(f"  Last 10: {still_missing[-10:]}")
                print(f"  Total missing: {len(still_missing)}")
    
    # 5. 저장
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged_products, f, ensure_ascii=False, indent=2)
    
    print(f"\n[MERGE RESULT]")
    print(f"  Original products: {original_count}")
    print(f"  Recovered products: {len(recovered_products)}")
    print(f"    - Inserted (new): {inserted_count}")
    print(f"    - Replaced (existing): {replaced_count}")
    print(f"  Final merged: {len(merged_products)}")
    if sorted_t_numbers:
        print(f"  t_number range: {sorted_t_numbers[0]} - {sorted_t_numbers[-1]}")
    print(f"[MERGE] Saved to {output_path}")
    
    return merged_products


def main(categories, out_path="products.json", headless=False, throttle=1.0, fetch_details=True):
    """
    fetch_details: True이면 각 상품의 상세 정보도 크롤링
    """
    all_products = []
    global_seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
            ]
        )
        context = browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080},
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
        )
        
        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        for primary, mids in categories.items():
            print(f"\n[PRIMARY] {primary}")
            
            for mid_name in mids:
                print(f"  [MID] {mid_name}")
                
                # 카테고리 페이지 방문
                category_url = (
                    "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do?"
                    + urlencode({
                        "dispCatNo": "10000010001",
                        "gateCd": "Drawer",
                        "t_page": "드로우_카테고리",
                        "t_click": "카테고리탭_대카테고리",
                        "t_1st_category_type": primary
                    }, encoding="utf-8", quote_via=quote)
                )
                
                print(f"    [STEP 1] Visiting category page...")
                page.goto(category_url, wait_until="domcontentloaded")
                time.sleep(2)
                
                # dispCatNo 찾기
                mid_links = page.query_selector_all('a[href*="moveCategory"]')
                disp_cat_no = None
                
                for link in mid_links:
                    text = link.text_content().strip()
                    href = link.get_attribute('href') or ''
                    
                    if any(kw in text for kw in ['원', '세일', 'ml', 'ML', '기획']):
                        continue
                    
                    if norm(text) == norm(mid_name) or text == mid_name:
                        match = re.search(r"moveCategory\('(\d+)'", href)
                        if match:
                            disp_cat_no = match.group(1)
                            print(f"    [FOUND] {mid_name} -> dispCatNo={disp_cat_no}")
                            break
                
                if not disp_cat_no:
                    print(f"    [ERROR] Could not find dispCatNo for {mid_name}")
                    continue
                
                # 상품 목록 크롤링
                page_idx = 1
                seen_goods = set()
                
                while True:
                    product_url = build_product_list_url(primary, mid_name, page_idx, disp_cat_no)
                    print(f"    [PAGE {page_idx}]")
                    
                    page.goto(product_url, wait_until="domcontentloaded")
                    time.sleep(1.5)
                    
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(0.3)
                    
                    count = page.locator('.prd_info').count()
                    print(f"      [COUNT] {count} products")
                    
                    if count == 0:
                        if page_idx == 1:
                            debug_file = f"debug_{sanitize(primary)}_{sanitize(mid_name)}.html"
                            with open(debug_file, "w", encoding="utf-8") as f:
                                f.write(page.content())
                            print(f"      [DEBUG] Saved to {debug_file}")
                        break
                    
                    items = parse_products_from_fragment(page)
                    
                    new_count = 0
                    actually_added_indices = []  # 실제로 all_products에 추가된 인덱스만 추적
                    
                    for idx, it in enumerate(items):
                        # t_number를 최우선 고유 ID로 사용
                        t_num = it.get("t_number", "")
                        goods_no = it.get("goodsNo", "")
                        
                        # 고유 ID 생성 우선순위: t_number > goodsNo > fallback
                        if t_num:
                            gid = f"t_{t_num}"
                        elif goods_no:
                            gid = f"g_{goods_no}"
                        else:
                            gid = f"fb_{disp_cat_no}:{page_idx}:{idx}"
                        
                        if gid in global_seen or gid in seen_goods:
                            continue
                        
                        seen_goods.add(gid)
                        global_seen.add(gid)
                        
                        it["first_category"] = primary
                        it["mid_category"] = mid_name
                        it["page_idx"] = page_idx
                        
                        # 상세 정보 크롤링
                        should_add = True  # 기본적으로 추가
                        if fetch_details and it.get("detailUrl"):
                            detail_url = it["detailUrl"]
                            # 상대 경로를 절대 경로로 변환
                            if not detail_url.startswith("http"):
                                detail_url = "https://www.oliveyoung.co.kr" + detail_url
                            
                            print(f"      [DETAIL {idx+1}/{len(items)}] t_number={t_num} {it['name'][:30]}...")
                            
                            try:
                                details = extract_product_details(page, detail_url)
                                
                                # 상세 정보가 하나라도 있으면 성공으로 간주
                                if any(details.values()):
                                    # 상세 정보 추가
                                    it.update({
                                        "volume": details["volume"],
                                        "spec": details["spec"],
                                        "usage": details["usage"],
                                        "ingredients": details["ingredients"],
                                        "caution": details["caution"]
                                    })
                                else:
                                    # 상세 정보 수집 실패 - 이 상품은 추가하지 않고 재시도 대상으로
                                    print(f"        [WARN] No details extracted, will retry")
                                    should_add = False
                                    seen_goods.remove(gid)  # 재시도를 위해 제거
                                    global_seen.remove(gid)
                                
                                # 목록 페이지로 돌아가기
                                page.goto(product_url, wait_until="domcontentloaded")
                                time.sleep(0.8)
                            except Exception as e:
                                print(f"        [ERROR] Failed to process detail: {e}")
                                should_add = False
                                seen_goods.remove(gid)  # 재시도를 위해 제거
                                global_seen.remove(gid)
                                # 목록 페이지로 돌아가기
                                try:
                                    page.goto(product_url, wait_until="domcontentloaded")
                                    time.sleep(0.8)
                                except:
                                    pass
                        
                        # 실제로 추가할 상품만 추가
                        if should_add:
                            all_products.append(it)
                            actually_added_indices.append(idx + 1)
                            new_count += 1
                    
                    # 누락된 인덱스 확인 (실제로 추가된 것 기준)
                    expected_indices = set(range(1, len(items) + 1))
                    missing_indices = expected_indices - set(actually_added_indices)
                    
                    if missing_indices:
                        print(f"      [MISSING] Indices not processed: {sorted(missing_indices)}")
                        print(f"      [RETRY] Attempting to process {len(missing_indices)} missing items...")
                        
                        # 누락된 아이템 재시도
                        retry_count = 0
                        retry_failed = 0
                        retry_skipped = 0
                        for missing_idx in sorted(missing_indices):
                            if missing_idx - 1 < len(items):  # 인덱스 범위 체크
                                it = items[missing_idx - 1]
                                
                                # t_number 기반 고유 ID 생성
                                t_num = it.get("t_number", "")
                                goods_no = it.get("goodsNo", "")
                                
                                if t_num:
                                    gid = f"t_{t_num}"
                                elif goods_no:
                                    gid = f"g_{goods_no}"
                                else:
                                    gid = f"fb_{disp_cat_no}:{page_idx}:{missing_idx}"
                                
                                # 이미 추가된 상품인지 확인 (t_number 기준)
                                already_added = any(
                                    (p.get("t_number") and p.get("t_number") == t_num) or
                                    (p.get("goodsNo") and p.get("goodsNo") == goods_no)
                                    for p in all_products
                                )
                                
                                if already_added:
                                    print(f"      [RETRY {missing_idx}/{len(items)}] Already in products list, skipping...")
                                    retry_skipped += 1
                                    continue
                                
                                if it.get("detailUrl"):
                                    detail_url = it["detailUrl"]
                                    if not detail_url.startswith("http"):
                                        detail_url = "https://www.oliveyoung.co.kr" + detail_url
                                    
                                    print(f"      [RETRY {missing_idx}/{len(items)}] t_number={t_num} {it['name'][:30]}...")
                                    
                                    try:
                                        # 기본 정보 먼저 세팅
                                        it["first_category"] = primary
                                        it["mid_category"] = mid_name
                                        it["page_idx"] = page_idx
                                        
                                        # 상세 정보 크롤링 (옵션)
                                        if fetch_details:
                                            details = extract_product_details(page, detail_url, max_retries=2)
                                            
                                            # 상세 정보 추가
                                            it.update({
                                                "volume": details.get("volume", ""),
                                                "spec": details.get("spec", ""),
                                                "usage": details.get("usage", ""),
                                                "ingredients": details.get("ingredients", ""),
                                                "caution": details.get("caution", "")
                                            })
                                            
                                            if not any(details.values()):
                                                print(f"        [WARN] No details extracted")
                                        
                                        # 상품 추가 (상세 정보 없어도 추가)
                                        seen_goods.add(gid)
                                        global_seen.add(gid)
                                        all_products.append(it)
                                        retry_count += 1
                                        new_count += 1
                                        
                                        # 목록 페이지로 돌아가기
                                        page.goto(product_url, wait_until="domcontentloaded")
                                        time.sleep(0.8)
                                    except Exception as e:
                                        print(f"        [RETRY ERROR] {e}")
                                        retry_failed += 1
                                        # 목록 페이지로 돌아가기
                                        try:
                                            page.goto(product_url, wait_until="domcontentloaded")
                                            time.sleep(0.8)
                                        except:
                                            pass
                        
                        # 재시도 결과 출력 (무조건 출력)
                        print(f"      [RETRY RESULT] Success: {retry_count}, Failed: {retry_failed}, Skipped: {retry_skipped}, Total: {len(missing_indices)}")
                    
                    print(f"      [ADDED] {new_count} new products (total: {len(all_products)})")
                    
                    if new_count == 0:
                        break
                    
                    page_idx += 1
                    if page_idx > 100:
                        break
                    
                    time.sleep(throttle)

        context.close()
        browser.close()

    # 저장
    with open(out_path, "w", encoding="utf-8") as fw:
        json.dump(all_products, fw, ensure_ascii=False, indent=2)
    
    print(f"\n[DONE] {len(all_products)} products -> {out_path}")
    
    # t_number 통계 출력
    t_numbers = [p.get("t_number") for p in all_products if p.get("t_number")]
    if t_numbers:
        t_nums_int = [int(t) for t in t_numbers]
        print(f"[STATS] t_number range: {min(t_nums_int)} - {max(t_nums_int)}")
        print(f"[STATS] Unique t_numbers: {len(set(t_numbers))}")

if __name__ == "__main__":
    categories_path = "category/categories_test.json"
    out = "products.json"
    with open(categories_path, "r", encoding="utf-8") as f:
        categories = json.load(f)

    # 1단계: 일반 크롤링
    main(
        categories=categories,
        out_path=out,
        headless=True
    )
    
    # 2단계: 누락된 t_number 확인 및 복구 (선택사항)
    # 아래 주석을 해제하면 자동으로 누락된 상품을 복구합니다
    
    # missing = find_missing_t_numbers(out, expected_max=879)
    
    # if missing:
    #     recovered, failed, still_missing = crawl_missing_products_by_t_number(
    #         missing_t_numbers=missing,
    #         categories=categories,
    #         existing_json_path=out,
    #         output_path="products_recovered.json",
    #         headless=True,
    #         throttle=1.0,
    #         fetch_details=True
    #     )
        
    #     # 3단계: 병합
    #     complete_products = merge_json_files(
    #         original_path=out,
    #         recovered_path="products_recovered.json",
    #         output_path="products_complete.json"
    #     )
        
    #     print(f"\n[FINAL] Complete! Total products: {len(complete_products)}")