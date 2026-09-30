import random
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from .models import TemporaryRegistrationVerification

# Firebase Auth Test Numbers & Predetermined Codes
FIREBASE_TEST_PHONE_WHITELIST = {
    '+2348000000001': '123456',
    '+2348000000002': '654321',
    '08000000001': '123456',
    '08000000002': '654321',
}

def send_brevo_email_otp(email: str) -> str:
    """Dispatches a genuine 6-digit OTP directly to user inbox using Brevo SMTP."""
    clean_email = email.strip().lower()
    otp_code = f"{random.randint(100000, 999999)}"

    # Record or update token in database
    TemporaryRegistrationVerification.objects.update_or_create(
        contact=clean_email,
        defaults={
            "otp": otp_code,
            "is_verified": False,
            "created_at": timezone.now(),
        }
    )

    subject = f"{otp_code} is your ROSCAVault confirmation code"
    body = (
        f"Hello,\n\n"
        f"Your ROSCAVault email verification code is: {otp_code}\n\n"
        f"This code will expire in 10 minutes. If you did not request this, please ignore."
    )

    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[clean_email],
        fail_silently=False,
    )
    return otp_code


def send_phone_otp_or_test_fixture(phone_number: str) -> str:
    """
    Validates Firebase test phone fixtures instantly, or falls back to live SMS dispatch.
    """
    clean_phone = phone_number.strip()

    # 1. Match Firebase Auth test numbers without incurring SMS cost
    if clean_phone in FIREBASE_TEST_PHONE_WHITELIST:
        otp_code = FIREBASE_TEST_PHONE_WHITELIST[clean_phone]
        TemporaryRegistrationVerification.objects.update_or_create(
            contact=clean_phone,
            defaults={
                "otp": otp_code,
                "is_verified": False,
                "created_at": timezone.now(),
            }
        )
        return otp_code

    # 2. Live production numbers (Termii / SMS gateway)
    otp_code = f"{random.randint(100000, 999999)}"
    TemporaryRegistrationVerification.objects.update_or_create(
        contact=clean_phone,
        defaults={
            "otp": otp_code,
            "is_verified": False,
            "created_at": timezone.now(),
        }
    )

    # Dispatch to live SMS provider if configured
    termii_key = getattr(settings, 'TERMII_API_KEY', None)
    if termii_key:
        import requests
        formatted_phone = clean_phone if clean_phone.startswith('+') else f"+234{clean_phone.lstrip('0')}"
        try:
            requests.post(
                "https://api.ng.termii.com/api/sms/send",
                json={
                    "to": formatted_phone,
                    "from": getattr(settings, 'TERMII_SENDER_ID', 'ROSCAVault'),
                    "sms": f"Your ROSCAVault confirmation code is {otp_code}. Valid for 10 mins.",
                    "type": "plain",
                    "channel": "generic",
                    "api_key": termii_key,
                },
                timeout=8,
            )
        except Exception as e:
            print(f"[SMS Gateway Warning]: {e}")
    else:
        print(f"[DEVELOPMENT FALLBACK] OTP for {clean_phone} is {otp_code}")

    return otp_code