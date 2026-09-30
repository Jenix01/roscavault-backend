from django.contrib import admin
from .models import ContributionGroup, GroupMembership, Transaction, PayoutSchedule

@admin.register(ContributionGroup)
class ContributionGroupAdmin(admin.ModelAdmin):
    # This defines the columns you will see in the admin table
    list_display = ('name', 'amount', 'max_members', 'creator', 'is_active')
    search_fields = ('name', 'description')
    list_filter = ('is_active',)
    
@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
    # Displays these columns in the admin list view
    list_display = ('user', 'group', 'role', 'is_active')
    list_filter = ('role', 'is_active', 'group')
    search_fields = ('user__email', 'group__name')
    
@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('membership', 'amount', 'reference', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('reference', 'membership__user__email', 'membership__group__name')
    # We make these read-only in the admin panel so no one can manually alter financial records
    readonly_fields = ('reference', 'amount', 'membership')
    
@admin.register(PayoutSchedule)
class PayoutScheduleAdmin(admin.ModelAdmin):
    list_display = ('group', 'member', 'cycle_number', 'expected_payout_date', 'payout_amount', 'status')
    list_filter = ('status', 'group', 'expected_payout_date')
    search_fields = ('group__name', 'member__user__email')
    ordering = ('group', 'cycle_number')
    
