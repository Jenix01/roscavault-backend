from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.password_validation import validate_password
from .models import User, Wallet, WithdrawalRequest, InvestmentLock, TemporaryRegistrationVerification

User = get_user_model()


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
        self.fields['email'] = serializers.CharField(required=False)
        self.fields['phone_number'] = serializers.CharField(required=False)
        self.fields['username'] = serializers.CharField(required=False)

    def validate(self, attrs):
        
        identifier = (
            attrs.get('email')
            or attrs.get('phone_number')
            or attrs.get('username')
        )
        password = attrs.get('password')

        if not identifier:
            raise serializers.ValidationError({
                "detail": "Please enter your email address or phone number."
            })

        cleaned_id = identifier.strip()

        # Support Nigerian phone number normalization: e.g. 080... vs +23480...
        phone_variants = [cleaned_id]
        if cleaned_id.startswith('0') and len(cleaned_id) == 11:
            phone_variants.append('+234' + cleaned_id[1:])
            phone_variants.append('234' + cleaned_id[1:])
        elif cleaned_id.startswith('+234') and len(cleaned_id) == 14:
            phone_variants.append('0' + cleaned_id[4:])
        elif cleaned_id.startswith('234') and len(cleaned_id) == 13:
            phone_variants.append('0' + cleaned_id[3:])

        # Query user by Email or Phone Number (case-insensitive for email)
        user = User.objects.filter(
            Q(email__iexact=cleaned_id) |
            Q(phone_number__in=phone_variants) |
            Q(username__iexact=cleaned_id)
        ).first()

        if user and user.check_password(password):
            # TokenObtainPairSerializer internally checks attrs['username']
            attrs['username'] = user.username
            return super().validate(attrs)

        raise serializers.ValidationError({
            "detail": "Invalid credentials. Please verify your email/phone and password."
        })


class UserProfileSerializer(serializers.ModelSerializer):
    trust_score = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'phone_number',
            'trust_score',
            'kyc_status',
            'kyc_tier',
            'date_joined',
        )
        read_only_fields = (
            'id',
            'username',
            'email',
            'trust_score',
            'kyc_status',
            'kyc_tier',
            'date_joined',
        )

    def get_trust_score(self, obj):
        # Return a score based on KYC status
        if getattr(obj, 'kyc_status', '') == 'verified':
            return 85
        return 50
        
class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True)

    def validate_new_password(self, value):
        validate_password(value)
        return value


class UserPreferencesSerializer(serializers.Serializer):
    sms_notifications = serializers.BooleanField(default=True)
    push_notifications = serializers.BooleanField(default=True)
    dark_mode = serializers.BooleanField(default=False)


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )

    class Meta:
        model = User
        fields = ('email', 'password', 'first_name', 'last_name')

    def create(self, validated_data):
        email = validated_data['email']
        user = User.objects.create_user(
            username=email,
            email=email,
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', '')
        )
        return user


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ['id', 'balance', 'updated_at']
        read_only_fields = ['balance']


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = [
            'id',
            'total_amount',
            'investment_percentage',
            'destination_bank',
            'destination_account',
            'status',
            'created_at',
        ]
        read_only_fields = ['status', 'created_at']

    def validate(self, data):
        user_wallet = self.context['request'].user.wallet
        total_amount = data.get('total_amount')
        investment_percentage = data.get('investment_percentage', 0)

        # 1. Enforce Wallet Sufficiency Rule
        if user_wallet.balance < total_amount:
            raise serializers.ValidationError("Insufficient wallet balance for this transaction.")

        # 2. Require bank details if any portion is being withdrawn as cash
        if investment_percentage < 100:
            if not data.get('destination_bank') or not data.get('destination_account'):
                raise serializers.ValidationError(
                    "Destination bank name and account number are required for cash withdrawals."
                )

        return data
    
class SendCodeSerializer(serializers.Serializer):
    contact = serializers.EmailField()
    channel = serializers.CharField(default="email")

    def validate_contact(self, value):
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email address already exists.")
        return email


class VerifyCodeSerializer(serializers.Serializer):
    contact = serializers.EmailField()
    otp = serializers.CharField(max_length=6, min_length=6)

    def validate(self, attrs):
        email = attrs.get("contact").strip().lower()
        otp_code = attrs.get("otp").strip()

        try:
            verification = TemporaryRegistrationVerification.objects.get(contact=email)
        except TemporaryRegistrationVerification.DoesNotExist:
            raise serializers.ValidationError({"detail": "No verification code requested for this email."})

        if verification.is_expired():
            raise serializers.ValidationError({"detail": "Verification code has expired. Please request a new one."})

        if verification.otp != otp_code:
            raise serializers.ValidationError({"detail": "Invalid verification code."})

        # Attach verification instance for use in view
        attrs["verification"] = verification
        return attrs


class CompleteRegistrationSerializer(serializers.ModelSerializer):
    contact = serializers.EmailField(write_only=True)
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["email", "password", "contact", "first_name", "last_name"]
        extra_kwargs = {"email": {"read_only": True}}

    def validate(self, attrs):
        contact = attrs.get("contact")
        try:
            verification = TemporaryRegistrationVerification.objects.get(contact=contact)
        except TemporaryRegistrationVerification.DoesNotExist:
            raise serializers.ValidationError({"detail": "Verification record not found."})

        if not verification.is_verified:
            raise serializers.ValidationError({"detail": "Email address has not been verified yet."})

        return attrs

    def create(self, validated_data):
        contact = validated_data.pop("contact")
        password = validated_data.pop("password")
        
        # Create user account
        user = User.objects.create_user(
            email=contact,
            username=contact,
            password=password,
            **validated_data
        )

        # Cleanup verification record after successful signup
        TemporaryRegistrationVerification.objects.filter(contact=contact).delete()
        return user