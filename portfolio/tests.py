from datetime import date
from decimal import Decimal

from django.test import TestCase

from portfolio.forms import TradeForm
from portfolio.models import (
    Account,
    AccountSnapshot,
    Category,
    HoldingSnapshot,
    PriceHistory,
    RebalanceCategory,
    RebalanceStock,
    Stock,
)
from portfolio.services import (
    account_can_trade_stock,
    default_requires_jonghap_account,
    grouped_rebalance_recommendations,
    is_jonghap_account,
)


class JonghapRestrictionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="리츠", target_weight=Decimal("100"))
        self.reb_cat = RebalanceCategory.objects.create(
            category=self.category,
            target_weight=Decimal("100"),
        )
        self.agnc = Stock.objects.create(
            ticker="AGNC",
            name="AGNC Investment",
            category=self.category,
            requires_jonghap_account=True,
        )
        self.nvda = Stock.objects.create(
            ticker="NVDA",
            name="NVIDIA",
            category=self.category,
            requires_jonghap_account=False,
        )
        RebalanceStock.objects.create(
            rebalance_category=self.reb_cat,
            stock=self.agnc,
            target_weight=Decimal("50"),
        )
        RebalanceStock.objects.create(
            rebalance_category=self.reb_cat,
            stock=self.nvda,
            target_weight=Decimal("50"),
        )
        today = date.today()
        PriceHistory.objects.create(stock=self.agnc, price_date=today, close_price=Decimal("10"), source="test")
        PriceHistory.objects.create(stock=self.nvda, price_date=today, close_price=Decimal("100"), source="test")

        self.isa = Account.objects.create(
            name="ISA",
            account_group="가족",
            initial_balance=10_000_000,
            is_jonghap=False,
        )
        self.jonghap = Account.objects.create(
            name="키움종합매매",
            account_group="가족",
            initial_balance=10_000_000,
            is_jonghap=True,
        )
        AccountSnapshot.objects.create(account=self.isa, as_of_date=today, cash_balance=10_000_000)
        AccountSnapshot.objects.create(account=self.jonghap, as_of_date=today, cash_balance=10_000_000)

    def test_default_tickers(self):
        self.assertTrue(default_requires_jonghap_account("AGNC"))
        self.assertTrue(default_requires_jonghap_account("NLY"))
        self.assertFalse(default_requires_jonghap_account("NVDA"))

    def test_is_jonghap_account(self):
        self.assertTrue(is_jonghap_account(self.jonghap))
        self.assertFalse(is_jonghap_account(self.isa))
        named = Account(name="미래에셋 종합매매", account_group="개인", is_jonghap=False)
        self.assertTrue(is_jonghap_account(named))

    def test_account_can_trade_stock(self):
        self.assertFalse(account_can_trade_stock(self.isa, self.agnc))
        self.assertTrue(account_can_trade_stock(self.jonghap, self.agnc))
        self.assertTrue(account_can_trade_stock(self.isa, self.nvda))

    def test_trade_form_blocks_buy_on_non_jonghap(self):
        form = TradeForm(
            data={
                "account": self.isa.id,
                "stock": self.agnc.id,
                "trade_type": "BUY",
                "quantity": 1,
                "price": 10000,
                "fee": 0,
                "tax": 0,
                "traded_on": date.today().isoformat(),
                "memo": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("종합매매", str(form.errors))

    def test_trade_form_allows_buy_on_jonghap(self):
        form = TradeForm(
            data={
                "account": self.jonghap.id,
                "stock": self.agnc.id,
                "trade_type": "BUY",
                "quantity": 1,
                "price": 10000,
                "fee": 0,
                "tax": 0,
                "traded_on": date.today().isoformat(),
                "memo": "",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_rebalance_buys_agnc_only_on_jonghap(self):
        results = grouped_rebalance_recommendations()
        self.assertEqual(len(results), 1)
        buys = [r for r in results[0]["trade_recommendations"] if r.action == "매수"]
        agnc_buys = [r for r in buys if r.ticker == "AGNC"]
        self.assertTrue(agnc_buys)
        for rec in agnc_buys:
            self.assertEqual(rec.account_name, self.jonghap.name)
