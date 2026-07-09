from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
import json
import re
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from django.db.models import Max, Sum

from .models import (
    Account,
    AccountSnapshot,
    CashFlow,
    HoldingSnapshot,
    InstrumentAlias,
    PriceHistory,
    RebalanceCategory,
    RebalanceStock,
    Stock,
)


@dataclass
class RebalanceItem:
    category: str
    current_value: int
    target_value: int
    gap_value: int


@dataclass
class GroupTradeRecommendation:
    action: str
    account_name: str
    category: str
    ticker: str
    stock_name: str
    price: int
    shares: int
    amount: int


def _collect_krx_tickers(pykrx_stock) -> set[str]:
    tickers: set[str] = set()
    for market in ("KOSPI", "KOSDAQ", "KONEX", "ETF"):
        try:
            tickers.update(pykrx_stock.get_market_ticker_list(market=market))
        except Exception:
            continue
    return tickers


def _lookup_naver_name_by_ticker(ticker: str) -> str | None:
    # 네이버 금융 종목 페이지는 6자리 숫자 티커를 사용한다.
    if not re.fullmatch(r"\d{6}", ticker):
        return None
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read()
    except (URLError, TimeoutError, ValueError):
        return None
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("euc-kr", errors="ignore")

    match = re.search(r"<title>\s*(.*?)\s*:\s*(?:Npay|네이버페이)\s*증권\s*</title>", html, re.IGNORECASE)
    if not match:
        return None
    name = unescape(match.group(1)).strip()
    return name or None


def _fetch_pykrx_close_by_ticker(pykrx_stock, ticker: str, price_date: date) -> Decimal | None:
    start_date = (price_date - timedelta(days=10)).strftime("%Y%m%d")
    end_date = price_date.strftime("%Y%m%d")
    try:
        ohlcv = pykrx_stock.get_market_ohlcv_by_date(
            fromdate=start_date,
            todate=end_date,
            ticker=ticker,
        )
    except Exception:
        return None
    if ohlcv.empty:
        return None
    return Decimal(str(ohlcv.iloc[-1]["종가"])).quantize(Decimal("0.0001"))


def _fetch_naver_close_by_symbol(symbol: str, price_date: date) -> Decimal | None:
    if not re.fullmatch(r"\d{6}", symbol):
        return None
    start_date = (price_date - timedelta(days=15)).strftime("%Y%m%d")
    end_date = price_date.strftime("%Y%m%d")
    url = (
        "https://api.finance.naver.com/siseJson.naver"
        f"?symbol={symbol}&requestType=1&startTime={start_date}&endTime={end_date}&timeframe=day"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload_text = response.read().decode("utf-8", errors="ignore").strip()
        payload = ast.literal_eval(payload_text)
    except Exception:
        return None
    if not isinstance(payload, list) or len(payload) <= 1:
        return None
    rows = payload[1:]
    if not rows:
        return None
    last_row = rows[-1]
    if not isinstance(last_row, (list, tuple)) or len(last_row) < 5:
        return None
    close_price = last_row[4]
    try:
        return Decimal(str(close_price)).quantize(Decimal("0.0001"))
    except (TypeError, ValueError):
        return None


def _fetch_yahoo_close_by_symbol(symbol: str, price_date: date) -> Decimal | None:
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-=]{0,19}", symbol):
        return None
    start_ts = int(datetime.combine(price_date - timedelta(days=10), datetime.min.time()).timestamp())
    end_ts = int(datetime.combine(price_date + timedelta(days=1), datetime.min.time()).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start_ts}&period2={end_ts}&interval=1d"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    valid_closes = [value for value in closes if value is not None]
    if not valid_closes:
        return None
    return Decimal(str(valid_closes[-1])).quantize(Decimal("0.0001"))


def _fetch_naver_realtime_price(symbol: str) -> Decimal | None:
    # 네이버 실시간 API: SERVICE_ITEM:{symbol}
    query_symbol = symbol.strip().upper()
    if not query_symbol:
        return None
    url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{query_symbol}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    areas = ((payload.get("result") or {}).get("areas") or [])
    datas = (areas[0].get("datas") if areas else []) or []
    if not datas:
        return None
    current_price = datas[0].get("nv")
    try:
        return Decimal(str(current_price)).quantize(Decimal("0.0001"))
    except Exception:
        return None


def _fetch_yahoo_realtime_price(symbol: str) -> Decimal | None:
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-=]{0,19}", symbol):
        return None
    end_ts = int(datetime.now(tz=ZoneInfo("UTC")).timestamp())
    start_ts = end_ts - 3600 * 24 * 5
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start_ts}&period2={end_ts}&interval=1m"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    valid_closes = [value for value in closes if value is not None]
    if not valid_closes:
        return None
    return Decimal(str(valid_closes[-1])).quantize(Decimal("0.0001"))


def previous_business_day(base_date: date) -> date:
    candidate = base_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def resolve_price_board_dates(now_kst: datetime | None = None) -> tuple[date, date, bool]:
    current = now_kst or datetime.now(tz=ZoneInfo("Asia/Seoul"))
    today_kst = current.date()
    market_open = (
        current.weekday() < 5
        and (current.hour > 9 or (current.hour == 9 and current.minute >= 0))
        and (current.hour < 15 or (current.hour == 15 and current.minute < 30))
    )
    display_date = today_kst if market_open else previous_business_day(today_kst)
    prev_date = previous_business_day(display_date)
    return display_date, prev_date, market_open


def _resolve_close_for_stock(
    stock: Stock,
    alias: InstrumentAlias | None,
    price_date: date,
    pykrx_stock=None,
) -> tuple[int | None, str]:
    krx_code = (
        (alias.krx_ticker.strip() if alias and alias.krx_ticker else "")
        or (stock.ticker if stock.ticker and stock.ticker[0].isdigit() else "")
    )
    naver_symbol = (
        (alias.naver_symbol.strip() if alias and alias.naver_symbol else "")
        or krx_code
    )
    yahoo_symbol = (alias.yahoo_symbol.strip().upper() if alias and alias.yahoo_symbol else "")

    if krx_code:
        close_price = _fetch_pykrx_close_by_ticker(pykrx_stock, krx_code, price_date)
        if close_price is not None:
            return close_price, "pykrx"
    if naver_symbol:
        close_price = _fetch_naver_close_by_symbol(naver_symbol, price_date)
        if close_price is not None:
            return close_price, "naver"
    yahoo_candidate = yahoo_symbol or (
        stock.ticker if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", stock.ticker) else ""
    )
    if yahoo_candidate:
        close_price = _fetch_yahoo_close_by_symbol(yahoo_candidate, price_date)
        if close_price is not None:
            return close_price, "yahoo"
    return None, ""


def _lookup_yahoo_name_by_symbol(symbol: str) -> str | None:
    # 미국 주식/ETF 심볼(예: AGNC, NVDA, BRK-B)에 대응한다.
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        return None
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={symbol}&quotesCount=10&newsCount=0"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None

    quotes = payload.get("quotes") or []
    if not quotes:
        return None
    symbol_upper = symbol.upper()
    for row in quotes:
        if (row or {}).get("symbol", "").upper() != symbol_upper:
            continue
        candidate = ((row or {}).get("longname") or (row or {}).get("shortname") or "").strip()
        if candidate:
            return candidate
    return None


def lookup_stock_name_by_ticker(
    ticker: str,
    *,
    pykrx_stock=None,
    valid_tickers: set[str] | None = None,
    prefer_existing: bool = True,
) -> str | None:
    ticker = ticker.strip().upper()
    if not ticker:
        return None

    if prefer_existing:
        existing_name = Stock.objects.filter(ticker=ticker).values_list("name", flat=True).first()
        if existing_name:
            return existing_name

    yahoo_name = _lookup_yahoo_name_by_symbol(ticker)
    if yahoo_name:
        return yahoo_name

    # pykrx 이름 조회는 6자리 숫자 티커에 대해서만 신뢰한다.
    if not re.fullmatch(r"\d{6}", ticker):
        return None

    stock_module = pykrx_stock
    if stock_module is None:
        try:
            from pykrx import stock as stock_module
        except ImportError:
            return None

    known_tickers = valid_tickers if valid_tickers is not None else _collect_krx_tickers(stock_module)
    if known_tickers and ticker not in known_tickers:
        return None

    try:
        fetched_name = stock_module.get_market_ticker_name(ticker)
    except Exception:
        fetched_name = None

    if isinstance(fetched_name, str):
        fetched_name = fetched_name.strip()
        if fetched_name:
            return fetched_name

    return _lookup_naver_name_by_ticker(ticker)


def latest_holdings_by_account() -> list[HoldingSnapshot]:
    latest_dates = (
        HoldingSnapshot.objects.values("account_id", "stock_id").annotate(latest_date=Max("as_of_date"))
    )
    result = []
    for row in latest_dates:
        snapshot = HoldingSnapshot.objects.get(
            account_id=row["account_id"],
            stock_id=row["stock_id"],
            as_of_date=row["latest_date"],
        )
        result.append(snapshot)
    return result


def latest_cash_by_account() -> dict[int, int]:
    latest_dates = AccountSnapshot.objects.values("account_id").annotate(latest_date=Max("as_of_date"))
    balances: dict[int, int] = {}
    for row in latest_dates:
        snapshot = AccountSnapshot.objects.get(
            account_id=row["account_id"],
            as_of_date=row["latest_date"],
        )
        balances[row["account_id"]] = snapshot.cash_balance
    return balances


def latest_price_map() -> dict[str, Decimal]:
    latest_dates = PriceHistory.objects.values("stock_id").annotate(latest_date=Max("price_date"))
    data: dict[str, Decimal] = {}
    for row in latest_dates:
        price_row = PriceHistory.objects.get(stock_id=row["stock_id"], price_date=row["latest_date"])
        data[price_row.stock.ticker] = Decimal(price_row.close_price)
    return data


def _is_usd_ticker(ticker: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", ticker))


def _fetch_usdkrw_rate(price_date: date) -> Decimal | None:
    # Yahoo KRW=X 는 USD/KRW 환율(원/달러)을 제공한다.
    rate = _fetch_yahoo_close_by_symbol("KRW=X", price_date)
    if rate is None or rate <= 0:
        return None
    return rate


def portfolio_totals() -> dict[str, int | Decimal]:
    holdings = latest_holdings_by_account()
    cash_map = latest_cash_by_account()
    cash_total = sum(cash_map.values())
    market_total = sum(row.market_value for row in holdings)
    invested_total = sum(row.invested_value for row in holdings)
    total_assets = cash_total + market_total
    initial_total = Account.objects.aggregate(total=Sum("initial_balance"))["total"] or 0
    deposit_total = (
        CashFlow.objects.filter(flow_type=CashFlow.DEPOSIT).aggregate(total=Sum("amount"))["total"] or 0
    )
    withdraw_total = (
        CashFlow.objects.filter(flow_type=CashFlow.WITHDRAW).aggregate(total=Sum("amount"))["total"] or 0
    )
    investment_total = initial_total + deposit_total - withdraw_total

    profit = total_assets - investment_total
    return_rate = Decimal("0.00")
    if investment_total > 0:
        return_rate = Decimal(profit * 100 / investment_total).quantize(Decimal("0.01"))
    return {
        "investment_total": investment_total,
        "cash_total": cash_total,
        "market_total": market_total,
        "invested_total": invested_total,
        "total_assets": total_assets,
        "profit": profit,
        "return_rate": return_rate,
    }


def category_breakdown() -> list[dict[str, Decimal | int | str]]:
    holdings = latest_holdings_by_account()
    totals = portfolio_totals()
    category_values: dict[str, int] = {}
    for row in holdings:
        name = row.stock.category.name
        category_values[name] = category_values.get(name, 0) + row.market_value

    rows = []
    rebalance_categories = RebalanceCategory.objects.select_related("category").order_by("category__name")
    for entry in rebalance_categories:
        category_name = entry.category.name
        current = category_values.get(category_name, 0)
        ratio = Decimal("0")
        if totals["market_total"] > 0:
            ratio = Decimal(current * 100 / totals["market_total"]).quantize(Decimal("0.01"))
        rows.append(
            {
                "name": category_name,
                "current_value": current,
                "current_ratio": ratio,
                "target_ratio": entry.target_weight,
            }
        )
    return rows


def category_rebalance_rows() -> list[dict[str, Decimal | int | str]]:
    breakdown_map = {str(row["name"]): row for row in category_breakdown()}
    rows: list[dict[str, Decimal | int | str]] = []
    for plan in rebalance_plan():
        breakdown = breakdown_map.get(plan.category, {})
        rows.append(
            {
                "name": plan.category,
                "current_ratio": breakdown.get("current_ratio", Decimal("0.00")),
                "target_ratio": breakdown.get("target_ratio", Decimal("0.00")),
                "current_value": plan.current_value,
                "target_value": plan.target_value,
                "gap_value": plan.gap_value,
            }
        )
    return rows


def rebalance_plan() -> list[RebalanceItem]:
    totals = portfolio_totals()
    investable = totals["total_assets"]
    breakdown = category_breakdown()
    plan: list[RebalanceItem] = []
    for row in breakdown:
        target = int(investable * float(row["target_ratio"]) / 100)
        gap = target - int(row["current_value"])
        plan.append(
            RebalanceItem(
                category=str(row["name"]),
                current_value=int(row["current_value"]),
                target_value=target,
                gap_value=gap,
            )
        )
    return plan


def sync_latest_prices(price_date: date) -> int:
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as exc:
        raise RuntimeError("pykrx 설치가 필요합니다. requirements.txt를 설치해주세요.") from exc

    alias_map = {alias.stock_id: alias for alias in InstrumentAlias.objects.select_related("stock").all()}
    updated = 0
    for row in Stock.objects.all():
        alias = alias_map.get(row.id)
        close_price, source = _resolve_close_for_stock(row, alias, price_date, pykrx_stock=pykrx_stock)
        if close_price is None:
            continue

        PriceHistory.objects.update_or_create(
            stock=row,
            price_date=price_date,
            defaults={"close_price": close_price, "source": source},
        )
        updated += 1
    return updated


def sync_realtime_prices(stock_ids: set[int], price_date: date) -> int:
    if not stock_ids:
        return 0
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as exc:
        raise RuntimeError("pykrx 설치가 필요합니다. requirements.txt를 설치해주세요.") from exc

    alias_map = {alias.stock_id: alias for alias in InstrumentAlias.objects.filter(stock_id__in=stock_ids)}
    updated = 0
    for stock in Stock.objects.filter(id__in=stock_ids):
        alias = alias_map.get(stock.id)
        naver_symbol = (
            (alias.naver_symbol.strip() if alias and alias.naver_symbol else "")
            or stock.ticker
        )
        yahoo_symbol = (
            (alias.yahoo_symbol.strip().upper() if alias and alias.yahoo_symbol else "")
            or (stock.ticker if _is_usd_ticker(stock.ticker) else "")
        )

        realtime_price: Decimal | None = None
        source = ""
        if _is_usd_ticker(stock.ticker):
            realtime_price = _fetch_yahoo_realtime_price(yahoo_symbol)
            source = "yahoo_rt"
        else:
            realtime_price = _fetch_naver_realtime_price(naver_symbol)
            source = "naver_rt"

        if realtime_price is None:
            realtime_price, source = _resolve_close_for_stock(
                stock,
                alias,
                price_date,
                pykrx_stock=pykrx_stock,
            )
        if realtime_price is None:
            continue

        PriceHistory.objects.update_or_create(
            stock=stock,
            price_date=price_date,
            defaults={"close_price": realtime_price, "source": source},
        )
        updated += 1
    return updated


def backfill_previous_business_prices(
    price_date: date,
    stock_ids: set[int],
    max_lookback_days: int = 10,
    refresh_existing: bool = False,
) -> int:
    if not stock_ids:
        return 0
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as exc:
        raise RuntimeError("pykrx 설치가 필요합니다. requirements.txt를 설치해주세요.") from exc

    alias_map = {alias.stock_id: alias for alias in InstrumentAlias.objects.filter(stock_id__in=stock_ids)}
    backfilled = 0
    for stock in Stock.objects.filter(id__in=stock_ids):
        has_prev = PriceHistory.objects.filter(stock=stock, price_date__lt=price_date).exists()
        if has_prev and not refresh_existing:
            continue
        for offset in range(1, max_lookback_days + 1):
            target_date = price_date - timedelta(days=offset)
            close_price, source = _resolve_close_for_stock(
                stock,
                alias_map.get(stock.id),
                target_date,
                pykrx_stock=pykrx_stock,
            )
            if close_price is None:
                continue
            PriceHistory.objects.update_or_create(
                stock=stock,
                price_date=target_date,
                defaults={"close_price": close_price, "source": source},
            )
            backfilled += 1
            break
    return backfilled


def stocks_missing_price(price_date: date):
    priced_ids = PriceHistory.objects.filter(price_date=price_date).values_list("stock_id", flat=True)
    return Stock.objects.exclude(id__in=priced_ids).order_by("ticker")


def sync_stock_names() -> tuple[int, int]:
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as exc:
        raise RuntimeError("pykrx 설치가 필요합니다. requirements.txt를 설치해주세요.") from exc

    updated = 0
    skipped = 0
    known_tickers = _collect_krx_tickers(pykrx_stock)
    for row in Stock.objects.all():
        fetched_name = lookup_stock_name_by_ticker(
            row.ticker,
            pykrx_stock=pykrx_stock,
            valid_tickers=known_tickers,
            prefer_existing=False,
        )
        if not fetched_name:
            skipped += 1
            continue
        if row.name != fetched_name:
            row.name = fetched_name
            row.save(update_fields=["name"])
            updated += 1
    return updated, skipped


def _group_name_for_account(account: Account) -> str | None:
    group_name = (account.account_group or "").strip()
    if group_name:
        return group_name
    return "미지정"


def grouped_rebalance_recommendations() -> list[dict[str, object]]:
    holdings = latest_holdings_by_account()
    cash_map = latest_cash_by_account()
    latest_prices = latest_price_map()
    global_holding_price_by_stock: dict[int, int] = {}
    for holding in holdings:
        # 가격 히스토리가 비어 있을 때를 대비해 최신 보유스냅샷의 현재가를 보조 소스로 사용한다.
        if holding.current_price > 0 and holding.stock_id not in global_holding_price_by_stock:
            global_holding_price_by_stock[holding.stock_id] = holding.current_price
    usdkrw_rate = _fetch_usdkrw_rate(date.today()) or Decimal("1")

    accounts = list(Account.objects.all())
    stocks_by_category: dict[str, list[RebalanceStock]] = {}
    listed_tickers: set[str] = set()
    for entry in RebalanceStock.objects.select_related(
        "rebalance_category__category",
        "stock",
    ).order_by("stock__ticker"):
        stocks_by_category.setdefault(entry.rebalance_category.category.name, []).append(entry)
        listed_tickers.add(entry.stock.ticker)

    rebalance_categories = list(RebalanceCategory.objects.select_related("category").order_by("category__name"))
    grouped_accounts: dict[str, list[Account]] = {}
    for account in accounts:
        group_name = _group_name_for_account(account)
        if group_name:
            grouped_accounts.setdefault(group_name, []).append(account)

    results: list[dict[str, object]] = []
    for group_name, group_accounts in grouped_accounts.items():
        group_account_ids = {account.id for account in group_accounts}
        raw_group_holdings = [row for row in holdings if row.account_id in group_account_ids]
        group_cash = {account.id: cash_map.get(account.id, 0) for account in group_accounts}

        recommendation_map: dict[tuple[str, str, str], GroupTradeRecommendation] = {}
        group_holdings: list[HoldingSnapshot] = []

        def _record_recommendation(
            *,
            action: str,
            account_name: str,
            category: str,
            ticker: str,
            stock_name: str,
            price: int,
            shares: int,
            amount: int,
        ) -> None:
            if shares <= 0 or amount <= 0:
                return
            key = (action, account_name, ticker)
            existing = recommendation_map.get(key)
            if existing:
                existing.shares += shares
                existing.amount += amount
                return
            recommendation_map[key] = GroupTradeRecommendation(
                action=action,
                account_name=account_name,
                category=category,
                ticker=ticker,
                stock_name=stock_name,
                price=price,
                shares=shares,
                amount=amount,
            )

        # 0) 리밸런싱 리스트 밖 종목은 전량 매도 후 현금으로 환산한다.
        for row in raw_group_holdings:
            if row.stock.ticker not in listed_tickers and row.quantity > 0:
                amount = row.market_value
                group_cash[row.account_id] = group_cash.get(row.account_id, 0) + amount
                _record_recommendation(
                    action="매도",
                    account_name=row.account.name,
                    category=row.stock.category.name,
                    ticker=row.stock.ticker,
                    stock_name=row.stock.name,
                    price=row.current_price,
                    shares=row.quantity,
                    amount=amount,
                )
                continue
            group_holdings.append(row)

        market_total = sum(row.market_value for row in group_holdings)
        cash_total = sum(group_cash.values())
        total_assets = market_total + cash_total

        category_values: dict[str, int] = {}
        for row in group_holdings:
            category_name = row.stock.category.name
            category_values[category_name] = category_values.get(category_name, 0) + row.market_value

        def _build_category_plan(values: dict[str, int]) -> list[dict[str, int | str | Decimal]]:
            plan: list[dict[str, int | str | Decimal]] = []
            for entry in rebalance_categories:
                category_name = entry.category.name
                current = values.get(category_name, 0)
                target = int(total_assets * float(entry.target_weight) / 100)
                gap = target - current
                plan.append(
                    {
                        "category": category_name,
                        "current_value": current,
                        "target_value": target,
                        "gap_value": gap,
                        "target_ratio": entry.target_weight,
                    }
                )
            return plan

        # 리밸런싱 전 현재 상태(사용자에게 보여줄 현재비중/현재금액)
        current_category_plan = _build_category_plan(dict(category_values))
        remaining_qty_by_holding_id = {h.id: h.quantity for h in group_holdings}
        account_known_tickers: dict[int, set[str]] = {account.id: set() for account in group_accounts}
        for holding in group_holdings:
            if holding.quantity > 0:
                account_known_tickers.setdefault(holding.account_id, set()).add(holding.stock.ticker)

        def _krw_price_for_stock(stock_item: Stock) -> int:
            raw_price: Decimal | None = latest_prices.get(stock_item.ticker)
            if raw_price is None:
                for h in group_holdings:
                    if h.stock_id == stock_item.id:
                        raw_price = Decimal(h.current_price)
                        break
            if raw_price is None:
                fallback_price = global_holding_price_by_stock.get(stock_item.id)
                if fallback_price:
                    raw_price = Decimal(fallback_price)
            if raw_price is None or raw_price <= 0:
                return 0
            effective_price = raw_price
            if _is_usd_ticker(stock_item.ticker):
                effective_price = raw_price * usdkrw_rate
            return int(effective_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        def _pick_account_for_buy(ticker: str, price: int) -> tuple[int, int] | None:
            affordable_accounts = [(aid, cash) for aid, cash in group_cash.items() if cash >= price]
            if not affordable_accounts:
                return None

            def _account_score(row: tuple[int, int]) -> tuple[int, int, int]:
                account_id, cash = row
                known = account_known_tickers.get(account_id, set())
                # 동일 종목 보유/추천 계좌 우선, 그다음 종목 수가 적은 계좌 우선(계좌별 종목 단순화)
                has_same_ticker = 1 if ticker in known else 0
                ticker_count_score = -len(known)
                return (has_same_ticker, ticker_count_score, cash)

            return max(affordable_accounts, key=_account_score)

        # 1) 초과 비중 카테고리는 종목 목표비중을 우선 반영해 매도한다.
        category_plan = _build_category_plan(category_values)
        for row in sorted(category_plan, key=lambda x: int(x["gap_value"])):
            gap = int(row["gap_value"])
            if gap >= 0:
                continue
            category_name = str(row["category"])
            need_to_reduce = abs(gap)
            category_entries = stocks_by_category.get(category_name, [])
            has_explicit_stock_targets = any(float(entry.target_weight) > 0 for entry in category_entries)
            category_target_value = int(row["target_value"])
            category_stock_count = len(category_entries)
            category_base_target = category_target_value // max(1, category_stock_count)
            category_remainder = category_target_value - (category_base_target * max(1, category_stock_count))

            stock_target_values: dict[int, int] = {}
            for idx, entry in enumerate(category_entries):
                if has_explicit_stock_targets:
                    stock_target_values[entry.stock_id] = int(total_assets * float(entry.target_weight) / 100)
                else:
                    stock_target_values[entry.stock_id] = category_base_target + (
                        1 if idx < category_remainder else 0
                    )

            candidate_states: list[dict[str, object]] = []
            for holding in group_holdings:
                if holding.stock.category.name != category_name:
                    continue
                available_qty = remaining_qty_by_holding_id.get(holding.id, 0)
                if available_qty <= 0:
                    continue
                price = _krw_price_for_stock(holding.stock) or holding.current_price
                if price <= 0:
                    continue
                candidate_states.append(
                    {
                        "holding_id": holding.id,
                        "account_id": holding.account_id,
                        "account_name": holding.account.name,
                        "stock": holding.stock,
                        "price": price,
                        "quantity": available_qty,
                        "current_value": available_qty * price,
                        "target_value": stock_target_values.get(holding.stock_id, 0),
                    }
                )

            while need_to_reduce > 0:
                eligible_states: list[dict[str, object]] = []
                for state in candidate_states:
                    price = int(state["price"])
                    if price <= 0 or price > need_to_reduce:
                        continue
                    if int(state["quantity"]) <= 0:
                        continue
                    stock_over = int(state["current_value"]) - int(state["target_value"])
                    if stock_over < price:
                        continue
                    eligible_states.append(state)

                if not eligible_states:
                    break

                eligible_states.sort(
                    key=lambda state: (
                        int(state["current_value"]) - int(state["target_value"]),
                        int(state["current_value"]),
                    ),
                    reverse=True,
                )
                chosen_state = eligible_states[0]
                price = int(chosen_state["price"])
                stock_over = int(chosen_state["current_value"]) - int(chosen_state["target_value"])
                shares = min(
                    int(chosen_state["quantity"]),
                    stock_over // price,
                    need_to_reduce // price,
                )
                if shares <= 0:
                    break

                amount = shares * price
                chosen_state["quantity"] = int(chosen_state["quantity"]) - shares
                chosen_state["current_value"] = int(chosen_state["current_value"]) - amount
                remaining_qty_by_holding_id[int(chosen_state["holding_id"])] = int(chosen_state["quantity"])
                need_to_reduce -= amount
                group_cash[int(chosen_state["account_id"])] = (
                    group_cash.get(int(chosen_state["account_id"]), 0) + amount
                )
                category_values[category_name] = category_values.get(category_name, 0) - amount
                stock_item = chosen_state["stock"]
                _record_recommendation(
                    action="매도",
                    account_name=str(chosen_state["account_name"]),
                    category=category_name,
                    ticker=stock_item.ticker,
                    stock_name=stock_item.name,
                    price=price,
                    shares=shares,
                    amount=amount,
                )

        # 2) 부족 비중 카테고리는 매도 후 생긴 총 예수금으로 매수해 목표에 맞춘다.
        category_plan = _build_category_plan(category_values)
        for row in sorted(category_plan, key=lambda x: int(x["gap_value"]), reverse=True):
            gap = int(row["gap_value"])
            if gap <= 0:
                continue
            category_name = str(row["category"])
            category_entries = stocks_by_category.get(category_name, [])
            if not category_entries:
                continue

            stock_current_values: dict[int, int] = {}
            for holding in group_holdings:
                if holding.stock.category.name != category_name:
                    continue
                stock_current_values[holding.stock_id] = stock_current_values.get(holding.stock_id, 0) + holding.market_value

            has_explicit_stock_targets = any(float(entry.target_weight) > 0 for entry in category_entries)
            category_target_value = int(row["target_value"])
            category_stock_count = len(category_entries)
            category_base_target = category_target_value // max(1, category_stock_count)
            category_remainder = category_target_value - (category_base_target * max(1, category_stock_count))

            stock_states: list[dict[str, object]] = []
            for idx, entry in enumerate(category_entries):
                stock_item = entry.stock
                price = _krw_price_for_stock(stock_item)
                if price <= 0:
                    continue
                if has_explicit_stock_targets:
                    stock_target_value = int(total_assets * float(entry.target_weight) / 100)
                else:
                    stock_target_value = category_base_target + (1 if idx < category_remainder else 0)
                stock_current_value = stock_current_values.get(stock_item.id, 0)
                stock_states.append(
                    {
                        "stock": stock_item,
                        "price": price,
                        "target_value": stock_target_value,
                        "current_value": stock_current_value,
                    }
                )
            if not stock_states:
                continue

            remaining_gap = gap

            # 카테고리 내 종목별 목표 갭을 우선순위로 매수한다.
            # 1주 미만 잔여 갭이 남으면 예수금 최소화를 위해 1주 과매수도 허용한다.
            while remaining_gap > 0:
                eligible_states: list[dict[str, object]] = []
                for state in stock_states:
                    price = int(state["price"])
                    if price <= 0:
                        continue
                    stock_gap = int(state["target_value"]) - int(state["current_value"])
                    if stock_gap <= 0:
                        continue
                    if not any(cash >= price for cash in group_cash.values()):
                        continue
                    eligible_states.append(state)

                if not eligible_states:
                    break

                eligible_states.sort(
                    key=lambda state: (
                        int(state["target_value"]) - int(state["current_value"]),
                        -int(state["price"]),
                    ),
                    reverse=True,
                )
                chosen_state = eligible_states[0]
                chosen_stock = chosen_state["stock"]
                price = int(chosen_state["price"])
                stock_gap = int(chosen_state["target_value"]) - int(chosen_state["current_value"])

                picked = _pick_account_for_buy(chosen_stock.ticker, price)
                if picked is None:
                    break
                target_account_id, available_cash = picked

                shares = min(stock_gap // price, available_cash // price, remaining_gap // price)
                if shares <= 0 and available_cash >= price:
                    # 잔여 부족금이 1주 미만이어도 예수금 최소화를 위해 1주 매수 허용
                    shares = 1
                if shares <= 0:
                    break

                amount = shares * price
                group_cash[target_account_id] = available_cash - amount
                remaining_gap -= amount
                chosen_state["current_value"] = int(chosen_state["current_value"]) + amount
                category_values[category_name] = category_values.get(category_name, 0) + amount
                account_known_tickers.setdefault(target_account_id, set()).add(chosen_stock.ticker)
                target_account_name = next((a.name for a in group_accounts if a.id == target_account_id), "")
                _record_recommendation(
                    action="매수",
                    account_name=target_account_name,
                    category=category_name,
                    ticker=chosen_stock.ticker,
                    stock_name=chosen_stock.name,
                    price=price,
                    shares=shares,
                    amount=amount,
                )

        category_plan = _build_category_plan(category_values)

        recommendations = sorted(
            recommendation_map.values(),
            key=lambda rec: (rec.account_name, rec.action, rec.category, rec.ticker),
        )

        results.append(
            {
                "group_name": group_name,
                "accounts": [a.name for a in group_accounts],
                "cash_total": cash_total,
                "market_total": market_total,
                "total_assets": total_assets,
                "current_category_plan": current_category_plan,
                "category_plan": category_plan,
                "trade_recommendations": recommendations,
            }
        )

    return results


def grouped_account_rebalance_status() -> list[dict[str, object]]:
    holdings = latest_holdings_by_account()
    cash_map = latest_cash_by_account()
    grouped_rebalances = grouped_rebalance_recommendations()
    grouped_rebalance_map = {str(group["group_name"]): group for group in grouped_rebalances}

    recommendation_shares_map: dict[tuple[str, str], int] = {}
    recommendation_meta_map: dict[tuple[str, str], dict[str, object]] = {}
    recommendation_cash_delta_by_account: dict[str, int] = {}
    for group in grouped_rebalances:
        for rec in group["trade_recommendations"]:
            key = (str(rec.account_name), str(rec.ticker))
            signed_shares = int(rec.shares) if rec.action == "매수" else -int(rec.shares)
            recommendation_shares_map[key] = recommendation_shares_map.get(key, 0) + signed_shares
            recommendation_meta_map[key] = {
                "category": rec.category,
                "stock_name": rec.stock_name,
                "price": rec.price,
            }
            cash_delta = int(rec.amount) if rec.action == "매도" else -int(rec.amount)
            recommendation_cash_delta_by_account[rec.account_name] = (
                recommendation_cash_delta_by_account.get(rec.account_name, 0) + cash_delta
            )

    grouped_accounts: dict[str, list[Account]] = {}
    for account in Account.objects.all():
        group_name = _group_name_for_account(account)
        if group_name:
            grouped_accounts.setdefault(group_name, []).append(account)

    results: list[dict[str, object]] = []
    for group_name, accounts in grouped_accounts.items():
        group_rebalance = grouped_rebalance_map.get(group_name, {})
        group_market_total = int(group_rebalance.get("market_total", 0))
        group_cash_total = int(group_rebalance.get("cash_total", 0))
        group_total_assets = int(group_rebalance.get("total_assets", 0))
        category_rows: list[dict[str, object]] = []
        for row in group_rebalance.get("current_category_plan", group_rebalance.get("category_plan", [])):
            current_value = int(row["current_value"])
            current_ratio = Decimal("0.00")
            if group_total_assets > 0:
                current_ratio = Decimal(current_value * 100 / group_total_assets).quantize(Decimal("0.01"))
            category_rows.append(
                {
                    "category": row["category"],
                    "current_ratio": current_ratio,
                    "target_ratio": row.get("target_ratio", Decimal("0.00")),
                    "current_value": current_value,
                    "target_value": int(row["target_value"]),
                    "gap_value": int(row["gap_value"]),
                }
            )

        account_rows: list[dict[str, object]] = []
        for account in accounts:
            account_holdings = [h for h in holdings if h.account_id == account.id]
            holding_rows: list[dict[str, object]] = []
            seen_tickers: set[str] = set()

            for h in sorted(account_holdings, key=lambda row: (row.stock.category.name, row.stock.ticker)):
                key = (account.name, h.stock.ticker)
                reco_shares = recommendation_shares_map.get(key, 0)
                if h.quantity == 0 and reco_shares == 0:
                    # 보유수량이 없고 신규 추천도 없으면 표에서 제외한다.
                    continue
                holding_rows.append(
                    {
                        "category": h.stock.category.name,
                        "ticker": h.stock.ticker,
                        "stock_name": h.stock.name,
                        "quantity": h.quantity,
                        "avg_price": h.avg_price,
                        "current_price": h.current_price,
                        "market_value": h.market_value,
                        "recommendation_shares": reco_shares,
                    }
                )
                seen_tickers.add(h.stock.ticker)

            # 보유하지 않은 종목이지만 신규 매수 추천이 있을 수 있어 별도 행으로 추가한다.
            for (account_name, ticker), reco_shares in recommendation_shares_map.items():
                if account_name != account.name or ticker in seen_tickers or reco_shares == 0:
                    continue
                meta = recommendation_meta_map.get((account_name, ticker), {})
                current_price = int(meta.get("price", 0))
                holding_rows.append(
                    {
                        "category": str(meta.get("category", "")),
                        "ticker": ticker,
                        "stock_name": str(meta.get("stock_name", "")),
                        "quantity": 0,
                        "avg_price": 0,
                        "current_price": current_price,
                        "market_value": 0,
                        "recommendation_shares": reco_shares,
                    }
                )

            account_rows.append(
                {
                    "account_name": account.name,
                    "cash_balance": cash_map.get(account.id, 0),
                    "expected_cash_balance": cash_map.get(account.id, 0)
                    + recommendation_cash_delta_by_account.get(account.name, 0),
                    "holding_rows": holding_rows,
                }
            )

        results.append(
            {
                "group_name": group_name,
                "market_total": group_market_total,
                "cash_total": group_cash_total,
                "total_assets": group_total_assets,
                "category_rows": category_rows,
                "account_rows": account_rows,
            }
        )
    return results
