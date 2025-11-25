import requests
from typing import List, Dict, Any
from prompt import WEB_RAG_PROMPT
from openai import OpenAI


class Retriever:
    def __init__(self, api_key, top_k):
        self.api_key = api_key
        self.endpoint = "https://api.tavily.com/search"
        self.top_k = top_k 

    def search(self, query):
        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",   # "basic" 또는 "advanced"
            "max_results": self.top_k,
            "include_answer": False,
            "include_images": False,
        }

        resp = requests.post(self.endpoint, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        # Tavily 응답 형식: {"results": [{title, url, content, ...}, ...], ...}
        results = data.get("results", [])
        documents: List[Dict[str, Any]] = []

        for r in results[: self.top_k]:
            doc = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", r.get("snippet", "")),
            }
            documents.append(doc)

        return documents

    def WebRetrieve(self, query):
        search_query = query + " site:oliveyoung.co.kr 상품 구매 후기"
        documents = self.search(search_query)
        return documents


class Generator: 
    def __init__(self, api_key, model, max_token, temperature):
        self.api_key = api_key
        self.model = model
        self.max_token = max_token
        self.temperature = temperature
        self.client = OpenAI(api_key=api_key)

    def get_prompt(self, query, documents):
        doc_blocks = []
        for idx, doc in enumerate(documents, start=1):
            title = doc.get("title", "")
            url = doc.get("url", "")
            content = doc.get("content", "")
            block = (
                f"[문서 {idx}]\n"
                f"제목: {title}\n"
                f"URL: {url}\n"
                f"내용: {content}\n"
            )
            doc_blocks.append(block)

        documents_text = "\n\n".join(doc_blocks)
        prompt = WEB_RAG_PROMPT.format(
            query=query,
            documents=documents_text
        )
        return prompt

    def Generate(self, query, documents):
        prompt = self.get_prompt(query, documents)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": """
- 반드시 실제 올리브영에서 판매 중인 제품명만 추천해야 한다.
- 제품명은 일반 표현이 아니라 정확한 브랜드 + 제품명으로 작성한다.
"""
                },
                {
                    "role": "user",
                    "content": prompt
                },
            ],
            max_tokens=self.max_token,
            temperature=self.temperature,
        )

        generation = response.choices[0].message.content
        return generation
