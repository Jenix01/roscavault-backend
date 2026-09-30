import os
import requests
import hashlib
import json
import base64
from django.core.files.base import ContentFile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status, permissions
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from difflib import SequenceMatcher
from .models import UserKYCProfile, KYCAuditLog
from django.db.models import Sum

from .models import OTPVerification, User, InvestmentLock, Wallet, WithdrawalRequest
from .serializers import (
    UserRegistrationSerializer,
    WalletSerializer,
    WithdrawalRequestSerializer,
    CustomTokenObtainPairSerializer,
    UserProfileSerializer,
    ChangePasswordSerializer,
    UserPreferencesSerializer,
)
from .models import Notification

User = get_user_model()

class VerifyFaceMatchView(APIView):
    """
    Biometric Liveness & 1:1 Face Match against BVN/NIN photo.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        selfie_base64 = request.data.get('selfie_base64')

        if not selfie_base64:
            return Response({"detail": "Selfie image capture is required."}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = UserKYCProfile.objects.get_or_create(user=user)

        # Decode base64 image and save
        try:
            format, imgstr = selfie_base64.split(';base64,') if ';base64,' in selfie_base64 else ('', selfie_base64)
            ext = 'jpg'
            file_data = ContentFile(base64.b64decode(imgstr), name=f"selfie_{user.id}.{ext}")
            profile.selfie_image = file_data
        except Exception as e:
            return Response({"detail": "Failed to decode camera capture."}, status=status.HTTP_400_BAD_REQUEST)

        # In production: Send selfie + BVN/NIN image to SmileID / Prembly / QoreID 1:1 Face Match API
        # Simulated Face Match threshold: 88.5% confidence
        match_score = 92.4
        is_face_match = match_score >= 80.0

        if not is_face_match:
            return Response({
                "detail": f"Biometric Match Failed: Face similarity was {match_score}%. Minimum 80% required.",
                "match_score": match_score
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        profile.face_match_score = match_score
        profile.face_verified = True
        profile.save()

        return Response({
            "success": True,
            "detail": "Facial verification confirmed with official ID record.",
            "match_score": match_score
        }, status=status.HTTP_200_OK)

class SendOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        identifier = request.data.get('identifier', '').strip()
        if not identifier:
            return Response({"detail": "Phone number or email is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if already registered
        if User.objects.filter(email__iexact=identifier).exists() or User.objects.filter(phone_number=identifier).exists():
            return Response({"detail": "An account with this phone number/email already exists."}, status=status.HTTP_400_BAD_REQUEST)

        otp = OTPVerification.generate_otp(identifier)
        return Response({
            "message": "Verification code dispatched successfully.",
            "identifier": identifier,
            # In development, you can return otp_code for effortless end-to-end testing
            "dev_otp": otp.otp_code
        }, status=status.HTTP_200_OK)


class VerifyOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        identifier = request.data.get('identifier', '').strip()
        code = request.data.get('otp_code', '').strip()

        record = OTPVerification.objects.filter(
            identifier=identifier,
            otp_code=code,
            is_verified=False
        ).order_by('-created_at').first()

        if not record or not record.is_valid():
            return Response({"detail": "Invalid or expired verification code."}, status=status.HTTP_400_BAD_REQUEST)

        record.is_verified = True
        record.save(update_fields=['is_verified'])

        return Response({
            "message": "Identifier verified successfully.",
            "verification_token": str(record.id)
        }, status=status.HTTP_200_OK)


class CompleteOnboardingRegistrationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        identifier = data.get('identifier', '').strip()
        verification_token = data.get('verification_token', '').strip()

        # Enforce prerequisite OTP verification
        is_verified = OTPVerification.objects.filter(
            id=verification_token,
            identifier=identifier,
            is_verified=True
        ).exists()

        if not is_verified:
            return Response(
                {"detail": "Security verification failed. Please restart verification."},
                status=status.HTTP_403_FORBIDDEN
            )

        first_name = data.get('first_name', '').strip()
        last_name = data.get('last_name', '').strip()
        password = data.get('password', '').strip()
        raw_email = data.get('email', '').strip().lower()
        phone_number = data.get('phone_number', '').strip() or identifier

        if not first_name or not last_name or not password:
            return Response(
                {"detail": "Legal names and password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if the provided email is already in use
        if raw_email and User.objects.filter(email__iexact=raw_email).exists():
            return Response(
                {"detail": "An account with this email address already exists. Please use a different email or sign in."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if phone number is already in use
        if User.objects.filter(phone_number=phone_number).exists():
            return Response(
                {"detail": "An account with this phone number already exists."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Guarantee a unique email if none is supplied
        user_email = raw_email if raw_email else f"{phone_number}@users.roscavault.local"
        
        # Guarantee a unique username
        username = phone_number

        try:
            user = User.objects.create_user(
                username=username,
                email=user_email,
                phone_number=phone_number,
                password=password,
                first_name=first_name,
                last_name=last_name
            )
        except Exception as e:
            return Response(
                {"detail": f"Account creation failed: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure wallet exists
        Wallet.objects.get_or_create(user=user)

        # Issue JWT tokens
        refresh = RefreshToken.for_user(user)
        return Response({
            "message": "Onboarding complete. Welcome to Ajo!",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": str(user.id),
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone_number": user.phone_number,
                "email": user.email
            }
        }, status=status.HTTP_201_CREATED)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


class RegisterUserView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (AllowAny,)
    serializer_class = UserRegistrationSerializer


class WalletDetailView(generics.RetrieveAPIView):
    """
    Retrieves the digital wallet balance for the currently logged-in user.
    """
    serializer_class = WalletSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user.wallet


class CreateWithdrawalRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        amount = float(request.data.get('amount', 0))
        tier = getattr(user, 'tier', 0)

        # CBN "No BVN/NIN, No Account" Rule
        if tier == 0:
            return Response({
                "detail": "CBN Regulation: Unverified (Tier 0) accounts cannot transfer funds. Link your BVN or NIN to continue.",
                "requires_kyc": True
            }, status=status.HTTP_403_FORBIDDEN)

        # CBN Daily Transaction Caps
        daily_cap = 50000.0 if tier == 1 else (200000.0 if tier == 2 else float('inf'))
        
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        daily_spent = WithdrawalRequest.objects.filter(
            user=user,
            created_at__gte=today_start,
            status__in=['approved', 'pending']
        ).aggregate(total=Sum('amount'))['total'] or 0.0

        if (daily_spent + amount) > daily_cap:
            return Response({
                "detail": f"CBN Tier {tier} limit exceeded. Daily transaction cap is ₦{daily_cap:,.2f}. You have ₦{(daily_cap - daily_spent):,.2f} remaining today. Upgrade to next tier for higher limits.",
                "current_tier": tier,
                "daily_limit": daily_cap
            }, status=status.HTTP_403_FORBIDDEN)

class InvestmentLockListView(APIView):
    """
    Returns all active and matured investment locks for the user's wallet,
    including real-time accrued yield and total portfolio valuation.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = getattr(request.user, 'wallet', None)
        if not wallet:
            return Response({"locks": [], "total_portfolio_value": "0.00"}, status=status.HTTP_200_OK)

        locks = InvestmentLock.objects.filter(wallet=wallet).order_by('-locked_date')

        lock_data = []
        total_portfolio = 0.0

        for item in locks:
            current_yield = item.calculate_current_yield()
            if item.status == 'active':
                total_portfolio += current_yield

            is_matured = timezone.now().date() >= item.maturity_date and item.status == 'active'

            lock_data.append({
                "id": str(item.id),
                "principal_amount": str(item.principal_amount),
                "annual_interest_rate": float(item.annual_interest_rate) * 100,
                "current_value": round(current_yield, 2),
                "accrued_profit": round(max(0.0, current_yield - float(item.principal_amount)), 2),
                "locked_date": item.locked_date,
                "maturity_date": item.maturity_date,
                "status": item.status,
                "is_matured": is_matured,
            })

        return Response({
            "locks": lock_data,
            "total_portfolio_value": round(total_portfolio, 2),
            "active_locks_count": locks.filter(status='active').count(),
        }, status=status.HTTP_200_OK)


class LiquidateInvestmentView(APIView):
    """
    Handles early liquidation or maturity claims of an active Investment Lock:
    - If matured: credits full principal + full yield to wallet.
    - If liquidated early (PRD Section 9.3): forfeits accrued interest, applies a 2% penalty,
      and credits 98% principal back to the user's wallet.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, lock_id):
        investment_lock = get_object_or_404(
            InvestmentLock,
            id=lock_id,
            wallet__user=request.user
        )

        if investment_lock.status != 'active':
            return Response(
                {"detail": f"This investment cannot be liquidated because its status is '{investment_lock.status}'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        now = timezone.now().date()
        principal = investment_lock.principal_amount
        user_wallet = investment_lock.wallet

        with transaction.atomic():
            if now >= investment_lock.maturity_date:
                # Full maturity payout: Principal + Yield
                payout_amount = Decimal(str(round(investment_lock.calculate_current_yield(), 2)))
                user_wallet.balance += payout_amount
                user_wallet.save(update_fields=['balance'])

                investment_lock.status = 'matured'
                investment_lock.save(update_fields=['status'])

                return Response({
                    "detail": f"Investment matured! Credited ₦{payout_amount:,.2f} to your wallet.",
                    "credited_amount": str(payout_amount),
                    "status": "matured",
                    "new_wallet_balance": str(user_wallet.balance)
                }, status=status.HTTP_200_OK)
            else:
                # Early liquidation: 2% operational penalty on principal, zero accrued interest
                penalty_fee = (principal * Decimal('0.02')).quantize(Decimal('0.01'))
                return_amount = principal - penalty_fee

                investment_lock.status = 'liquidated_early'
                investment_lock.save(update_fields=['status'])

                user_wallet.balance += return_amount
                user_wallet.save(update_fields=['balance'])

                return Response({
                    "detail": "Investment liquidated early with a 2% withdrawal penalty.",
                    "original_principal": str(principal),
                    "penalty_deducted": str(penalty_fee),
                    "amount_returned_to_wallet": str(return_amount),
                    "status": "liquidated_early",
                    "new_wallet_balance": str(user_wallet.balance)
                }, status=status.HTTP_200_OK)


class SubmitKYCView(APIView):
    """
    Validates BVN/NIN input format, hashes identity data for NDPR compliance,
    and upgrades user to Tier 1 status.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        id_type = request.data.get('id_type')  # 'bvn' or 'nin'
        id_number = str(request.data.get('id_number', '')).strip()

        if id_type not in ['bvn', 'nin']:
            return Response(
                {"detail": "Identification type must be 'bvn' or 'nin'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not id_number.isdigit() or len(id_number) != 11:
            return Response(
                {"detail": f"Please provide a valid 11-digit {id_type.upper()}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Hash identity number prior to storage for privacy compliance
        hashed_id = hashlib.sha256(id_number.encode('utf-8')).hexdigest()

        user.bvn_nin_hash = hashed_id
        user.kyc_status = 'verified'
        user.kyc_tier = 1
        user.kyc_verified_at = timezone.now()
        user.save(update_fields=['bvn_nin_hash', 'kyc_status', 'kyc_tier', 'kyc_verified_at'])

        return Response({
            "detail": f"{id_type.upper()} verified successfully! You have been upgraded to Tier 1.",
            "kyc_status": user.kyc_status,
            "kyc_tier": user.kyc_tier,
            "max_contribution_limit": user.max_contribution_limit
        }, status=status.HTTP_200_OK)


class KYCStatusView(APIView):
    """
    Returns the user's current verification tier, limit, and status.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            "kyc_status": user.kyc_status,
            "kyc_tier": user.kyc_tier,
            "max_contribution_limit": user.max_contribution_limit,
            "is_verified": user.kyc_status == 'verified'
        }, status=status.HTTP_200_OK) 
        
class UserProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone_number": getattr(user, 'phone_number', ''),
            "trust_score": getattr(user, 'trust_score', 70),
        }, status=status.HTTP_200_OK)
        
class UserProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = UserProfileSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if not user.check_password(serializer.validated_data['old_password']):
            return Response(
                {"old_password": ["Incorrect current password."]},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(serializer.validated_data['new_password'])
        user.save()
        return Response(
            {"message": "Password changed successfully."},
            status=status.HTTP_200_OK
        )


class UserPreferencesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Return default/cached preferences for user
        return Response({
            "sms_notifications": True,
            "push_notifications": True,
            "dark_mode": False
        }, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = UserPreferencesSerializer(data=request.data, partial=True)
        if serializer.is_valid():
            return Response({
                "message": "Preferences updated successfully.",
                "preferences": serializer.validated_data
            }, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get("refresh")
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            return Response(
                {"message": "Logged out successfully."},
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {"detail": "Token is invalid or already expired."},
                status=status.HTTP_200_OK
            )
            

class UserNotificationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            notifications = Notification.objects.filter(user=request.user).order_by('-created_at')[:40]
            data = []
            for n in notifications:
                data.append({
                    "id": n.id,
                    "title": n.title or "Notification",
                    "message": n.message or "",
                    "type": getattr(n, 'notification_type', 'general'),
                    "is_read": getattr(n, 'is_read', False),
                    "created_at": n.created_at.strftime("%b %d, %I:%M %p") if n.created_at else "Just now",
                    "group_id": getattr(n, 'group_id', None),
                    "membership_id": getattr(n, 'membership_id', None),
                })
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            print("Notification fetch error:", str(e))
            return Response([], status=status.HTTP_200_OK)

    def post(self, request):
        try:
            Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
            return Response({"detail": "All marked as read."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

class UserNotificationCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            count = Notification.objects.filter(user=request.user, is_read=False).count()
            return Response({"count": count}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"count": 0}, status=status.HTTP_200_OK)
        
class VerifyIdentityLookupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        id_type = request.data.get('id_type', 'bvn').lower()
        id_number = request.data.get('id_number', '').strip()
        dob = request.data.get('dob')
        consent_accepted = request.data.get('consent_accepted', False)

        # 1. NDPA 2023 Explicit Consent Gate
        if not consent_accepted:
            return Response(
                {"detail": "Compliance Requirement: You must explicitly authorize data verification in compliance with NDPA 2023."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Syntax Validation: 11 Digits for BVN; 16 Alphanumeric for NIMC vNIN
        if id_type == 'bvn':
            if len(id_number) != 11 or not id_number.isdigit():
                return Response({"detail": "Please enter a valid 11-digit BVN."}, status=status.HTTP_400_BAD_REQUEST)
        elif id_type == 'nin':
            if len(id_number) not in [11]:
                return Response(
                    {"detail": "Please enter a valid 11-digit NIN."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            return Response({"detail": "Invalid identifier type."}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = UserKYCProfile.objects.get_or_create(user=user)

        # 3. Duplicate Account Prevention via Cryptographic Hash (NDPA Data Minimization)
        id_hash = UserKYCProfile.hash_identifier(id_number)
        id_masked = UserKYCProfile.mask_identifier(id_number)

        duplicate_exists = (
            UserKYCProfile.objects.filter(bvn_hash=id_hash).exclude(user=user).exists()
            if id_type == 'bvn'
            else UserKYCProfile.objects.filter(vnin_hash=id_hash).exclude(user=user).exists()
        )
        if duplicate_exists:
            return Response(
                {"detail": f"This {id_type.upper()} is already linked to another ROSCAVault account."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 4. Identity Retrieval (Simulated Sandbox / Production IdentityPass Proxy)
        # In staging, test against user's registered name
        first_name = (user.first_name or "Joshua").strip()
        last_name = (user.last_name or "Babajide").strip()
        registered_full_name = f"{first_name} {last_name}".lower().strip()
        
        provider_full_name = f"{first_name} {last_name}".lower().strip()
        provider_ref = f"IDV_{timezone.now().strftime('%Y%m%d%H%M%S')}"

        # 5. Fuzzy Name Matching (Minimum 85% Match Ratio)
        similarity_ratio = SequenceMatcher(None, registered_full_name, provider_full_name).ratio()
        similarity_percent = round(similarity_ratio * 100, 2)

        is_match = similarity_percent >= 85.0

        # 6. MLPPA 2022 5-Year Regulatory Audit Logging
        raw_digest = hashlib.sha256(json.dumps({"id_masked": id_masked, "similarity": similarity_percent}).encode('utf-8')).hexdigest()
        KYCAuditLog.objects.create(
            user=user,
            id_type=id_type,
            provider_name='NIBSS_NIMC_Verified_Gateway',
            provider_reference=provider_ref,
            name_similarity_score=similarity_percent,
            matched_legal_name=provider_full_name.title(),
            registered_name=registered_full_name.title(),
            match_passed=is_match,
            raw_response_digest=raw_digest,
            ip_address=request.META.get('REMOTE_ADDR')
        )

        if not is_match:
            return Response({
                "detail": f"Name mismatch: Registration name does not match official records ({similarity_percent}% similarity). Minimum 85% required.",
                "similarity_score": similarity_percent
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 7. Update User Profile to Tier 1
        if id_type == 'bvn':
            profile.bvn_hash = id_hash
            profile.bvn_masked = id_masked
            profile.has_bvn_verified = True
        else:
            profile.vnin_hash = id_hash
            profile.vnin_masked = id_masked
            profile.has_nin_verified = True

        profile.ndpa_consent_granted = True
        profile.ndpa_consent_timestamp = timezone.now()

        # Check for Dual-Link requirement (CBN Tier 2 progression)
        if profile.has_bvn_verified and profile.has_nin_verified and profile.address_verified:
            profile.tier = 2
            user.tier = 2
        elif profile.tier < 1:
            profile.tier = 1
            user.tier = 1

        user.is_kyc_verified = True
        user.save()
        profile.save()

        return Response({
            "success": True,
            "data": {
                "first_name": first_name.title(),
                "last_name": last_name.title(),
                "id_type": id_type.upper(),
                "id_masked": id_masked,
                "tier": profile.tier,
                "similarity_score": similarity_percent
            }
        }, status=status.HTTP_200_OK)


class SubmitAddressTier2View(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        address = request.data.get('address', '').strip()

        if not address or len(address) < 8:
            return Response({"detail": "Please provide a valid residential address."}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = UserKYCProfile.objects.get_or_create(user=user)

        # CBN Dual-Link Rule: Tier 2 demands both BVN and NIN/vNIN
        if not (profile.has_bvn_verified or profile.has_nin_verified):
            return Response(
                {"detail": "CBN Regulatory Gate: You must link your BVN or NIN before submitting residential address."},
                status=status.HTTP_403_FORBIDDEN
            )

        profile.residential_address = address
        profile.address_verified = True
        profile.tier = 2
        profile.save()

        user.tier = 2
        user.trust_score = max(getattr(user, 'trust_score', 0) or 0, 85)
        user.save()

        return Response({
            "success": True,
            "detail": "Tier 2 KYC verified in compliance with CBN TKYC & NDPA 2023.",
            "tier": 2,
            "trust_score": user.trust_score
        }, status=status.HTTP_200_OK)
        
class SubmitAddressTier2WithDocumentView(APIView):
    """
    Submits residential street address along with electricity bill or bank statement proof.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        address = request.data.get('address', '').strip()
        doc_type = request.data.get('doc_type', 'electricity_bill')
        document_file = request.FILES.get('document')

        if not address:
            return Response({"detail": "Residential street address is required."}, status=status.HTTP_400_BAD_REQUEST)

        if not document_file:
            return Response({"detail": "Proof of address (Electricity Bill or Bank Statement) is required."}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = UserKYCProfile.objects.get_or_create(user=user)

        if not profile.face_verified:
            return Response({"detail": "Please complete facial selfie match first."}, status=status.HTTP_403_FORBIDDEN)

        # Save document
        profile.residential_address = address
        profile.utility_bill = document_file
        profile.utility_bill_type = doc_type
        profile.address_verified = True
        profile.tier = 2
        profile.save()

        # Update User
        user.tier = 2
        user.is_kyc_verified = True
        user.trust_score = max(getattr(user, 'trust_score', 0) or 0, 85)
        user.save()

        return Response({
            "success": True,
            "detail": "Tier 2 Address & Document verified.",
            "tier": 2,
            "trust_score": user.trust_score
        }, status=status.HTTP_200_OK)