"""
KRX 투자자별 순매수 상위종목 -> Notion 데이터베이스 자동 업데이트 스크립트

필요한 환경변수 (GitHub Actions Secrets 또는 로컬 .env):
  NOTION_TOKEN         : Notion Integration의 Internal Integration Token
  NOTION_DATABASE_ID   : 데이터를 넣을 Notion 데이터베이스 ID

필요한 패키지: pip install pykrx requests
"""

import os
import csv
import datetime
from collections import Counter

import requests
from pykrx import stock

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
NOTION_VERSION = "2022-06-28"
NOTION_API_URL = "https://api.notion.com/v1/pages"

SECTOR_MAP_PATH = os.path.join(os.path.dirname(__file__), "sector_map.csv")
TOP_N = 5          # 각 카테고리별 상위 몇 개 종목을 보여줄지
SECTOR_TOP_N = 3   # 오늘의 섹터 태그를 몇 개까지 뽑을지


def load_sector_map():
    """ticker -> 섹터명 매핑표 로드"""
    mapping = {}
    with open(SECTOR_MAP_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mapping[row["ticker"]] = row["sector"]
    return mapping


def format_eok(value_won: int) -> str:
    """원 단위 숫자를 '+1,234억' 형태 문자열로 변환"""
    eok = round(value_won / 100_000_000)
    sign = "+" if eok >= 0 else ""
    return f"{sign}{eok:,}억"


def get_target_date() -> str:
    """가장 최근 영업일(YYYYMMDD)을 오늘 기준으로 계산"""
    today = datetime.datetime.now().strftime("%Y%m%d")
    return stock.get_nearest_business_day_in_a_week(today, prev=True)


def get_market_net_value(date: str, market: str) -> str:
    """해당 시장의 외국인/기관 순매수 대금 합계를 '외국인 / 기관' 문자열로 반환"""
    df = stock.get_market_trading_value_by_investor(date, date, market)
    foreign = df.loc["외국인합계", "순매수"] if "외국인합계" in df.index else 0
    inst = df.loc["기관합계", "순매수"] if "기관합계" in df.index else 0
    return f"{format_eok(foreign)} / {format_eok(inst)}"


def get_top10(date: str, market: str, investor: str):
    """
    투자자 유형별 순매수 상위 종목 리스트 반환.
    investor: '외국인' 또는 '기관합계'
    반환값: [(순위, 종목명, 순매수금액문자열, 티커), ...]
    """
    df = stock.get_market_net_purchases_of_equities_by_ticker(
        date, date, market=market, investor=investor
    )
    df = df.sort_values("순매수거래대금", ascending=False).head(TOP_N)

    results = []
    for i, (ticker, row) in enumerate(df.iterrows(), start=1):
        name = row["종목명"]
        amount = format_eok(row["순매수거래대금"])
        results.append((i, name, amount, ticker))
    return results


def top10_to_text(top10_list) -> str:
    """Notion Rich text(여러 줄)로 넣을 문자열 생성"""
    lines = [f"{rank}. {name}  {amount}" for rank, name, amount, _ in top10_list]
    return "\n".join(lines)


def guess_sectors(all_top10_lists, sector_map) -> list:
    """오늘 등장한 TOP10 종목들의 섹터를 집계해서 가장 많이 나온 섹터 상위 N개 반환"""
    counter = Counter()
    for top10_list in all_top10_lists:
        for _, _, _, ticker in top10_list:
            sector = sector_map.get(ticker)
            if sector:
                counter[sector] += 1
    return [sector for sector, _ in counter.most_common(SECTOR_TOP_N)]


def build_rich_text(content: str):
    return [{"type": "text", "text": {"content": content}}]


def push_to_notion(date_str, sectors, kospi_value, kospi_foreign, kospi_inst,
                    kosdaq_value, kosdaq_foreign, kosdaq_inst):
    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "날짜": {"date": {"start": date_str}},
            "섹터": {"multi_select": [{"name": s} for s in sectors]},
            "코스피 순매수 금액": {"rich_text": build_rich_text(kospi_value)},
            "코스피 외국인 TOP10": {"rich_text": build_rich_text(kospi_foreign)},
            "코스피 기관 TOP10": {"rich_text": build_rich_text(kospi_inst)},
            "코스닥 순매수 금액": {"rich_text": build_rich_text(kosdaq_value)},
            "코스닥 외국인 TOP10": {"rich_text": build_rich_text(kosdaq_foreign)},
            "코스닥 기관 TOP10": {"rich_text": build_rich_text(kosdaq_inst)},
        },
    }

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    resp = requests.post(NOTION_API_URL, headers=headers, json=payload)
    resp.raise_for_status()
    print("Notion 페이지 생성 완료:", resp.json().get("id"))


def main():
    date = get_target_date()
    date_str = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    sector_map = load_sector_map()

    kospi_foreign_top10 = get_top10(date, "KOSPI", "외국인")
    kospi_inst_top10 = get_top10(date, "KOSPI", "기관합계")
    kosdaq_foreign_top10 = get_top10(date, "KOSDAQ", "외국인")
    kosdaq_inst_top10 = get_top10(date, "KOSDAQ", "기관합계")

    kospi_value = get_market_net_value(date, "KOSPI")
    kosdaq_value = get_market_net_value(date, "KOSDAQ")

    sectors = guess_sectors(
        [kospi_foreign_top10, kospi_inst_top10, kosdaq_foreign_top10, kosdaq_inst_top10],
        sector_map,
    )

    push_to_notion(
        date_str=date_str,
        sectors=sectors,
        kospi_value=kospi_value,
        kospi_foreign=top10_to_text(kospi_foreign_top10),
        kospi_inst=top10_to_text(kospi_inst_top10),
        kosdaq_value=kosdaq_value,
        kosdaq_foreign=top10_to_text(kosdaq_foreign_top10),
        kosdaq_inst=top10_to_text(kosdaq_inst_top10),
    )


if __name__ == "__main__":
    main()
