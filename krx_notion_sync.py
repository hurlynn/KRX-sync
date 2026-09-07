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


def get_candidate_dates(max_back: int = 10):
    """
    오늘부터 최대 max_back일 전까지, 주말을 제외한 날짜(YYYYMMDD) 목록을
    최신순으로 반환한다. (pykrx의 get_nearest_business_day_in_a_week는
    KRX 서버 응답이 불안정할 때 자주 오류가 나서 사용하지 않는다.)
    """
    dates = []
    d = datetime.datetime.now()
    while len(dates) < max_back:
        if d.weekday() < 5:  # 0=월 ... 4=금, 주말(5,6) 제외
            dates.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    return dates


def fetch_with_retry(fetch_fn, dates):
    """
    dates(최신순) 목록을 하나씩 시도하면서 fetch_fn(date)이 비어있지 않은
    결과를 낼 때까지 뒤로 물러난다. 공휴일 등으로 데이터가 없는 날은
    건너뛴다. 성공하면 (date, 결과)를 반환한다.
    """
    last_err = None
    for date in dates:
        try:
            result = fetch_fn(date)
            if result is not None and len(result) > 0:
                return date, result
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"최근 {len(dates)}일 내에 유효한 데이터를 찾지 못했습니다. 마지막 에러: {last_err}")


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
    if resp.status_code >= 400:
        print("Notion API 에러 상세 내용:")
        print(resp.text)
    resp.raise_for_status()
    print("Notion 페이지 생성 완료:", resp.json().get("id"))


def main():
    sector_map = load_sector_map()
    candidate_dates = get_candidate_dates()

    print(f"시도할 날짜 후보: {candidate_dates}")
    date, kospi_foreign_top10 = fetch_with_retry(
        lambda d: get_top10(d, "KOSPI", "외국인"), candidate_dates
    )
    print(f"사용할 날짜로 확정: {date}")
    date_str = f"{date[:4]}-{date[4:6]}-{date[6:]}"

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
