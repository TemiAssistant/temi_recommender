import json
import re
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Set
from urllib.parse import urlencode, quote
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError


# ================== 설정 ==================
@dataclass
class CrawlerConfig:
    base_url: str = "https://www.oliveyoung.co.kr"
    product_list_url: str = "https://www.oliveyoung.co.kr/store/display/getMCategoryList.do"
    headless: bool = True
    throttle: float = 1.0
    fetch_details: bool = True
    max_retries: int = 3
    page_timeout: int = 30000
    limit: int = 5


# ================== 데이터 모델 ==================
@dataclass
class ProductDetails:
    volume: str = ""
    spec: str = ""
    usage: str = ""
    ingredients: str = ""
    caution: str = ""


@dataclass
class Product:
    goods_no: str
    disp_cat_no: str
    image: str
    brand: str
    name: str
    detail_url: str
    t_number: str
    price_org: str
    price_cur: str
    first_category: str = ""
    mid_category: str = ""
    page_idx: int = 0
    details: Optional[ProductDetails] = None
    
    def to_dict(self) -> Dict:
        result = asdict(self)
        if self.details:
            result.update(asdict(self.details))
            del result['details']
        return result


# ================== 유틸리티 ==================
class TextNormalizer:
    @staticmethod
    def normalize(text: str) -> str: # 카테고리명 비교용 정규화
        if not text:
            return ""
        text = text.strip()
        text = re.sub(r'[\s_\-/\\·・]', '', text)
        return text.casefold()
    
    @staticmethod
    def sanitize_filename(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', '_', name)
    
    @staticmethod
    def extract_number(text: str) -> str:
        return re.sub(r'[^\d]', '', text or '')
    
    @staticmethod
    def extract_t_number(url: str) -> str:
        if not url:
            return ""
        match = re.search(r't_number=(\d+)', url)
        return match.group(1) if match else ""


# ================== 브라우저 관리 ==================
class BrowserManager:
    def __init__(self, config: CrawlerConfig):
        self.config = config
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.config.headless,
            args=['--disable-blink-features=AutomationControlled']
        )
        self.context = self.browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080}
        )
        self.page = self.context.new_page()
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        return self.page
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()


# ================== 페이지 네비게이션 ==================
class PageNavigator:
    def __init__(self, page: Page, config: CrawlerConfig):
        self.page = page
        self.config = config
    
    def navigate(self, url: str): # 페이지 이동 및 로딩 대기
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_timeout)
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeoutError:
            pass
        self._trigger_dynamic_content()
    
    def _trigger_dynamic_content(self):
        try:
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.3)
            self.page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.2)
        except Exception:
            pass


# ================== 카테고리 수집 ==================
class CategoryCollector:
    def __init__(self, page: Page, navigator: PageNavigator):
        self.page = page
        self.navigator = navigator
        self.normalizer = TextNormalizer()
    
    def find_disp_cat_no(self, category_url: str, target_mid_name: str) -> Optional[str]: # 중분류 카테고리의 dispCatNo 찾기
        self.navigator.navigate(category_url)
        time.sleep(1.5)
        
        mid_links = self.page.query_selector_all('a[href*="moveCategory"]')
        
        for link in mid_links:
            text = link.text_content().strip()
            href = link.get_attribute('href') or ''
            
            # 불필요한 링크 필터링
            if any(kw in text for kw in ['원', '세일', 'ml', 'ML', '기획']):
                continue
            
            # 카테고리명 매칭
            if (self.normalizer.normalize(text) == self.normalizer.normalize(target_mid_name) 
                or text == target_mid_name):
                match = re.search(r"moveCategory\('(\d+)'", href)
                if match:
                    return match.group(1)
        
        return None


# ================== 상품 파싱 ==================
class ProductParser:
    def __init__(self, page: Page):
        self.page = page
        self.normalizer = TextNormalizer()
    
    def parse_product_list(self) -> List[Dict]:
        """상품 목록 페이지에서 상품 정보 추출"""
        items = self.page.eval_on_selector_all(
            ".prd_info",
            """
            els => els.map(root => {
              const aThumb = root.querySelector('a.prd_thumb') || root.querySelector('a[name$="_Small"]');
              const img = aThumb ? aThumb.querySelector('img') : root.querySelector('img');
              
              const nameWrap = root.querySelector('.prd_name');
              const nameLink = nameWrap ? nameWrap.querySelector('a') : null;
              const brand = nameWrap ? (nameWrap.querySelector('.tx_brand')?.textContent || '').trim() : '';
              const name = nameWrap ? (nameWrap.querySelector('.tx_name')?.textContent || '').trim() : '';
              const detailUrl = nameLink ? (nameLink.getAttribute('href') || '').trim() : '';
              
              const priceWrap = root.querySelector('.prd_price');
              const orgNode = priceWrap ? priceWrap.querySelector('.tx_org .tx_num') : null;
              const curNode = priceWrap ? priceWrap.querySelector('.tx_cur .tx_num') : null;
              
              const goodsNo = (aThumb || nameLink) ? 
                ((aThumb ? aThumb.getAttribute('data-ref-goodsno') : '') || 
                 (nameLink ? nameLink.getAttribute('data-ref-goodsno') : '')) : '';
              const dispCat = (aThumb || nameLink) ? 
                ((aThumb ? aThumb.getAttribute('data-ref-dispcatno') : '') || 
                 (nameLink ? nameLink.getAttribute('data-ref-dispcatno') : '')) : '';
              
              return {
                goodsNo, dispCatNo: dispCat, image: img ? (img.getAttribute('src') || '').trim() : '',
                brand, name, detailUrl,
                price_org_raw: orgNode ? orgNode.textContent : '',
                price_cur_raw: curNode ? curNode.textContent : ''
              };
            })
            """
        )
        
        return [self._convert_to_product(item) for item in items]
    
    def _convert_to_product(self, raw_item: Dict) -> Product:
        """원시 데이터를 Product 객체로 변환"""
        price_org = self.normalizer.extract_number(raw_item.get('price_org_raw', ''))
        price_cur = self.normalizer.extract_number(raw_item.get('price_cur_raw', ''))
        
        # 원가가 없으면 현재가를 원가로
        if not price_org and price_cur:
            price_org = price_cur
        
        t_number = self.normalizer.extract_t_number(raw_item.get('detailUrl', ''))
        
        return Product(
            goods_no=raw_item.get('goodsNo', ''),
            disp_cat_no=raw_item.get('dispCatNo', ''),
            image=raw_item.get('image', ''),
            brand=raw_item.get('brand', ''),
            name=raw_item.get('name', ''),
            detail_url=raw_item.get('detailUrl', ''),
            t_number=t_number,
            price_org=price_org,
            price_cur=price_cur
        )
    
    def parse_product_details(self, product_url: str, max_retries: int = 3) -> ProductDetails:
        """상품 상세 페이지에서 구매정보 추출"""
        details = ProductDetails()
        
        for attempt in range(max_retries):
            try:
                self.page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(1.5)
                
                # 구매정보 탭 클릭
                buy_info_button = (
                    self.page.query_selector('li#buyInfo a.goods_buyinfo') or
                    self.page.query_selector('a.goods_buyinfo') or
                    self.page.query_selector('li#buyInfo a') or
                    self.page.query_selector('a:has-text("구매정보")')
                )
                if buy_info_button:
                    buy_info_button.click()
                    # Ajax 로딩 기다리기
                    self.page.wait_for_selector('#artcInfo dl.detail_info_list', timeout=10000)
                else:
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")

                # 정보 추출
                info_lists = self.page.query_selector_all('dl.detail_info_list')

                for dl in info_lists:
                    dt = dl.query_selector('dt')
                    dd = dl.query_selector('dd')
                    if not dt or not dd:
                        continue

                    key = dt.text_content().strip()
                    value = dd.text_content().strip()

                    if "내용물의 용량" in key or "중량" in key:
                        details.volume = value
                    elif "제품 주요 사양" in key:
                        details.spec = value
                    elif "사용방법" in key:
                        details.usage = value
                    elif "성분" in key and "화장품법" in key:
                        details.ingredients = value
                    elif "주의사항" in key:
                        details.caution = value
                
                if any([details.volume, details.spec, details.usage, 
                       details.ingredients, details.caution]):
                    break
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1.0)
                continue
        
        return details


# ================== URL 빌더 ==================
class URLBuilder:
    @staticmethod
    def build_category_url(first_category: str) -> str:
        """대분류 카테고리 URL 생성"""
        params = {
            "dispCatNo": "10000010001",
            "gateCd": "Drawer",
            "t_page": "드로우_카테고리",
            "t_click": "카테고리탭_대카테고리",
            "t_1st_category_type": first_category
        }
        base = "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do"
        return f"{base}?{urlencode(params, encoding='utf-8', quote_via=quote)}"
    
    @staticmethod
    def build_product_list_url(first_key: str, mid_name: str, 
                               page_idx: int, disp_cat_no: str) -> str:
        """상품 목록 URL 생성"""
        params = {
            "dispCatNo": disp_cat_no,
            "fltDispCatNo": "",
            "prdSort": "01",
            "pageIdx": str(page_idx),
            "rowsPerPage": "24",
            "searchTypeSort": "btn_thumb",
            "plusButtonFlag": "N",
            "trackingCd": f"Cat{disp_cat_no}_Small",
            "t_page": "카테고리관",
            "t_click": "카테고리상세_중카테고리",
            "t_1st_category_type": first_key,
            "t_2nd_category_type": f"중_{mid_name}",
        }
        base = "https://www.oliveyoung.co.kr/store/display/getMCategoryList.do"
        return f"{base}?{urlencode(params, encoding='utf-8', quote_via=quote)}"


# ================== 메인 크롤러 ==================
class OliveYoungCrawler:
    def __init__(self, config: CrawlerConfig = None):
        self.config = config or CrawlerConfig()
        self.seen_products: Set[str] = set()
        self.all_products: List[Product] = []
    
    def crawl(self, categories: Dict[str, List[str]]) -> List[Dict]:
        """카테고리별 상품 크롤링"""
        with BrowserManager(self.config) as page:
            navigator = PageNavigator(page, self.config)
            category_collector = CategoryCollector(page, navigator)
            parser = ProductParser(page)
            
            for primary, mids in categories.items():
                print(f"\n[PRIMARY] {primary}")
                self._crawl_primary_category(
                    page, navigator, category_collector, parser,
                    primary, mids
                )
        
        return [p.to_dict() for p in self.all_products]
    
    def _crawl_primary_category(self, page, navigator, category_collector, 
                                parser, primary: str, mids: List[str]):
        """대분류 카테고리 크롤링"""
        for mid_name in mids:
            print(f"  [MID] {mid_name}")
            
            # dispCatNo 찾기
            category_url = URLBuilder.build_category_url(primary)
            disp_cat_no = category_collector.find_disp_cat_no(category_url, mid_name)
            
            if not disp_cat_no:
                print(f"    [ERROR] Could not find dispCatNo for {mid_name}")
                continue
            
            print(f"    [FOUND] dispCatNo={disp_cat_no}")
            
            # 상품 목록 크롤링
            self._crawl_mid_category(
                page, navigator, parser, primary, mid_name, disp_cat_no
            )
    
    def _crawl_mid_category(self, page, navigator, parser, 
                           primary: str, mid_name: str, disp_cat_no: str):
        """중분류 카테고리 상품 크롤링"""
        page_idx = 1
        
        while page_idx <= 100:  # 안전장치
            product_url = URLBuilder.build_product_list_url(
                primary, mid_name, page_idx, disp_cat_no
            )
            
            print(f"    [PAGE {page_idx}]")
            navigator.navigate(product_url)
            time.sleep(1.5)
            
            # 상품 개수 확인
            count = page.locator('.prd_info').count()
            print(f"      [COUNT] {count} products")
            
            if count == 0:
                break
            
            # 상품 파싱
            products = parser.parse_product_list()
            new_count = self._process_products(
                page, parser, products, primary, mid_name, page_idx, product_url
            )
            
            print(f"      [ADDED] {new_count} new products (total: {len(self.all_products)})")
            
            if new_count == 0:
                break
            
            page_idx += 1
            time.sleep(self.config.throttle)
    
    def _process_products(self, page, parser, products: List[Product], 
                         primary: str, mid_name: str, page_idx: int,
                         list_url: str) -> int:
        """상품 목록 처리 및 상세 정보 수집"""
        new_count = 0
        
        for idx, product in enumerate(products):
            if len(self.all_products) >= self.config.limit:
                return new_count
                
            # 중복 체크
            product_id = self._get_product_id(product)
            if product_id in self.seen_products:
                continue
            
            self.seen_products.add(product_id)
            
            # 카테고리 정보 설정
            product.first_category = primary
            product.mid_category = mid_name
            product.page_idx = page_idx
            
            # 상세 정보 크롤링
            if self.config.fetch_details and product.detail_url:
                detail_url = self._normalize_url(product.detail_url)
                print(f"      [DETAIL {idx+1}/{len(products)}] t_number={product.t_number} {product.name[:30]}...")
                
                try:
                    product.details = parser.parse_product_details(detail_url)
                    
                    if not any(asdict(product.details).values()):
                        print(f"        [WARN] No details extracted")
                        continue
                    
                    # 목록 페이지로 복귀
                    page.goto(list_url, wait_until="domcontentloaded")
                    time.sleep(0.8)
                    
                except Exception as e:
                    print(f"        [ERROR] {e}")
                    self.seen_products.remove(product_id)
                    continue
            
            self.all_products.append(product)
            new_count += 1
        
        return new_count
    
    def _get_product_id(self, product: Product) -> str:
        """상품 고유 ID 생성"""
        if product.t_number:
            return f"t_{product.t_number}"
        elif product.goods_no:
            return f"g_{product.goods_no}"
        else:
            return f"fb_{product.disp_cat_no}:{product.detail_url}"
    
    def _normalize_url(self, url: str) -> str:
        """URL 정규화"""
        if not url.startswith("http"):
            return self.config.base_url + url
        return url
    
    def save_results(self, output_path: str):
        """결과를 JSON 파일로 저장"""
        products_dict = [p.to_dict() for p in self.all_products]
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(products_dict, f, ensure_ascii=False, indent=2)
        
        print(f"\n[DONE] {len(self.all_products)} products -> {output_path}")
        self._print_statistics()
    
    def _print_statistics(self):
        """수집 통계 출력"""
        t_numbers = [int(p.t_number) for p in self.all_products if p.t_number]
        
        if t_numbers:
            print(f"[STATS] t_number range: {min(t_numbers)} - {max(t_numbers)}")
            print(f"[STATS] Unique t_numbers: {len(set(t_numbers))}")


if __name__ == "__main__":
    # 설정
    config = CrawlerConfig(
        headless=True,
        throttle=1.0,
        fetch_details=True,
        limit=5
    )
    
    # 카테고리 로드
    with open("category/categories_test.json", "r", encoding="utf-8") as f:
        categories = json.load(f)
    
    # 크롤링 실행
    crawler = OliveYoungCrawler(config)
    crawler.crawl(categories)
    crawler.save_results("products.json")
