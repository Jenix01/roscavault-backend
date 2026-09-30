from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


admin.site.register(User, UserAdmin)

from .models import User, Wallet, InvestmentLock, WithdrawalRequest

# ... (keep your existing CustomUserAdmin here) ...

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'updated_at')
    search_fields = ('user__email',)
    # Prevent manual editing of balances to maintain financial integrity
    readonly_fields = ('balance',)

@admin.register(InvestmentLock)
class InvestmentLockAdmin(admin.ModelAdmin):
    list_display = ('wallet', 'principal_amount', 'annual_interest_rate', 'locked_date', 'maturity_date', 'status')
    list_filter = ('status',)
    search_fields = ('wallet__user__email',)

@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ('wallet', 'total_amount', 'investment_percentage', 'cash_withdrawal_amount', 'status')
    list_filter = ('status',)
    search_fields = ('wallet__user__email',)