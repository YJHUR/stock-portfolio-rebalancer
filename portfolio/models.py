from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    target_weight = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return f"{self.name} ({self.target_weight}%)"


class Stock(models.Model):
    ticker = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="stocks")

    class Meta:
        ordering = ["ticker"]

    def __str__(self) -> str:
        return f"{self.ticker} {self.name}"


class InstrumentAlias(models.Model):
    stock = models.OneToOneField(Stock, on_delete=models.CASCADE, related_name="instrument_alias")
    krx_ticker = models.CharField(max_length=10, blank=True)
    naver_symbol = models.CharField(max_length=10, blank=True)
    yahoo_symbol = models.CharField(max_length=20, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stock__ticker"]
        verbose_name_plural = "instrument aliases"

    def __str__(self) -> str:
        return f"{self.stock.ticker} alias"


class Account(models.Model):
    name = models.CharField(max_length=50, unique=True)
    account_group = models.CharField(max_length=50, blank=True, default="")
    initial_balance = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CashFlow(models.Model):
    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"
    FLOW_TYPES = [
        (DEPOSIT, "입금"),
        (WITHDRAW, "출금"),
    ]

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="cashflows")
    flow_type = models.CharField(max_length=10, choices=FLOW_TYPES)
    amount = models.PositiveBigIntegerField()
    occurred_on = models.DateField()
    memo = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_on", "-id"]

    def __str__(self) -> str:
        return f"{self.account.name} {self.get_flow_type_display()} {self.amount:,}"


class Trade(models.Model):
    BUY = "BUY"
    SELL = "SELL"
    TRADE_TYPES = [
        (BUY, "매수"),
        (SELL, "매도"),
    ]

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="trades")
    stock = models.ForeignKey(Stock, on_delete=models.PROTECT, related_name="trades")
    trade_type = models.CharField(max_length=4, choices=TRADE_TYPES)
    quantity = models.PositiveIntegerField()
    price = models.PositiveIntegerField(help_text="1주당 체결 단가")
    fee = models.PositiveIntegerField(default=0)
    tax = models.PositiveIntegerField(default=0)
    traded_on = models.DateField()
    memo = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-traded_on", "-id"]

    @property
    def gross_amount(self) -> int:
        return self.quantity * self.price

    @property
    def net_cash_effect(self) -> int:
        base = self.gross_amount + self.fee + self.tax
        return -base if self.trade_type == self.BUY else self.gross_amount - self.fee - self.tax

    def __str__(self) -> str:
        return f"{self.account.name} {self.stock.ticker} {self.get_trade_type_display()} {self.quantity}주"


class AccountSnapshot(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="snapshots")
    as_of_date = models.DateField()
    cash_balance = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["-as_of_date", "-id"]
        unique_together = [("account", "as_of_date")]

    def __str__(self) -> str:
        return f"{self.account.name} {self.as_of_date} 예수금 {self.cash_balance:,}"


class HoldingSnapshot(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="holding_snapshots")
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="holding_snapshots")
    as_of_date = models.DateField()
    quantity = models.PositiveIntegerField()
    avg_price = models.PositiveIntegerField()
    current_price = models.PositiveIntegerField()

    class Meta:
        ordering = ["-as_of_date", "stock__ticker"]
        unique_together = [("account", "stock", "as_of_date")]

    @property
    def market_value(self) -> int:
        return self.quantity * self.current_price

    @property
    def invested_value(self) -> int:
        return self.quantity * self.avg_price

    def __str__(self) -> str:
        return f"{self.account.name} {self.stock.ticker} {self.quantity}주"


class PriceHistory(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="prices")
    price_date = models.DateField()
    close_price = models.DecimalField(max_digits=16, decimal_places=4)
    source = models.CharField(max_length=20, default="pykrx")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-price_date", "stock__ticker"]
        unique_together = [("stock", "price_date")]

    def __str__(self) -> str:
        return f"{self.stock.ticker} {self.price_date} {self.close_price:,}"


class RebalanceCategory(models.Model):
    category = models.OneToOneField(Category, on_delete=models.CASCADE, related_name="rebalance_entry")
    target_weight = models.DecimalField(max_digits=5, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category__name"]

    def __str__(self) -> str:
        return f"{self.category.name} ({self.target_weight}%)"


class RebalanceCategoryHistory(models.Model):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    CHANGE_TYPES = [
        (CREATED, "생성"),
        (UPDATED, "수정"),
    ]

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="rebalance_histories")
    old_weight = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    new_weight = models.DecimalField(max_digits=5, decimal_places=2)
    change_type = models.CharField(max_length=10, choices=CHANGE_TYPES)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        verbose_name_plural = "rebalance category histories"

    def __str__(self) -> str:
        return f"{self.category.name} {self.old_weight} -> {self.new_weight}"


class RebalanceStock(models.Model):
    rebalance_category = models.ForeignKey(
        RebalanceCategory,
        on_delete=models.CASCADE,
        related_name="stocks",
    )
    stock = models.OneToOneField(Stock, on_delete=models.CASCADE, related_name="rebalance_entry")
    target_weight = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rebalance_category__category__name", "stock__ticker"]

    def __str__(self) -> str:
        return f"{self.rebalance_category.category.name} - {self.stock.ticker}"


class RebalanceStockHistory(models.Model):
    ADDED = "ADDED"
    UPDATED = "UPDATED"
    DELETED = "DELETED"
    CHANGE_TYPES = [
        (ADDED, "추가"),
        (UPDATED, "수정"),
        (DELETED, "삭제"),
    ]

    ticker = models.CharField(max_length=10)
    stock_name = models.CharField(max_length=100, blank=True)
    old_category_name = models.CharField(max_length=50, blank=True)
    new_category_name = models.CharField(max_length=50, blank=True)
    change_type = models.CharField(max_length=10, choices=CHANGE_TYPES)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        verbose_name_plural = "rebalance stock histories"

    def __str__(self) -> str:
        return f"{self.ticker} {self.old_category_name}->{self.new_category_name}"
