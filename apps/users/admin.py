from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Wallet, InvestmentLock, WithdrawalRequest, UserKYCProfile, KYCAuditLog

# 1. Custom User Admin
if not admin.site.is_registered(User):
    admin.site.register(User, UserAdmin)

# 2. Financial & Wallet Admins
@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'updated_at')
    search_fields = ('user__email',)
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

# Helper to safely select fields that exist on a model
def get_safe_fields(model, preferred):
    model_field_names = {f.name for f in model._meta.get_fields()}
    return [name for name in preferred if name in model_field_names]

# 3. Dynamic Safe Registration for UserKYCProfile
@admin.register(UserKYCProfile)
class UserKYCProfileAdmin(admin.ModelAdmin):
    list_display = get_safe_fields(
        UserKYCProfile,
        ['user', 'tier', 'kyc_tier', 'id_type', 'id_masked', 'face_verified', 'face_match_score', 'address_verified', 'created_at', 'updated_at']
    ) or ['id', 'user']
    list_filter = get_safe_fields(
        UserKYCProfile,
        ['tier', 'kyc_tier', 'face_verified', 'address_verified', 'id_type']
    )
    readonly_fields = get_safe_fields(
        UserKYCProfile,
        ['id_hash', 'created_at', 'updated_at']
    )
    search_fields = ('user__email', 'user__first_name', 'user__last_name')

# 4. Dynamic Safe Registration for KYCAuditLog
@admin.register(KYCAuditLog)
class KYCAuditLogAdmin(admin.ModelAdmin):
    list_display = get_safe_fields(
        KYCAuditLog,
        ['user', 'action', 'event', 'status', 'fuzzy_score', 'ip_address', 'created_at', 'timestamp']
    ) or ['id', 'user']
    list_filter = get_safe_fields(
        KYCAuditLog,
        ['action', 'event', 'status']
    )
    readonly_fields = get_safe_fields(
        KYCAuditLog,
        ['created_at', 'timestamp']
    )
    search_fields = ('user__email',)