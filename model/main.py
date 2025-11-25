import json
from module import Retriever, Generator


if __name__ == "__main__":
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    retriever = Retriever(**config["retriever"])
    generator = Generator(**config["generator"])

    # 테스트용
    query = "올리브영 제품 중에서 건성 피부에 좋은 제품 추천해줘"
    documents = retriever.WebRetrieve(query)
    answer = generator.Generate(query, documents)
    print("=== 최종 답변 ===")
    print(answer)