from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import quote

from django.contrib import messages
from django.core.management import call_command
from django.db import transaction
from django.db.models import Max, Min, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .forms import (
    AccountCreateForm,
    AccountUpdateForm,
    CashFlowForm,
    ManualPriceForm,
    RebalanceCategoryCreateForm,
    RebalanceCategoryUpdateForm,
    RebalanceStockCreateForm,
    RebalanceStockUpdateForm,
    TradeForm,
)
from .models import (
    Account,
    AccountSnapshot,
    Category,
    CashFlow,
    HoldingSnapshot,
    PriceHistory,
    RebalanceCategory,
    RebalanceCategoryHistory,
    RebalanceStock,
    RebalanceStockHistory,
    Stock,
    Trade,
)
from .portfolio_registry import (
    create_portfolio as registry_create_portfolio,
    delete_portfolio as registry_delete_portfolio,
    get_portfolio,
    list_portfolios,
    rename_portfolio as registry_rename_portfolio,
)
from .db_context import using_default_database
from .services import (
    category_rebalance_rows,
    category_breakdown,
    default_requires_jonghap_account,
    grouped_account_rebalance_status,
    lookup_stock_name_by_ticker,
    portfolio_totals,
    backfill_previous_business_prices,
    resolve_price_board_dates,
    sync_latest_prices,
    sync_realtime_prices,
    sync_stock_names,
)

ACTIVE_PORTFOLIO_COOKIE = "active_portfolio_id"


def _parse_optional_non_negative_int(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    if stripped == "":
        return None
    parsed = int(stripped)
    if parsed < 0:
        raise ValueError("negative value")
    return parsed


def _cashflow_effect_total(
    account: Account,
    *,
    up_to_date: date | None = None,
    exclude_cashflow_id: int | None = None,
) -> int:
    queryset = CashFlow.objects.filter(account=account)
    if up_to_date is not None:
        queryset = queryset.filter(occurred_on__lte=up_to_date)
    if exclude_cashflow_id is not None:
        queryset = queryset.exclude(pk=exclude_cashflow_id)
    total = 0
    for flow in queryset:
        total += flow.amount if flow.flow_type == CashFlow.DEPOSIT else -flow.amount
    return total


def _trade_effect_total(
    account: Account,
    *,
    up_to_date: date | None = None,
    exclude_trade_id: int | None = None,
) -> int:
    queryset = Trade.objects.filter(account=account)
    if up_to_date is not None:
        queryset = queryset.filter(traded_on__lte=up_to_date)
    if exclude_trade_id is not None:
        queryset = queryset.exclude(pk=exclude_trade_id)
    return sum(item.net_cash_effect for item in queryset)


def _current_account_cash_balance(
    account: Account,
    *,
    up_to_date: date | None = None,
    exclude_cashflow_id: int | None = None,
    exclude_trade_id: int | None = None,
) -> int:
    return (
        account.initial_balance
        + _cashflow_effect_total(
            account,
            up_to_date=up_to_date,
            exclude_cashflow_id=exclude_cashflow_id,
        )
        + _trade_effect_total(account, up_to_date=up_to_date, exclude_trade_id=exclude_trade_id)
    )


def _calculate_position_from_trades(
    account: Account,
    stock: Stock,
    *,
    exclude_trade_id: int | None = None,
    additional_trade: dict | None = None,
) -> tuple[int, int]:
    def _trade_sort_key(row):
        if isinstance(row, dict):
            return row["traded_on"], row.get("id", 0)
        return row.traded_on, row.id

    trade_rows = list(
        Trade.objects.filter(account=account, stock=stock)
        .exclude(pk=exclude_trade_id)
        .order_by("traded_on", "id")
    )
    if additional_trade:
        trade_rows.append(additional_trade)
        trade_rows.sort(key=_trade_sort_key)

    quantity = 0
    avg_price = 0
    for row in trade_rows:
        trade_type = row["trade_type"] if isinstance(row, dict) else row.trade_type
        trade_quantity = int(row["quantity"] if isinstance(row, dict) else row.quantity)
        trade_price = int(row["price"] if isinstance(row, dict) else row.price)

        if trade_type == Trade.BUY:
            new_quantity = quantity + trade_quantity
            avg_price = int(
                (
                    Decimal(quantity * avg_price + trade_quantity * trade_price) / Decimal(new_quantity)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            quantity = new_quantity
            continue

        if trade_quantity > quantity:
            raise ValueError("SELL quantity exceeds position")
        quantity -= trade_quantity
        if quantity == 0:
            avg_price = 0

    return quantity, avg_price


def _latest_holding_current_price(account: Account, stock: Stock, fallback_price: int) -> int:
    snapshot = (
        HoldingSnapshot.objects.filter(account=account, stock=stock)
        .order_by("-as_of_date", "-id")
        .first()
    )
    return snapshot.current_price if snapshot else fallback_price


def _build_unified_activity_rows(today: date) -> list[dict]:
    accounts = list(Account.objects.order_by("name"))
    cashflows = list(CashFlow.objects.select_related("account").all())
    trades = list(Trade.objects.select_related("account", "stock").all())

    cashflows_by_account: dict[int, list[CashFlow]] = {}
    for entry in cashflows:
        cashflows_by_account.setdefault(entry.account_id, []).append(entry)

    trades_by_account: dict[int, list[Trade]] = {}
    for entry in trades:
        trades_by_account.setdefault(entry.account_id, []).append(entry)

    rows: list[dict] = []
    for account in accounts:
        account_cashflows = cashflows_by_account.get(account.id, [])
        account_trades = trades_by_account.get(account.id, [])
        first_cashflow_date = min((entry.occurred_on for entry in account_cashflows), default=None)
        first_trade_date = min((entry.traded_on for entry in account_trades), default=None)
        candidate_dates = [value for value in [first_cashflow_date, first_trade_date] if value is not None]
        opened_on = (min(candidate_dates) - timedelta(days=1)) if candidate_dates else today

        timeline: list[dict] = [
            {
                "kind": "account",
                "event_date": opened_on,
                "event_dt": datetime.combine(opened_on, time.min),
                "sort_id": account.id,
                "account": account,
            }
        ]

        for entry in account_cashflows:
            timeline.append(
                {
                    "kind": "cashflow",
                    "event_date": entry.occurred_on,
                    "event_dt": datetime.combine(entry.occurred_on, time.min),
                    "sort_id": entry.id,
                    "entry": entry,
                }
            )
        for entry in account_trades:
            timeline.append(
                {
                    "kind": "trade",
                    "event_date": entry.traded_on,
                    "event_dt": datetime.combine(entry.traded_on, time.min),
                    "sort_id": entry.id,
                    "entry": entry,
                }
            )

        kind_order = {"account": 0, "cashflow": 1, "trade": 2}
        timeline.sort(
            key=lambda item: (
                item["event_date"],
                kind_order.get(item["kind"], 9),
                item["sort_id"],
            )
        )

        running_balance = 0
        for item in timeline:
            kind = item["kind"]
            if kind == "account":
                running_balance = account.initial_balance
                rows.append(
                    {
                        "row_type": "account",
                        "event_date": item["event_date"],
                        "event_dt": item["event_dt"],
                        "sort_id": item["sort_id"],
                        "account_id": account.id,
                        "account_name": account.name,
                        "kind_label": "계좌개설",
                        "quantity": None,
                        "unit_amount": account.initial_balance,
                        "cash_balance": running_balance,
                        "account_group": account.account_group,
                        "initial_balance": account.initial_balance,
                        "is_jonghap": account.is_jonghap,
                    }
                )
                continue

            if kind == "cashflow":
                entry: CashFlow = item["entry"]
                delta = entry.amount if entry.flow_type == CashFlow.DEPOSIT else -entry.amount
                running_balance += delta
                rows.append(
                    {
                        "row_type": "cashflow",
                        "event_date": item["event_date"],
                        "event_dt": item["event_dt"],
                        "sort_id": item["sort_id"],
                        "account_id": entry.account_id,
                        "account_name": entry.account.name,
                        "kind_label": entry.get_flow_type_display(),
                        "quantity": None,
                        "unit_amount": entry.amount,
                        "cash_balance": running_balance,
                        "cashflow_id": entry.id,
                        "flow_type": entry.flow_type,
                        "amount": entry.amount,
                        "memo": entry.memo,
                    }
                )
                continue

            entry = item["entry"]
            running_balance += entry.net_cash_effect
            rows.append(
                {
                    "row_type": "trade",
                    "event_date": item["event_date"],
                    "event_dt": item["event_dt"],
                    "sort_id": item["sort_id"],
                    "account_id": entry.account_id,
                    "account_name": entry.account.name,
                    "kind_label": entry.get_trade_type_display(),
                    "quantity": entry.quantity,
                    "unit_amount": entry.price,
                    "cash_balance": running_balance,
                    "trade_id": entry.id,
                    "stock_id": entry.stock_id,
                    "stock_ticker": entry.stock.ticker,
                    "trade_type": entry.trade_type,
                    "price": entry.price,
                    "fee": entry.fee,
                    "tax": entry.tax,
                    "memo": entry.memo,
                }
            )

    rows.sort(key=lambda item: (item["event_date"], item["event_dt"], item["sort_id"]), reverse=True)
    return rows


def _needs_decimal_display(value: Decimal | None, source: str) -> bool:
    if value is None:
        return False
    if source == "yahoo":
        return True
    return value != value.quantize(Decimal("1"))


@require_http_methods(["GET"])
def dashboard(request):
    today = date.today()
    price_display_date, prev_display_date, is_market_open = resolve_price_board_dates()

    totals = portfolio_totals()
    latest_holding_date = HoldingSnapshot.objects.aggregate(last_date=Max("as_of_date"))["last_date"]
    category_add_form = RebalanceCategoryCreateForm()
    stock_add_form = RebalanceStockCreateForm()
    account_add_form = AccountCreateForm()
    target_weight_total = RebalanceCategory.objects.aggregate(total=Sum("target_weight"))["total"] or Decimal("0.00")
    stock_target_weight_total = RebalanceStock.objects.aggregate(total=Sum("target_weight"))["total"] or Decimal("0.00")
    holding_stock_ids = set()
    if latest_holding_date:
        holding_stock_ids = set(
            HoldingSnapshot.objects.filter(as_of_date=latest_holding_date).values_list("stock_id", flat=True)
        )
    rebalance_stock_ids = set(RebalanceStock.objects.values_list("stock_id", flat=True))
    target_stock_ids = holding_stock_ids | rebalance_stock_ids
    try:
        # 리프레시 시점마다 오늘/직전 영업일 가격을 다시 조회해 최신 값으로 덮어쓴다.
        if is_market_open and price_display_date == today:
            sync_realtime_prices(target_stock_ids, price_display_date)
        else:
            sync_latest_prices(price_display_date)
        backfill_previous_business_prices(price_display_date, target_stock_ids, refresh_existing=True)
    except RuntimeError:
        pass

    target_stocks = list(Stock.objects.filter(id__in=target_stock_ids).select_related("category").order_by("ticker"))
    today_prices = {
        row.stock_id: row
        for row in PriceHistory.objects.filter(
            price_date=price_display_date,
            stock_id__in=target_stock_ids,
        ).select_related("stock")
    }
    prev_prices: dict[int, PriceHistory] = {}
    prev_rows = PriceHistory.objects.filter(
        stock_id__in=target_stock_ids,
        price_date__lt=price_display_date,
    ).select_related("stock").order_by("stock_id", "-price_date")
    for row in prev_rows:
        if row.stock_id not in prev_prices:
            prev_prices[row.stock_id] = row

    price_board_rows = []
    for stock in target_stocks:
        today_row = today_prices.get(stock.id)
        prev_row = prev_prices.get(stock.id)
        today_price = today_row.close_price if today_row else None
        today_source = today_row.source if today_row else ""
        prev_price = prev_row.close_price if prev_row else None
        prev_source = prev_row.source if prev_row else ""
        change_value = None
        change_rate = None
        if today_price is not None and prev_price is not None:
            change_value = today_price - prev_price
            if prev_price > 0:
                change_rate = (change_value * Decimal("100") / prev_price).quantize(Decimal("0.01"))

        today_needs_decimal = _needs_decimal_display(today_price, today_source)
        prev_needs_decimal = _needs_decimal_display(prev_price, prev_source)
        price_board_rows.append(
            {
                "stock": stock,
                "in_holdings": stock.id in holding_stock_ids,
                "in_rebalance": stock.id in rebalance_stock_ids,
                "today_price": today_price,
                "today_source": today_source,
                "prev_price": prev_price,
                "prev_date": prev_row.price_date if prev_row else None,
                "prev_source": prev_source,
                "change_value": change_value,
                "change_rate": change_rate,
                "today_needs_decimal": today_needs_decimal,
                "prev_needs_decimal": prev_needs_decimal,
                "change_needs_decimal": today_needs_decimal or prev_needs_decimal,
                "needs_manual": today_row is None,
            }
        )

    latest_cash_map = {}
    latest_cash_rows = (
        AccountSnapshot.objects.values("account_id").annotate(last_date=Max("as_of_date"))
    )
    for cash_row in latest_cash_rows:
        snapshot = AccountSnapshot.objects.get(
            account_id=cash_row["account_id"],
            as_of_date=cash_row["last_date"],
        )
        latest_cash_map[cash_row["account_id"]] = snapshot.cash_balance

    account_rows = [
        {
            "account": account,
            "cash_balance": latest_cash_map.get(account.id, 0),
            "initial_balance": account.initial_balance,
        }
        for account in Account.objects.order_by("name")
    ]

    context = {
        "totals": totals,
        "category_rows": category_breakdown(),
        "category_rebalance_rows": category_rebalance_rows(),
        "grouped_account_status": grouped_account_rebalance_status(),
        "cashflow_form": CashFlowForm(),
        "trade_form": TradeForm(),
        "rebalance_categories": RebalanceCategory.objects.select_related("category").order_by("category__name"),
        "recent_rebalance_category_histories": RebalanceCategoryHistory.objects.select_related("category").all()[:10],
        "rebalance_stocks": RebalanceStock.objects.select_related(
            "rebalance_category__category",
            "stock",
            "stock__category",
        ).order_by("rebalance_category__category__name", "stock__ticker"),
        "recent_rebalance_stock_histories": RebalanceStockHistory.objects.all()[:10],
        "category_add_form": category_add_form,
        "stock_add_form": stock_add_form,
        "account_add_form": account_add_form,
        "account_rows": account_rows,
        "recent_cashflows": CashFlow.objects.select_related("account").all()[:10],
        "cashflow_type_choices": CashFlow.FLOW_TYPES,
        "recent_trades": Trade.objects.select_related("account", "stock").all()[:10],
        "unified_activity_rows": _build_unified_activity_rows(today),
        "stocks": Stock.objects.order_by("ticker"),
        "trade_type_choices": Trade.TRADE_TYPES,
        "accounts": Account.objects.all(),
        "today": today,
        "price_display_date": price_display_date,
        "prev_display_date": prev_display_date,
        "is_market_open": is_market_open,
        "latest_holding_date": latest_holding_date,
        "target_weight_total": target_weight_total,
        "is_target_weight_total_valid": target_weight_total == Decimal("100"),
        "stock_target_weight_total": stock_target_weight_total,
        "is_stock_target_weight_total_valid": stock_target_weight_total == Decimal("100"),
        "price_board_rows": price_board_rows,
        "portfolio_list": getattr(request, "portfolio_list", list_portfolios()),
        "active_portfolio": getattr(request, "active_portfolio", None),
    }
    return render(request, "portfolio/dashboard.html", context)


@require_http_methods(["POST"])
def create_portfolio(request):
    portfolio_name = (request.POST.get("portfolio_name") or "").strip()
    if not portfolio_name:
        messages.error(request, "포트폴리오 이름을 입력해주세요.")
        return redirect("portfolio:dashboard")

    created = registry_create_portfolio(portfolio_name)
    try:
        with using_default_database(created["db_path"]):
            call_command("migrate", interactive=False, verbosity=0)
    except Exception as exc:
        messages.error(request, f"포트폴리오 생성 후 DB 초기화에 실패했습니다: {exc}")
        return redirect("portfolio:dashboard")

    messages.success(request, f"포트폴리오를 생성했습니다: {created['name']}")
    response = redirect("portfolio:dashboard")
    response.set_cookie(ACTIVE_PORTFOLIO_COOKIE, quote(created["id"], safe=""), samesite="Lax")
    return response


@require_http_methods(["POST"])
def select_portfolio(request, portfolio_id):
    selected = get_portfolio(portfolio_id)
    if not selected:
        messages.error(request, "선택한 포트폴리오를 찾을 수 없습니다.")
        return redirect("portfolio:dashboard")

    messages.success(request, f"{selected['name']} 포트폴리오로 전환했습니다.")
    response = redirect("portfolio:dashboard")
    response.set_cookie(ACTIVE_PORTFOLIO_COOKIE, quote(selected["id"], safe=""), samesite="Lax")
    return response


@require_http_methods(["POST"])
def update_portfolio(request, portfolio_id):
    new_name = (request.POST.get("portfolio_name") or "").strip()
    if not new_name:
        messages.error(request, "포트폴리오 이름을 입력해주세요.")
        return redirect("portfolio:dashboard")

    updated = registry_rename_portfolio(portfolio_id, new_name)
    if not updated:
        messages.error(request, "수정할 포트폴리오를 찾지 못했습니다.")
        return redirect("portfolio:dashboard")

    messages.success(request, f"포트폴리오 이름을 수정했습니다: {updated['name']}")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def delete_portfolio(request, portfolio_id):
    deleting = get_portfolio(portfolio_id)
    ok, error_message = registry_delete_portfolio(portfolio_id)
    if not ok:
        messages.error(request, error_message or "포트폴리오 삭제에 실패했습니다.")
        return redirect("portfolio:dashboard")

    remaining = list_portfolios()
    current_id = request.COOKIES.get(ACTIVE_PORTFOLIO_COOKIE)
    next_portfolio_id = remaining[0]["id"] if remaining else ""
    switched = current_id == portfolio_id and bool(next_portfolio_id)

    deleted_name = deleting["name"] if deleting else portfolio_id
    messages.success(request, f"포트폴리오를 삭제했습니다: {deleted_name}")
    response = redirect("portfolio:dashboard")
    if switched:
        response.set_cookie(ACTIVE_PORTFOLIO_COOKIE, quote(next_portfolio_id, safe=""), samesite="Lax")
    return response


@require_http_methods(["POST"])
def create_cashflow(request):
    form = CashFlowForm(request.POST)
    if form.is_valid():
        account = form.cleaned_data["account"]
        flow_type = form.cleaned_data["flow_type"]
        amount = int(form.cleaned_data["amount"])
        occurred_on = form.cleaned_data["occurred_on"]
        base_balance = _current_account_cash_balance(account, up_to_date=occurred_on)
        delta = amount if flow_type == CashFlow.DEPOSIT else -amount
        new_balance = base_balance + delta

        if new_balance < 0:
            messages.error(request, "출금 금액이 현재 예수금보다 커서 저장할 수 없습니다.")
            return redirect("portfolio:dashboard")

        with transaction.atomic():
            form.save()
            current_balance = _current_account_cash_balance(account)
            AccountSnapshot.objects.update_or_create(
                account=account,
                as_of_date=date.today(),
                defaults={"cash_balance": current_balance},
            )
        messages.success(request, "입출금 내역과 예수금이 반영되었습니다.")
    else:
        messages.error(request, "입출금 내역 저장에 실패했습니다. 값을 확인해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def create_account(request):
    form = AccountCreateForm(request.POST)
    if form.is_valid():
        account_group = form.cleaned_data["account_group"]
        account_name = form.cleaned_data["account_name"]
        cash_balance = form.cleaned_data["cash_balance"]

        if Account.objects.filter(name=account_name).exists():
            messages.error(request, "이미 같은 이름의 계좌가 있습니다. 계좌명은 고유해야 합니다.")
            return redirect("portfolio:dashboard")

        account = Account.objects.create(
            name=account_name,
            account_group=account_group,
            initial_balance=cash_balance,
            is_jonghap=bool(form.cleaned_data.get("is_jonghap")),
        )
        AccountSnapshot.objects.update_or_create(
            account=account,
            as_of_date=date.today(),
            defaults={"cash_balance": cash_balance},
        )
        messages.success(request, "계좌를 저장했습니다. 최초입금액이 예수금으로 반영되었습니다.")
    else:
        messages.error(request, "계좌 저장에 실패했습니다. 입력값을 확인해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def update_account(request, pk):
    account = get_object_or_404(Account, pk=pk)
    form = AccountUpdateForm(request.POST)
    if form.is_valid():
        account_group = form.cleaned_data["account_group"]
        account_name = form.cleaned_data["account_name"]
        initial_balance = int(form.cleaned_data["initial_balance"])
        if account_name != account.name and Account.objects.filter(name=account_name).exclude(pk=account.pk).exists():
            messages.error(request, "이미 같은 이름의 계좌가 있습니다. 계좌명은 고유해야 합니다.")
            return redirect("portfolio:dashboard")

        account.account_group = account_group
        account.name = account_name
        account.initial_balance = initial_balance
        account.is_jonghap = bool(form.cleaned_data.get("is_jonghap"))
        account.save(update_fields=["account_group", "name", "initial_balance", "is_jonghap"])
        messages.success(request, "계좌 정보를 수정했습니다. 최초입금액만 변경되고 현재 예수금은 유지됩니다.")
    else:
        messages.error(request, "계좌 수정에 실패했습니다. 입력값을 확인해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def delete_account(request, pk):
    account = get_object_or_404(Account, pk=pk)
    account_name = account.name
    account.delete()
    messages.success(request, f"{account_name} 계좌를 삭제했습니다.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def create_trade(request):
    form = TradeForm(request.POST)
    if form.is_valid():
        account = form.cleaned_data["account"]
        stock = form.cleaned_data["stock"]
        trade_type = form.cleaned_data["trade_type"]
        quantity = int(form.cleaned_data["quantity"])
        price = int(form.cleaned_data["price"])
        fee = int(form.cleaned_data["fee"] or 0)
        tax = int(form.cleaned_data["tax"] or 0)

        try:
            post_trade_cash = _parse_optional_non_negative_int(request.POST.get("post_trade_cash"))
        except (TypeError, ValueError):
            messages.error(request, "거래후 예수금은 0 이상의 숫자로 입력해주세요.")
            return redirect("portfolio:dashboard")

        gross_amount = quantity * price
        if post_trade_cash is not None:
            base_balance = _current_account_cash_balance(account, up_to_date=form.cleaned_data["traded_on"])
            tax = 0
            if trade_type == Trade.BUY:
                fee = base_balance - gross_amount - post_trade_cash
            else:
                fee = base_balance + gross_amount - post_trade_cash
            if fee < 0:
                messages.error(request, "거래후 예수금 기준으로 계산한 수수료가 음수입니다. 값을 확인해주세요.")
                return redirect("portfolio:dashboard")

        cash_delta = (
            -(gross_amount + fee + tax)
            if trade_type == Trade.BUY
            else gross_amount - fee - tax
        )

        base_balance = _current_account_cash_balance(account, up_to_date=form.cleaned_data["traded_on"])
        new_balance = base_balance + cash_delta

        if new_balance < 0:
            messages.error(request, "매수 후 예수금이 음수가 되어 거래를 저장할 수 없습니다.")
            return redirect("portfolio:dashboard")

        latest_holding_snapshot = (
            HoldingSnapshot.objects.filter(account=account, stock=stock).order_by("-as_of_date", "-id").first()
        )
        base_quantity = latest_holding_snapshot.quantity if latest_holding_snapshot else 0
        base_avg_price = latest_holding_snapshot.avg_price if latest_holding_snapshot else 0
        base_current_price = latest_holding_snapshot.current_price if latest_holding_snapshot else price

        if trade_type == Trade.SELL and quantity > base_quantity:
            messages.error(request, "매도 수량이 현재 보유 수량보다 많아 거래를 저장할 수 없습니다.")
            return redirect("portfolio:dashboard")

        if trade_type == Trade.BUY:
            new_quantity = base_quantity + quantity
            new_avg_price = int(
                (
                    Decimal(base_quantity * base_avg_price + quantity * price) / Decimal(new_quantity)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        else:
            new_quantity = base_quantity - quantity
            new_avg_price = base_avg_price if new_quantity > 0 else 0

        with transaction.atomic():
            trade_entry = form.save(commit=False)
            trade_entry.fee = fee
            trade_entry.tax = tax
            trade_entry.save()
            current_balance = _current_account_cash_balance(account)
            AccountSnapshot.objects.update_or_create(
                account=account,
                as_of_date=date.today(),
                defaults={"cash_balance": current_balance},
            )
            HoldingSnapshot.objects.update_or_create(
                account=account,
                stock=stock,
                as_of_date=date.today(),
                defaults={
                    "quantity": new_quantity,
                    "avg_price": new_avg_price,
                    "current_price": base_current_price,
                },
            )
        messages.success(request, "거래 내역과 예수금이 반영되었습니다.")
    else:
        error_text = "; ".join(
            f"{' '.join(errs)}" for errs in form.errors.values()
        ) or "값을 확인해주세요."
        messages.error(request, f"거래 내역 저장에 실패했습니다. {error_text}")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def update_cashflow(request, pk):
    cashflow = get_object_or_404(CashFlow.objects.select_related("account"), pk=pk)
    form = CashFlowForm(request.POST)
    if not form.is_valid():
        messages.error(request, "입출금 내역 수정에 실패했습니다. 값을 확인해주세요.")
        return redirect("portfolio:dashboard")

    new_account = form.cleaned_data["account"]
    new_flow_type = form.cleaned_data["flow_type"]
    new_amount = int(form.cleaned_data["amount"])
    new_occurred_on = form.cleaned_data["occurred_on"]
    new_memo = form.cleaned_data["memo"]
    new_delta = new_amount if new_flow_type == CashFlow.DEPOSIT else -new_amount

    projected_new_balance = _current_account_cash_balance(
        new_account,
        up_to_date=new_occurred_on,
        exclude_cashflow_id=cashflow.id,
    ) + new_delta
    if projected_new_balance < 0:
        messages.error(request, "수정 후 예수금이 음수가 되어 저장할 수 없습니다.")
        return redirect("portfolio:dashboard")

    old_account = cashflow.account
    affected_accounts = {
        old_account.id: old_account,
        new_account.id: new_account,
    }

    with transaction.atomic():
        cashflow.account = new_account
        cashflow.flow_type = new_flow_type
        cashflow.amount = new_amount
        cashflow.occurred_on = new_occurred_on
        cashflow.memo = new_memo
        cashflow.save(update_fields=["account", "flow_type", "amount", "occurred_on", "memo"])

        for account in affected_accounts.values():
            current_balance = _current_account_cash_balance(account)
            AccountSnapshot.objects.update_or_create(
                account=account,
                as_of_date=date.today(),
                defaults={"cash_balance": current_balance},
            )

    messages.success(request, "입출금 내역을 수정했습니다.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def update_trade(request, pk):
    trade = get_object_or_404(Trade.objects.select_related("account", "stock"), pk=pk)
    form = TradeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "거래 내역 수정에 실패했습니다. 값을 확인해주세요.")
        return redirect("portfolio:dashboard")

    new_account = form.cleaned_data["account"]
    new_stock = form.cleaned_data["stock"]
    new_trade_type = form.cleaned_data["trade_type"]
    new_quantity = int(form.cleaned_data["quantity"])
    new_price = int(form.cleaned_data["price"])
    new_fee = int(form.cleaned_data["fee"] or 0)
    new_tax = int(form.cleaned_data["tax"] or 0)
    new_traded_on = form.cleaned_data["traded_on"]
    new_memo = form.cleaned_data["memo"]

    try:
        post_trade_cash = _parse_optional_non_negative_int(request.POST.get("post_trade_cash"))
    except (TypeError, ValueError):
        messages.error(request, "거래후 예수금은 0 이상의 숫자로 입력해주세요.")
        return redirect("portfolio:dashboard")

    old_account = trade.account
    old_stock = trade.stock

    old_effect = trade.net_cash_effect
    base_new_account_balance = _current_account_cash_balance(
        new_account,
        up_to_date=new_traded_on,
        exclude_trade_id=trade.pk,
    )

    if post_trade_cash is not None:
        new_gross = new_quantity * new_price
        new_tax = 0
        if new_trade_type == Trade.BUY:
            new_fee = base_new_account_balance - new_gross - post_trade_cash
        else:
            new_fee = base_new_account_balance + new_gross - post_trade_cash
        if new_fee < 0:
            messages.error(request, "거래후 예수금 기준으로 계산한 수수료가 음수입니다. 값을 확인해주세요.")
            return redirect("portfolio:dashboard")

    new_effect_base = new_quantity * new_price + new_fee + new_tax
    new_effect = -new_effect_base if new_trade_type == Trade.BUY else (new_quantity * new_price - new_fee - new_tax)

    affected_accounts = {
        old_account.id: old_account,
        new_account.id: new_account,
    }
    old_projected_balance = _current_account_cash_balance(old_account) - old_effect
    new_projected_balance = base_new_account_balance + new_effect
    if old_account.id == new_account.id:
        old_projected_balance = new_projected_balance

    if old_projected_balance < 0:
        messages.error(request, f"{old_account.name} 계좌 예수금이 음수가 되어 거래를 수정할 수 없습니다.")
        return redirect("portfolio:dashboard")
    if new_projected_balance < 0:
        messages.error(request, f"{new_account.name} 계좌 예수금이 음수가 되어 거래를 수정할 수 없습니다.")
        return redirect("portfolio:dashboard")

    replacement_trade = {
        "id": trade.pk,
        "trade_type": new_trade_type,
        "quantity": new_quantity,
        "price": new_price,
        "traded_on": new_traded_on,
    }
    affected_pairs = {
        (old_account.id, old_stock.id): (old_account, old_stock, None),
        (new_account.id, new_stock.id): (new_account, new_stock, replacement_trade),
    }

    projected_positions: dict[tuple[int, int], tuple[int, int, int]] = {}
    for key, pair in affected_pairs.items():
        account, stock, additional_trade = pair
        try:
            projected_quantity, projected_avg_price = _calculate_position_from_trades(
                account=account,
                stock=stock,
                exclude_trade_id=trade.pk,
                additional_trade=additional_trade,
            )
        except ValueError:
            messages.error(request, "매도 수량이 보유 수량보다 많아 거래를 수정할 수 없습니다.")
            return redirect("portfolio:dashboard")
        current_price = _latest_holding_current_price(account, stock, fallback_price=new_price)
        projected_positions[key] = (projected_quantity, projected_avg_price, current_price)

    with transaction.atomic():
        trade.account = new_account
        trade.stock = new_stock
        trade.trade_type = new_trade_type
        trade.quantity = new_quantity
        trade.price = new_price
        trade.fee = new_fee
        trade.tax = new_tax
        trade.traded_on = new_traded_on
        trade.memo = new_memo
        trade.save(
            update_fields=[
                "account",
                "stock",
                "trade_type",
                "quantity",
                "price",
                "fee",
                "tax",
                "traded_on",
                "memo",
            ]
        )

        for account in affected_accounts.values():
            updated_balance = _current_account_cash_balance(account)
            AccountSnapshot.objects.update_or_create(
                account=account,
                as_of_date=date.today(),
                defaults={"cash_balance": updated_balance},
            )

        for (account_id, stock_id), values in projected_positions.items():
            quantity, avg_price, current_price = values
            HoldingSnapshot.objects.update_or_create(
                account_id=account_id,
                stock_id=stock_id,
                as_of_date=date.today(),
                defaults={
                    "quantity": quantity,
                    "avg_price": avg_price,
                    "current_price": current_price,
                },
            )

    messages.success(request, "거래 내역을 수정했습니다.")
    return redirect("portfolio:dashboard")


@require_http_methods(["GET"])
def preview_trade_fee(request):
    account_id = request.GET.get("account")
    trade_type = (request.GET.get("trade_type") or "").strip()
    quantity_raw = request.GET.get("quantity")
    price_raw = request.GET.get("price")
    traded_on_raw = request.GET.get("traded_on")
    post_trade_cash_raw = request.GET.get("post_trade_cash")
    exclude_trade_id_raw = request.GET.get("exclude_trade_id")

    try:
        account = Account.objects.get(pk=int(account_id))
        quantity = int(quantity_raw or "0")
        price = int(price_raw or "0")
        traded_on = date.fromisoformat(traded_on_raw or "")
        post_trade_cash = _parse_optional_non_negative_int(post_trade_cash_raw)
        exclude_trade_id = int(exclude_trade_id_raw) if exclude_trade_id_raw else None
    except (Account.DoesNotExist, TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "입력값이 올바르지 않습니다."}, status=400)

    if trade_type not in {Trade.BUY, Trade.SELL}:
        return JsonResponse({"ok": False, "message": "거래 구분이 올바르지 않습니다."}, status=400)
    if quantity <= 0 or price <= 0:
        return JsonResponse({"ok": False, "message": "수량/단가는 1 이상이어야 합니다."}, status=400)
    if post_trade_cash is None:
        return JsonResponse({"ok": False, "message": "거래후 예수금을 입력해주세요."}, status=400)

    base_balance = _current_account_cash_balance(
        account,
        up_to_date=traded_on,
        exclude_trade_id=exclude_trade_id,
    )
    gross_amount = quantity * price
    if trade_type == Trade.BUY:
        fee = base_balance - gross_amount - post_trade_cash
    else:
        fee = base_balance + gross_amount - post_trade_cash

    if fee < 0:
        return JsonResponse(
            {"ok": False, "message": "계산된 수수료가 음수입니다. 거래후 예수금을 확인해주세요."},
            status=400,
        )

    return JsonResponse(
        {
            "ok": True,
            "fee": fee,
            "base_cash_balance": base_balance,
            "traded_on": traded_on.isoformat(),
        }
    )


@require_http_methods(["POST"])
def update_prices(request):
    target_date = date.today()
    try:
        updated_count = sync_latest_prices(target_date)
        latest_holding_date = HoldingSnapshot.objects.aggregate(last_date=Max("as_of_date"))["last_date"]
        holding_stock_ids = set()
        if latest_holding_date:
            holding_stock_ids = set(
                HoldingSnapshot.objects.filter(as_of_date=latest_holding_date).values_list("stock_id", flat=True)
            )
        rebalance_stock_ids = set(RebalanceStock.objects.values_list("stock_id", flat=True))
        target_stock_ids = holding_stock_ids | rebalance_stock_ids
        priced_stock_ids = set(PriceHistory.objects.filter(price_date=target_date).values_list("stock_id", flat=True))
        missing_count = len(target_stock_ids - priced_stock_ids)
        renamed_count, skipped_count = sync_stock_names()
        messages.success(
            request,
            (
                f"{target_date} 기준 가격 {updated_count}개 업데이트, "
                f"종목명 {renamed_count}개 동기화 "
                f"(이름 조회 불가 {skipped_count}개, 가격 미수집 {missing_count}개)"
            ),
        )
    except RuntimeError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # pragma: no cover
        messages.error(request, f"가격 업데이트 중 오류가 발생했습니다: {exc}")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def add_rebalance_category(request):
    form = RebalanceCategoryCreateForm(request.POST)
    if form.is_valid():
        category_name = form.cleaned_data["category_name"].strip()
        target_weight = form.cleaned_data["target_weight"]
        category, category_created = Category.objects.get_or_create(
            name=category_name,
            defaults={"target_weight": target_weight},
        )
        if not category_created and category.target_weight != target_weight:
            category.target_weight = target_weight
            category.save(update_fields=["target_weight"])

        existing_entry = RebalanceCategory.objects.filter(category=category).first()
        old_weight = existing_entry.target_weight if existing_entry else None
        _, created = RebalanceCategory.objects.update_or_create(
            category=category,
            defaults={"target_weight": target_weight},
        )
        if created:
            RebalanceCategoryHistory.objects.create(
                category=category,
                old_weight=None,
                new_weight=target_weight,
                change_type=RebalanceCategoryHistory.CREATED,
            )
        elif old_weight is not None and old_weight != target_weight:
            RebalanceCategoryHistory.objects.create(
                category=category,
                old_weight=old_weight,
                new_weight=target_weight,
                change_type=RebalanceCategoryHistory.UPDATED,
            )

        if created:
            messages.success(request, "리밸런싱 카테고리를 추가했습니다.")
        elif old_weight is not None and old_weight != target_weight:
            messages.success(request, "카테고리 목표비중을 업데이트했습니다.")
        else:
            messages.success(request, "변경된 값이 없어 기존 설정을 유지했습니다.")
    else:
        messages.error(request, "카테고리 추가에 실패했습니다. 입력값을 확인해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def delete_rebalance_category(request, pk):
    entry = get_object_or_404(RebalanceCategory, pk=pk)
    category_name = entry.category.name
    entry.delete()
    messages.success(request, f"{category_name} 카테고리를 리밸런싱 리스트에서 삭제했습니다.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def update_rebalance_category(request, pk):
    entry = get_object_or_404(RebalanceCategory, pk=pk)
    form = RebalanceCategoryUpdateForm(request.POST)
    if form.is_valid():
        target_weight = form.cleaned_data["target_weight"]
        old_weight = entry.target_weight
        if old_weight != target_weight:
            entry.target_weight = target_weight
            entry.save(update_fields=["target_weight"])

            if entry.category.target_weight != target_weight:
                entry.category.target_weight = target_weight
                entry.category.save(update_fields=["target_weight"])

            RebalanceCategoryHistory.objects.create(
                category=entry.category,
                old_weight=old_weight,
                new_weight=target_weight,
                change_type=RebalanceCategoryHistory.UPDATED,
            )
            messages.success(request, f"{entry.category.name} 목표비중을 수정했습니다.")
        else:
            messages.success(request, "변경된 값이 없어 기존 설정을 유지했습니다.")
    else:
        messages.error(request, "목표비중 수정에 실패했습니다. 0~100 범위 값을 입력해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def add_rebalance_stock(request):
    form = RebalanceStockCreateForm(request.POST)
    if form.is_valid():
        rebalance_category = form.cleaned_data["rebalance_category"]
        ticker = form.cleaned_data["ticker"]
        entered_name = form.cleaned_data["stock_name"]
        target_weight = form.cleaned_data["target_weight"]
        requires_jonghap = bool(form.cleaned_data.get("requires_jonghap_account")) or default_requires_jonghap_account(
            ticker
        )
        resolved_name = lookup_stock_name_by_ticker(ticker, prefer_existing=False)
        stock_name = resolved_name or entered_name

        if not stock_name:
            messages.error(
                request,
                "티커로 종목명을 조회하지 못했습니다. 종목명을 직접 입력해주세요.",
            )
            return redirect("portfolio:dashboard")

        existing_stock = Stock.objects.filter(ticker=ticker).select_related("category").first()
        old_category_name = existing_stock.category.name if existing_stock else ""
        old_stock_name = existing_stock.name if existing_stock else ""

        stock, stock_created = Stock.objects.get_or_create(
            ticker=ticker,
            defaults={
                "name": stock_name,
                "category": rebalance_category.category,
                "requires_jonghap_account": requires_jonghap,
            },
        )

        existing_rebalance = (
            RebalanceStock.objects.filter(stock=stock)
            .select_related("rebalance_category__category")
            .first()
        )
        if existing_rebalance:
            old_category_name = existing_rebalance.rebalance_category.category.name

        stock_updated = False
        if not stock_created:
            update_fields = []
            if stock.name != stock_name:
                stock.name = stock_name
                update_fields.append("name")
            if stock.category_id != rebalance_category.category_id:
                stock.category = rebalance_category.category
                update_fields.append("category")
            if stock.requires_jonghap_account != requires_jonghap:
                stock.requires_jonghap_account = requires_jonghap
                update_fields.append("requires_jonghap_account")
            if update_fields:
                stock.save(update_fields=update_fields)
                stock_updated = True

        existing_rebalance_weight = existing_rebalance.target_weight if existing_rebalance else None
        _, rebalance_created = RebalanceStock.objects.update_or_create(
            stock=stock,
            defaults={"rebalance_category": rebalance_category, "target_weight": target_weight},
        )
        category_changed = bool(
            existing_rebalance and existing_rebalance.rebalance_category_id != rebalance_category.id
        )
        target_weight_changed = (
            existing_rebalance_weight is not None and existing_rebalance_weight != target_weight
        )

        if rebalance_created:
            RebalanceStockHistory.objects.create(
                ticker=stock.ticker,
                stock_name=stock.name,
                old_category_name="",
                new_category_name=rebalance_category.category.name,
                change_type=RebalanceStockHistory.ADDED,
            )
        elif stock_updated or category_changed or target_weight_changed:
            RebalanceStockHistory.objects.create(
                ticker=stock.ticker,
                stock_name=stock.name or old_stock_name,
                old_category_name=old_category_name,
                new_category_name=rebalance_category.category.name,
                change_type=RebalanceStockHistory.UPDATED,
            )

        if resolved_name:
            messages.success(request, f"리밸런싱 종목을 추가했습니다. (종목명 자동조회: {resolved_name})")
        else:
            messages.success(request, "리밸런싱 종목을 추가했습니다.")
    else:
        messages.error(request, "종목 추가에 실패했습니다. 카테고리와 종목 매칭을 확인해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["GET"])
def lookup_stock_name(request):
    ticker = (request.GET.get("ticker") or "").strip().upper()
    if not ticker:
        return JsonResponse({"ok": False, "message": "티커를 입력해주세요."}, status=400)

    resolved_name = lookup_stock_name_by_ticker(ticker, prefer_existing=False)
    if resolved_name:
        return JsonResponse({"ok": True, "ticker": ticker, "stock_name": resolved_name})
    return JsonResponse(
        {
            "ok": False,
            "ticker": ticker,
            "message": "티커로 종목명을 찾지 못했습니다. 종목명을 직접 입력해주세요.",
        },
        status=404,
    )


@require_http_methods(["POST"])
def upsert_manual_price(request):
    form = ManualPriceForm(request.POST)
    if form.is_valid():
        stock = form.cleaned_data["stock"]
        price_date = form.cleaned_data["price_date"]
        close_price = form.cleaned_data["close_price"]
        PriceHistory.objects.update_or_create(
            stock=stock,
            price_date=price_date,
            defaults={"close_price": close_price, "source": "manual"},
        )
        messages.success(request, f"{stock.ticker} {price_date} 종가를 수동 저장했습니다.")
    else:
        messages.error(request, "수동 종가 저장에 실패했습니다. 입력값을 확인해주세요.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def update_rebalance_stock(request, pk):
    entry = get_object_or_404(
        RebalanceStock.objects.select_related("rebalance_category__category", "stock", "stock__category"),
        pk=pk,
    )
    form = RebalanceStockUpdateForm(request.POST)
    if form.is_valid():
        new_rebalance_category = form.cleaned_data["rebalance_category"]
        target_weight = form.cleaned_data["target_weight"]
        requires_jonghap = bool(form.cleaned_data.get("requires_jonghap_account"))
        old_category_name = entry.rebalance_category.category.name
        new_category_name = new_rebalance_category.category.name
        changed = False

        if entry.rebalance_category_id != new_rebalance_category.id:
            entry.rebalance_category = new_rebalance_category
            changed = True

            if entry.stock.category_id != new_rebalance_category.category_id:
                entry.stock.category = new_rebalance_category.category
                entry.stock.save(update_fields=["category"])

        if entry.target_weight != target_weight:
            entry.target_weight = target_weight
            changed = True

        if entry.stock.requires_jonghap_account != requires_jonghap:
            entry.stock.requires_jonghap_account = requires_jonghap
            entry.stock.save(update_fields=["requires_jonghap_account"])
            changed = True

        if changed:
            entry.save(update_fields=["rebalance_category", "target_weight"])
            RebalanceStockHistory.objects.create(
                ticker=entry.stock.ticker,
                stock_name=entry.stock.name,
                old_category_name=old_category_name,
                new_category_name=new_category_name,
                change_type=RebalanceStockHistory.UPDATED,
            )
            messages.success(request, f"{entry.stock.ticker} 종목 설정을 수정했습니다.")
        else:
            messages.success(request, "변경된 값이 없어 기존 설정을 유지했습니다.")
    else:
        messages.error(request, "카테고리 수정에 실패했습니다. 값을 확인해주세요.")
    return redirect("portfolio:dashboard")


def _equalize_category_stock_weights(rebalance_category: RebalanceCategory) -> bool:
    entries = list(RebalanceStock.objects.filter(rebalance_category=rebalance_category).order_by("stock__ticker"))
    if not entries:
        return False

    total_weight = Decimal(rebalance_category.target_weight)
    count = len(entries)
    base_weight = (total_weight / count).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    assigned_total = Decimal("0.00")
    for idx, entry in enumerate(entries):
        if idx == count - 1:
            target_weight = (total_weight - assigned_total).quantize(Decimal("0.01"))
        else:
            target_weight = base_weight
            assigned_total += base_weight
        if entry.target_weight != target_weight:
            entry.target_weight = target_weight
            entry.save(update_fields=["target_weight"])
    return True


@require_http_methods(["POST"])
def equalize_all_rebalance_stocks(request):
    categories = RebalanceCategory.objects.select_related("category").all()
    changed_categories = 0
    for rebalance_category in categories:
        if _equalize_category_stock_weights(rebalance_category):
            changed_categories += 1

    if changed_categories == 0:
        messages.error(request, "균등 배분할 종목이 없습니다.")
    else:
        messages.success(request, f"{changed_categories}개 카테고리 종목 비중을 균등 배분했습니다.")
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def equalize_rebalance_stocks(request, pk):
    rebalance_category = get_object_or_404(RebalanceCategory.objects.select_related("category"), pk=pk)
    _equalize_category_stock_weights(rebalance_category)
    messages.success(
        request,
        f"{rebalance_category.category.name} 카테고리 종목 비중을 균등 배분했습니다.",
    )
    return redirect("portfolio:dashboard")


@require_http_methods(["POST"])
def delete_rebalance_stock(request, pk):
    entry = get_object_or_404(RebalanceStock, pk=pk)
    RebalanceStockHistory.objects.create(
        ticker=entry.stock.ticker,
        stock_name=entry.stock.name,
        old_category_name=entry.rebalance_category.category.name,
        new_category_name="",
        change_type=RebalanceStockHistory.DELETED,
    )
    stock_label = f"{entry.stock.ticker} {entry.stock.name}"
    entry.delete()
    messages.success(request, f"{stock_label} 종목을 리밸런싱 리스트에서 삭제했습니다.")
    return redirect("portfolio:dashboard")
