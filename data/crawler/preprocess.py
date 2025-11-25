import json
import re
from pathlib import Path


SKIN_TYPES = [
    "건성",
    "지성",
    "복합성",
    "중성",
    "민감성",
    "여드름성",
    "수분 부족형 지성",
    "노화",
    "모든 타입",
]

def extract_skin_types_from_spec(spec_text: str) -> list:
    if not spec_text:
        return []

    text = spec_text.replace(" ", "")

    found = []

    mapping = {
        "건성": ["건성"],
        "지성": ["지성"],
        "복합": ["복합성"],
        "중성": ["중성"],
        "민감": ["민감성"],
        "여드름": ["여드름성"],
        "노화": ["노화"],
        "수부지": ["수분 부족형 지성"],
        "수분부족": ["수분 부족형 지성"],
        "모든": ["모든 타입"],
        "전피부": ["모든 타입"],
        "모든피부": ["모든 타입"],
        "모든타입": ["모든 타입"],
    }

    for key, values in mapping.items():
        if key in text:
            for v in values:
                if v not in found:
                    found.append(v)

    return found


def generate_product_id(index: int):
    return f"prod_{str(index).zfill(3)}"


def parse_volume(text: str):
    if not text:
        return None

    s = str(text).lower()
    total = 0.0

    # ml 또는 g 만 인정
    for match in re.finditer(r'(\d+(?:\.\d+)?)\s*(ml|mℓ|㎖|g)', s, flags=re.I):
        amount = float(match.group(1))

        tail = s[match.end(): match.end() + 6]
        mul_match = re.search(r'\*(\d+)', tail)
        mul = int(mul_match.group(1)) if mul_match else 1

        total += amount * mul

    if total == 0:
        return None

    return int(total) if total.is_integer() else total


def is_empty_product(item: dict) -> bool:
    check_keys = ["volume", "spec", "usage", "ingredients", "caution"]

    for key in check_keys:
        value = item.get(key)
        if value not in ("", None):
            return False
    return True


def parse_price(value):
    if value is None:
        return None

    s = str(value)

    # 숫자 이외 문자 제거 (콤마, 원, 공백 등)
    s = re.sub(r"[^\d]", "", s)

    if s == "":
        return None

    return int(s)


def main(input_path):
    with Path(input_path).open(encoding="utf-8") as f:
        data = json.load(f)

    print("원본 상품 개수:", len(data))

    cleaned_data = []

    for item in data:
        item.pop("page_idx", None)
        item.pop("t_number", None)
        item.pop("goodsNo", None)
        item.pop("dispCatNo", None)

        if "first_category" in item and isinstance(item["first_category"], str):
            item["first_category"] = item["first_category"].replace("대_", "", 1)

        item["price_org"] = parse_price(item.get("price_org"))
        item["price_cur"] = parse_price(item.get("price_cur"))

        raw_volume = item.get("volume")
        parsed_volume = parse_volume(raw_volume)
        item["volume"] = parsed_volume

        if parsed_volume is None:
            continue

        # ✅ 여기 추가됨
        spec_text = item.get("spec", "")
        skin_types = extract_skin_types_from_spec(spec_text)
        if skin_types:
            item["spec"] = skin_types

        if is_empty_product(item):
            continue

        cleaned_data.append(item)

    for idx, item in enumerate(cleaned_data, start=1):
        item["product_id"] = f"prod_{str(idx).zfill(3)}"

    output_path = Path("product_clean.json")
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(cleaned_data, f, ensure_ascii=False, indent=2)

    print("정리 후 상품 개수:", len(cleaned_data))
    print("삭제된 상품 수:", len(data) - len(cleaned_data))
    print("저장 위치:", output_path)


if __name__ == "__main__":
    main("/Users/bigeco/Documents/GitHub/temi_recommender/data/crawler/product/data_5/product.json")
