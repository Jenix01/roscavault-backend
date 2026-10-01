import hashlib
import uuid
import random
from datetime import date
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from apps.core.models import AbstractBaseModel
from django.utils import timezone
from datetime import timedelta


class UserKYCProfile(models.Model):
    TIER_CHOICES = (
        (0, 'Tier 0 - Unverified'),
        (1, 'Tier 1 - Basic (BVN or vNIN)'),
        (2, 'Tier 2 - Medium (Dual BVN+vNIN & Residential Address)'),
        (3, 'Tier 3 - Full KYC (Proof of Address & Enhanced Due Diligence)'),
    )

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='kyc_profile')
    tier = models.PositiveSmallIntegerField(choices=TIER_CHOICES, default=0)
    
    # NDPA 2023 Data Minimization: Store cryptographic hash for uniqueness checks + masked text for display
    bvn_hash = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    bvn_masked = models.CharField(max_length=16, blank=True, null=True)
    
    vnin_hash = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    vnin_masked = models.CharField(max_length=16, blank=True, null=True)
    
    # Dual-Link Tracking (Required for Tier 2/3)
    has_bvn_verified = models.BooleanField(default=False)
    has_nin_verified = models.BooleanField(default=False)
    
    residential_address = models.TextField(blank=True, null=True)
    address_verified = models.BooleanField(default=False)
    
    # NDPA Consent Record
    ndpa_consent_granted = models.BooleanField(default=False)
    ndpa_consent_timestamp = models.DateTimeField(null=True, blank=True)
    
    updated_at = models.DateTimeField(auto_now=True)
    
    selfie_image = models.ImageField(upload_to='kyc/selfies/', null=True, blank=True)
    utility_bill = models.FileField(upload_to='kyc/utility_bills/', null=True, blank=True)
    utility_bill_type = models.CharField(max_length=30, null=True, blank=True) # 'electricity_bill' or 'bank_statement'
    face_match_score = models.FloatField(null=True, blank=True)
    face_verified = models.BooleanField(default=False)

    @staticmethod
    def hash_identifier(value: str) -> str:
        """One-way cryptographic hash with backend salt to prevent rainbow-table exposure."""
        salt = getattr(settings, 'SECRET_KEY', 'roscavault_salt')
        return hashlib.sha256(f"{salt}_{value.strip()}".encode('utf-8')).hexdigest()

    @staticmethod
    def mask_identifier(value: str) -> str:
        clean = value.strip()
        if len(clean) < 5:
            return "****"
        return f"{clean[:3]}******{clean[-2:]}"

    def __str__(self):
        return f"{self.user} - Tier {self.tier}"


class KYCAuditLog(models.Model):
    """Mandatory 5-Year Regulatory Audit Trail under MLPPA 2022 Section 7."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='kyc_audit_logs')
    id_type = models.CharField(max_length=10) # 'bvn' or 'vnin'
    provider_name = models.CharField(max_length=50) # 'Prembly' | 'QoreID' | 'IdentityPass'
    provider_reference = models.CharField(max_length=100)
    name_similarity_score = models.FloatField()
    matched_legal_name = models.CharField(max_length=255)
    registered_name = models.CharField(max_length=255)
    match_passed = models.BooleanField(default=False)
    raw_response_digest = models.CharField(max_length=64) # SHA256 of response
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']

class OTPVerification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identifier = models.CharField(max_length=150, db_index=True)  # Phone number or email
    otp_code = models.CharField(max_length=6)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def is_valid(self):
        return not self.is_verified and timezone.now() <= self.expires_at

    @classmethod
    def generate_otp(cls, identifier):
        # 6-digit OTP code (fixed default '123456' for development/local testing, or random in production)
        code = f"{random.randint(100000, 999999)}"
        # For development safety on localhost, you can print this directly to console
        expiry = timezone.now() + timedelta(minutes=10)
        otp_instance = cls.objects.create(
            identifier=identifier.strip(),
            otp_code=code,
            expires_at=expiry
        )
        print(f"\n==========================================")
        print(f" [OPAY ONBOARDING OTP] Code for {identifier}: {code}")
        print(f"==========================================\n")
        return otp_instance

class User(AbstractUser):
    """
    Custom User model for the AJO platform.
    Inherits UUID and timestamps from AbstractBaseModel.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)

    trust_score = models.IntegerField(
        default=100, 
        help_text="Dynamic user trust score (0-100)"
    )
    
    KYC_STATUS_CHOICES = (
        ('unverified', 'Unverified'),
        ('pending', 'Pending Verification'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected'),
    )

    kyc_status = models.CharField(max_length=20, choices=KYC_STATUS_CHOICES, default='unverified')
    kyc_tier = models.PositiveSmallIntegerField(default=0)  # 0: None, 1: BVN/NIN, 2: Full Document
    bvn_nin_hash = models.CharField(max_length=128, blank=True, null=True)
    kyc_verified_at = models.DateTimeField(null=True, blank=True)

    # Email for login instead of username
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    @property
    def max_contribution_limit(self):
        if self.kyc_tier >= 2:
            return 10000000.00
        elif self.kyc_tier == 1:
            return 50000.00
        return 10000.00

    def __str__(self):
        return self.email

    class Meta:
        app_label = 'users'


class Wallet(AbstractBaseModel):
    """
    Model representing a user's digital wallet for receiving AJO payouts and holding funds.
    """
    user = models.OneToOneField(
        User, 
        on_delete=models.CASCADE, 
        related_name='wallet'
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def __str__(self):
        return f"{self.user.email} - ₦{self.balance}"


@receiver(post_save, sender=User)
def create_user_wallet(sender, instance, created, **kwargs):
    if created:
        Wallet.objects.create(user=instance)


class InvestmentLock(AbstractBaseModel):
    """
    Model managing the PRD's Wealth Engine micro-yield asset locks.
    """
    STATUS_CHOICES = (
        ('active', 'Active - Yielding Interest'),
        ('matured', 'Matured - Successfully Completed'),
        ('liquidated_early', 'Liquidated Early - Penalized'),
    )

    wallet = models.ForeignKey(
        'users.Wallet', 
        on_delete=models.CASCADE, 
        related_name='investment_locks'
    )
    principal_amount = models.DecimalField(max_digits=12, decimal_places=2)
    annual_interest_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0.1500)
    locked_date = models.DateField(auto_now_add=True)
    maturity_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    def calculate_current_yield(self):
        if self.status != 'active':
            return float(self.principal_amount)
            
        days_locked = (date.today() - self.locked_date).days
        t = days_locked / 365.0 
        return float(self.principal_amount) * (1 + float(self.annual_interest_rate) * t)

    def __str__(self):
        return f"{self.wallet.user.email} - Locked: ₦{self.principal_amount} ({self.status})"


class WithdrawalRequest(AbstractBaseModel):
    """
    Handles user withdrawals to bank or split to Wealth Engine.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    )

    wallet = models.ForeignKey(
        'users.Wallet', 
        on_delete=models.CASCADE, 
        related_name='withdrawals'
    )
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    investment_percentage = models.PositiveIntegerField(
        default=0, 
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage of total amount to route to the Wealth Engine."
    )
    destination_bank = models.CharField(max_length=100, blank=True, null=True)
    destination_account = models.CharField(max_length=20, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    @property
    def investment_amount(self):
        return (self.total_amount * self.investment_percentage) / 100

    @property
    def cash_withdrawal_amount(self):
        return self.total_amount - self.investment_amount

    def __str__(self):
        return f"{self.wallet.user.email} - Total: ₦{self.total_amount} (Invested: {self.investment_percentage}%)"
    

class Notification(models.Model):
    TYPES = (
        ('join_request', 'Join Request'),
        ('applicant_status', 'Applicant Status'),
        ('payment_reminder', 'Payment Reminder'),
        ('payout', 'Payout Credited'),
        ('general', 'General Announcement'),
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    notification_type = models.CharField(max_length=30, choices=TYPES, default='general')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']   
        
class TemporaryRegistrationVerification(models.Model):
    contact = models.CharField(max_length=255, unique=True, db_index=True)
    otp = models.CharField(max_length=6)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Temporary Registration Verification'
        verbose_name_plural = 'Temporary Registration Verifications'

    def __str__(self):
        return f"{self.contact} - {self.otp} (Verified: {self.is_verified})"

    def is_expired(self):
        # Verification code remains valid for 10 minutes
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @classmethod
    def generate_otp(cls, contact):
        code = f"{random.randint(100000, 999999)}"
        obj, _ = cls.objects.update_or_create(
            contact=contact.strip().lower(),
            defaults={
                "otp": code,
                "is_verified": False,
                "created_at": timezone.now(),
            }
        )
        return code