from django.urls import path

from . import views

app_name = "portfolio"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("portfolios/new/", views.create_portfolio, name="create_portfolio"),
    path("portfolios/<str:portfolio_id>/select/", views.select_portfolio, name="select_portfolio"),
    path("portfolios/<str:portfolio_id>/update/", views.update_portfolio, name="update_portfolio"),
    path("portfolios/<str:portfolio_id>/delete/", views.delete_portfolio, name="delete_portfolio"),
    path("accounts/new/", views.create_account, name="create_account"),
    path("accounts/<int:pk>/update/", views.update_account, name="update_account"),
    path("accounts/<int:pk>/delete/", views.delete_account, name="delete_account"),
    path("cashflows/new/", views.create_cashflow, name="create_cashflow"),
    path("cashflows/<int:pk>/update/", views.update_cashflow, name="update_cashflow"),
    path("trades/new/", views.create_trade, name="create_trade"),
    path("trades/<int:pk>/update/", views.update_trade, name="update_trade"),
    path("prices/update/", views.update_prices, name="update_prices"),
    path("rebalance/categories/add/", views.add_rebalance_category, name="add_rebalance_category"),
    path(
        "rebalance/categories/<int:pk>/delete/",
        views.delete_rebalance_category,
        name="delete_rebalance_category",
    ),
    path(
        "rebalance/categories/<int:pk>/update/",
        views.update_rebalance_category,
        name="update_rebalance_category",
    ),
    path("rebalance/stocks/add/", views.add_rebalance_stock, name="add_rebalance_stock"),
    path(
        "rebalance/categories/<int:pk>/stocks/equalize/",
        views.equalize_rebalance_stocks,
        name="equalize_rebalance_stocks",
    ),
    path(
        "rebalance/stocks/equalize-all/",
        views.equalize_all_rebalance_stocks,
        name="equalize_all_rebalance_stocks",
    ),
    path("prices/manual/", views.upsert_manual_price, name="upsert_manual_price"),
    path("stocks/lookup-name/", views.lookup_stock_name, name="lookup_stock_name"),
    path("trades/preview-fee/", views.preview_trade_fee, name="preview_trade_fee"),
    path("rebalance/stocks/<int:pk>/update/", views.update_rebalance_stock, name="update_rebalance_stock"),
    path("rebalance/stocks/<int:pk>/delete/", views.delete_rebalance_stock, name="delete_rebalance_stock"),
]
