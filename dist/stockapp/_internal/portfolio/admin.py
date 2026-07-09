from django.contrib import admin

from .models import (
    Account,
    AccountSnapshot,
    CashFlow,
    Category,
    HoldingSnapshot,
    PriceHistory,
    RebalanceCategory,
    RebalanceStock,
    Stock,
    Trade,
)

admin.site.register(Category)
admin.site.register(Stock)
admin.site.register(Account)
admin.site.register(CashFlow)
admin.site.register(Trade)
admin.site.register(AccountSnapshot)
admin.site.register(HoldingSnapshot)
admin.site.register(PriceHistory)
admin.site.register(RebalanceCategory)
admin.site.register(RebalanceStock)
