import random
import os
import requests
from django.conf import settings
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
    """Dispatches a genuine 6-digit OTP using Brevo's HTTPS API."""
    clean_email = email.strip().lower()
    otp_code = f"{random.randint(100000, 999999)}"

    # Store or update token in database
    TemporaryRegistrationVerification.objects.update_or_create(
        contact=clean_email,
        defaults={
            "otp": otp_code,
            "is_verified": False,
            "created_at": timezone.now(),
        }
    )

    # Directly check os.environ first, then settings fallbacks
    brevo_api_key = (
        os.environ.get('BREVO_API_KEY') or 
        os.environ.get('BREVO_SMTP_KEY') or 
        getattr(settings, 'BREVO_API_KEY', None) or 
        getattr(settings, 'BREVO_SMTP_KEY', None)
    )
    
    if not brevo_api_key:
        raise Exception("Brevo API key is not configured in environment variables.")

    url = "https://api.brevo.com/v3/smtp/email"
    
    # Safely clean and parse the sender email to avoid unverified domain rejections
    raw_sender = os.environ.get('DEFAULT_FROM_EMAIL', 'bbdd5b001@smtp-brevo.com')
    clean_sender = raw_sender.replace('<', '').replace('>', '').strip()
    if '@roscavault.com' in clean_sender:
        clean_sender = 'bbdd5b001@smtp-brevo.com'  # Fallback to verified smtp user if domain isn't authenticated

    payload = {
        "sender": {
            "name": "ROSCAVault",
            "email": clean_sender
        },
        "to": [{"email": clean_email}],
        "subject": f"{otp_code} is your ROSCAVault confirmation code",
        "htmlContent": f"""
            <div style="font-family: Arial, sans-serif; padding: 20px;">
                <h2>Welcome to ROSCAVault!</h2>
                <p>Your email verification code is:</p>
                <h1 style="color: #15803D; letter-spacing: 2px;">{otp_code}</h1>
                <p>This code expires in 10 minutes. If you did not request this, please ignore.</p>
            </div>
        """
    }
    headers = {
        "accept": "application/json",
        "api-key": brevo_api_key,
        "content-type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers, timeout=10)
    
    if response.status_code not in [200, 201]:
        raise Exception(f"Brevo API error: {response.text}")

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