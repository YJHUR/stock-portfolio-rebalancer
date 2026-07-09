from django import forms
from decimal import Decimal

from .models import Account, CashFlow, RebalanceCategory, Stock, Trade


class DateInput(forms.DateInput):
    input_type = "date"


class CashFlowForm(forms.ModelForm):
    class Meta:
        model = CashFlow
        fields = ["account", "flow_type", "amount", "occurred_on", "memo"]
        widgets = {
            "occurred_on": DateInput(),
        }


class TradeForm(forms.ModelForm):
    class Meta:
        model = Trade
        fields = [
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
        widgets = {
            "traded_on": DateInput(),
        }


class RebalanceCategoryCreateForm(forms.Form):
    category_name = forms.CharField(max_length=50, label="카테고리명", widget=forms.TextInput(attrs={"placeholder": "예: 테크"}))
    target_weight = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        label="목표비중(%)",
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "예: 5"}),
    )


class RebalanceCategoryUpdateForm(forms.Form):
    target_weight = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        label="목표비중(%)",
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )


class RebalanceStockCreateForm(forms.Form):
    rebalance_category = forms.ModelChoiceField(queryset=RebalanceCategory.objects.none(), label="카테고리")
    ticker = forms.CharField(max_length=10, label="종목코드", widget=forms.TextInput(attrs={"placeholder": "예: 478150"}))
    stock_name = forms.CharField(
        max_length=100,
        required=False,
        label="종목명",
        widget=forms.TextInput(attrs={"placeholder": "선택 입력 (티커 조회 실패 시)"}),
    )
    target_weight = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        label="종목목표비중(%)",
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "예: 5"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rebalance_category"].queryset = RebalanceCategory.objects.select_related("category").order_by(
            "category__name"
        )

    def clean_ticker(self):
        ticker = self.cleaned_data["ticker"].strip().upper()
        if not ticker:
            raise forms.ValidationError("종목코드를 입력해주세요.")
        return ticker

    def clean_stock_name(self):
        return self.cleaned_data["stock_name"].strip()

    def clean(self):
        return super().clean()


class RebalanceStockUpdateForm(forms.Form):
    rebalance_category = forms.ModelChoiceField(queryset=RebalanceCategory.objects.none(), label="카테고리")
    target_weight = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        label="종목목표비중(%)",
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rebalance_category"].queryset = RebalanceCategory.objects.select_related("category").order_by(
            "category__name"
        )


class InstrumentAliasForm(forms.Form):
    stock = forms.ModelChoiceField(queryset=Stock.objects.none(), label="종목")
    krx_ticker = forms.CharField(
        max_length=10, required=False, label="KRX 코드", widget=forms.TextInput(attrs={"placeholder": "예: 395270"})
    )
    naver_symbol = forms.CharField(
        max_length=10, required=False, label="네이버 코드", widget=forms.TextInput(attrs={"placeholder": "예: 395270"})
    )
    yahoo_symbol = forms.CharField(
        max_length=20, required=False, label="Yahoo 심볼", widget=forms.TextInput(attrs={"placeholder": "예: AGNC"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stock"].queryset = Stock.objects.order_by("ticker")

    def clean_krx_ticker(self):
        return self.cleaned_data["krx_ticker"].strip().upper()

    def clean_naver_symbol(self):
        return self.cleaned_data["naver_symbol"].strip()

    def clean_yahoo_symbol(self):
        return self.cleaned_data["yahoo_symbol"].strip().upper()


class ManualPriceForm(forms.Form):
    stock = forms.ModelChoiceField(queryset=Stock.objects.none(), label="종목")
    price_date = forms.DateField(widget=DateInput(), label="가격일")
    close_price = forms.DecimalField(max_digits=16, decimal_places=4, min_value=Decimal("0.0001"), label="종가")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stock"].queryset = Stock.objects.order_by("ticker")


class AccountCreateForm(forms.Form):
    account_group = forms.CharField(max_length=50, label="그룹")
    account_name = forms.CharField(max_length=50, label="계좌명")
    cash_balance = forms.IntegerField(min_value=0, label="최초입금액")

    def clean_account_group(self):
        return self.cleaned_data["account_group"].strip()

    def clean_account_name(self):
        return self.cleaned_data["account_name"].strip()


class AccountUpdateForm(forms.Form):
    account_group = forms.CharField(max_length=50, label="그룹")
    account_name = forms.CharField(max_length=50, label="계좌명")
    initial_balance = forms.IntegerField(min_value=0, label="최초입금액")

    def clean_account_group(self):
        return self.cleaned_data["account_group"].strip()

    def clean_account_name(self):
        return self.cleaned_data["account_name"].strip()
