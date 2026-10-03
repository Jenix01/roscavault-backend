from django.db import models
from django.conf import settings
from apps.core.models import AbstractBaseModel
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal


class ContributionGroup(AbstractBaseModel):
    """
    Model representing an AJO savings/contribution cycle.
    """
    name = models.CharField(max_length=255, help_text="Name of the AJO group")
    description = models.TextField(blank=True, help_text="Rules or description of the cycle")
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Amount each member contributes per cycle")
    max_members = models.PositiveIntegerField(help_text="Maximum number of participants allowed")
    
    # Platform Commission & Ajo Laws
    alajo_fee_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('2.00'),  # 2% Alajo Platform Fee
        help_text="The Alajo commission fee percentage deducted upon payout distribution (e.g., 2.00 for 2%)."
    )
    cycle_frequency = models.CharField(
        max_length=20, 
        choices=[('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')],
        default='monthly'
    )
    deadline_time = models.TimeField(
        default='12:00:00',
        help_text="The exact time of day the system will auto-deduct (e.g., 18:00:00 for 6 PM)."
    )
    deadline_day = models.IntegerField(
        default=1,
        help_text="The day of the week or month the deadline falls on."
    )
    
    start_date = models.DateField(default=timezone.now)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    daily_fine_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('2.00'), 
        help_text="The daily percentage fine levied on defaulters (e.g., 2.00 for 2%)."
    )
    # Link the group to the user who created it
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='created_groups'
    )
    is_active = models.BooleanField(default=True)

    @property
    def total_pool_amount(self):
        """Total gross contribution pool for one cycle."""
        return self.amount * self.max_members

    @property
    def platform_fee_amount(self):
        """Platform fee deducted from the pool (2%)."""
        return (self.total_pool_amount * (self.alajo_fee_percentage / Decimal('100.00'))).quantize(Decimal('0.01'))

    @property
    def net_payout_amount(self):
        """Net amount disbursed to beneficiary after 2% fee deduction."""
        return self.total_pool_amount - self.platform_fee_amount

    def __str__(self):
        return f"{self.name} - ₦{self.amount} ({self.alajo_fee_percentage}% Fee)"
    

class GroupMembership(AbstractBaseModel):
    """
    Model representing a user's membership in a specific AJO group.
    """
    ROLE_CHOICES = (
        ('admin', 'Admin'),     # Can manage the group
        ('member', 'Member'),   # Standard participant
    )

    STATUS_CHOICES = (
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='memberships'
    )
    group = models.ForeignKey(
        ContributionGroup, 
        on_delete=models.CASCADE, 
        related_name='memberships'
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='member')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    current_cycle_number = models.IntegerField(
        default=1,
        help_text="Tracks which cycle the user is currently paying for."
    )
    is_active = models.BooleanField(
        default=False, 
        help_text="Active is True only when status is 'approved'"
    )
    next_deadline = models.DateTimeField(
        default=timezone.now,
        help_text="The exact date and time the auto-deduction runs"
    )

    class Meta:
        unique_together = ('user', 'group')

    def advance_to_next_cycle(self):
        self.current_cycle_number += 1
        if self.group.cycle_frequency == 'daily':
            self.next_deadline += timedelta(days=1)
        elif self.group.cycle_frequency == 'weekly':
            self.next_deadline += timedelta(weeks=1)
        elif self.group.cycle_frequency == 'monthly':
            self.next_deadline += timedelta(days=30) 
        self.save()

    def __str__(self):
        return f"{self.user.email} - {self.group.name} ({self.status})"

class Transaction(AbstractBaseModel):
    """
    Model representing a financial contribution made by a member to a group.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('successful', 'Successful'),
        ('failed', 'Failed'),
    )

    membership = models.ForeignKey(
        GroupMembership, 
        on_delete=models.PROTECT,
        null=True, 
        blank=True, 
        related_name='transactions'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='wallet_transactions'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Amount paid in this transaction")
    cycle_number = models.IntegerField(help_text="The Ajo cycle this payment covers", null=True, blank=True)
    reference = models.CharField(max_length=100, unique=True, help_text="Payment gateway reference ID")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        if self.membership and self.membership.user:
            user_identifier = self.membership.user.email
        elif hasattr(self, 'user') and self.user:
            user_identifier = self.user.email
        else:
            user_identifier = "Direct Wallet Deposit"

        return f"{user_identifier} - ₦{self.amount} ({self.status})"
    

class PayoutSchedule(AbstractBaseModel):
    """
    Model representing the scheduled rotation for AJO group payouts.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),         # Waiting for their turn
        ('processing', 'Processing'),   # Payout is currently being sent
        ('paid', 'Paid'),               # Money successfully dropped in their wallet
    )

    group = models.ForeignKey(
        ContributionGroup, 
        on_delete=models.CASCADE, 
        related_name='payout_schedules'
    )
    member = models.ForeignKey(
        GroupMembership, 
        on_delete=models.CASCADE, 
        related_name='payouts_due'
    )
    next_deadline = models.DateTimeField(
        default=timezone.now,
        help_text="The exact date and time the auto-deduction runs"
    )
    cycle_number = models.PositiveIntegerField(help_text="The position/turn in the AJO cycle")
    expected_payout_date = models.DateField()
    payout_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    investment_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Percentage of payout automatically routed to investment vault (0 to 100%)."
    )
    invested_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Actual amount moved into the investment vault upon payout."
    )

    class Meta:
        unique_together = ('group', 'cycle_number')
        ordering = ['cycle_number']

    def __str__(self):
        return f"{self.group.name} - Turn {self.cycle_number}: {self.member.user.email} ({self.status})"
    

class InvestmentVault(AbstractBaseModel):
    """
    Holds funds that members chose to reinvest from their Ajo payouts
    instead of withdrawing to their liquid wallet.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='investments'
    )
    payout_source = models.ForeignKey(
        PayoutSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='investments'
    )
    principal_amount = models.DecimalField(max_digits=12, decimal_places=2)
    interest_rate_annual = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('12.00')) # e.g. 12% APY
    lock_duration_days = models.PositiveIntegerField(default=90) # Standard 90-day lock
    is_matured = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.email} - Invested ₦{self.principal_amount} ({self.interest_rate_annual}% APY)"