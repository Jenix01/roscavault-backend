import hmac
import hashlib
import json
import requests
import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.http import HttpResponse
from django.conf import settings
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from django.db.models import Sum, Q
from django.shortcuts import redirect
from django.views import View
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import status, permissions

from .models import ContributionGroup, GroupMembership, Transaction, PayoutSchedule
from users.models import Wallet, UserKYCProfile
from .services import process_due_circle_deductions
from .serializers import (
    ContributionGroupSerializer,
    GroupMembershipSerializer,
    TransactionSerializer,
    PayoutScheduleSerializer,
    MembershipApplicantSerializer,
)

User = get_user_model()

class PaymentCallbackRedirectView(View):
    """
    Receives Paystack's browser redirect and bounces the user back to the React Native app.
    """
    def get(self, request, *args, **kwargs):
        reference = request.GET.get('reference', '') or request.GET.get('trxref', '')
        # Deep links back to your mobile app
        app_redirect_url = f"roscavault://payment-callback?reference={reference}"
        return redirect(app_redirect_url)

class ContributionGroupListCreateView(generics.ListCreateAPIView):
    serializer_class = ContributionGroupSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ContributionGroup.objects.filter(is_active=True).order_by('-created_at')

    @db_transaction.atomic
    def perform_create(self, serializer):
        group = serializer.save(creator=self.request.user)

        join_as_member = self.request.data.get('join_as_member', False)
        if str(join_as_member).lower() in ['true', '1']:
            membership = GroupMembership.objects.create(
                user=self.request.user,
                group=group,
                role='admin',
                current_cycle_number=1,
                is_active=True,
                next_deadline=timezone.now()
            )

            days_offset = 1 if group.cycle_frequency == 'daily' else (7 if group.cycle_frequency == 'weekly' else 30)
            net_pot = (group.amount * group.max_members * Decimal('0.95')).quantize(Decimal('0.01'))

            PayoutSchedule.objects.create(
                group=group,
                member=membership,
                cycle_number=1,
                expected_payout_date=(timezone.now() + timedelta(days=days_offset)).date(),
                payout_amount=net_pot,
                status='pending'
            )


class JoinContributionGroupView(generics.CreateAPIView):
    serializer_class = GroupMembershipSerializer
    permission_classes = [IsAuthenticated]

    @db_transaction.atomic
    def create(self, request, *args, **kwargs):
        group_id = self.kwargs.get('group_id')
        group = get_object_or_404(ContributionGroup, id=group_id)

        if GroupMembership.objects.filter(user=request.user, group=group).exists():
            return Response({"detail": "You are already a member of this group."}, status=status.HTTP_400_BAD_REQUEST)

        if group.memberships.count() >= group.max_members:
            return Response({"detail": "Sorry, this AJO group is already at maximum capacity."}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Find the lowest available slot number (Gap-filling logic)
        existing_positions = GroupMembership.objects.filter(
            group=group, status='approved'
        ).values_list('current_cycle_number', flat=True)

        assigned_slot = 1
        for i in range(1, group.max_members + 1):
            if i not in existing_positions:
                assigned_slot = i
                break

        # 2. Create Membership with the correct gap-filled slot
        membership = GroupMembership.objects.create(
            user=request.user,
            group=group,
            role='member',
            status='approved',
            current_cycle_number=assigned_slot,
            is_active=True,
            next_deadline=timezone.now()
        )

        # 3. Schedule Payout matching the exact assigned slot
        days_offset = 1 if group.cycle_frequency == 'daily' else (7 if group.cycle_frequency == 'weekly' else 30)
        
        # 2% Alajo fee calculation (Net Pot = 98%)
        net_pot = (Decimal(str(group.amount)) * Decimal(str(group.max_members)) * Decimal('0.98')).quantize(Decimal('0.01'))

        base_date = group.start_date if hasattr(group, 'start_date') and group.start_date else timezone.now().date()

        PayoutSchedule.objects.get_or_create(
            group=group,
            member=membership,
            cycle_number=assigned_slot,
            defaults={
                'expected_payout_date': base_date + timedelta(days=days_offset * (assigned_slot - 1)),
                'payout_amount': net_pot,
                'status': 'pending'
            }
        )

        # 4. Trigger notification for user activity feed
        try:
            from apps.users.models import Notification
            Notification.objects.create(
                user=request.user,
                title="Circle Joined 🎉",
                message=f"You have successfully joined '{group.name}' at Slot #{assigned_slot}."
            )
        except Exception:
            pass

        serializer = self.get_serializer(membership)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CreateTransactionView(generics.CreateAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        group_id = self.kwargs.get('group_id')

        try:
            membership = GroupMembership.objects.get(group__id=group_id, user=request.user)
        except GroupMembership.DoesNotExist:
            return Response(
                {"detail": "You must be a member of this group to make a contribution."},
                status=status.HTTP_403_FORBIDDEN
            )

        amount = request.data.get('amount')
        reference = request.data.get('reference')
        notes = request.data.get('notes', '')

        if not amount or not reference:
            return Response(
                {"detail": "Both 'amount' and 'reference' are required to process a payment."},
                status=status.HTTP_400_BAD_REQUEST
            )

        tx = Transaction.objects.create(
            membership=membership,
            amount=amount,
            reference=reference,
            notes=notes,
            status='pending'
        )

        serializer = self.get_serializer(tx)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WalletContributionView(APIView):
    """
    Deducts cycle dues from member's wallet, records transaction,
    and disburses pot (98% to recipient, 2% fee to Alajo) once all members pay.
    """
    permission_classes = [IsAuthenticated]

    @db_transaction.atomic
    def post(self, request, group_id):
        try:
            membership = GroupMembership.objects.select_for_update().get(group__id=group_id, user=request.user)
            group = membership.group
            wallet = Wallet.objects.select_for_update().get(user=request.user)
        except (GroupMembership.DoesNotExist, Wallet.DoesNotExist):
            return Response({"detail": "Membership or wallet not found."}, status=status.HTTP_404_NOT_FOUND)

        amount_due = group.amount
        current_cycle = membership.current_cycle_number

        if Transaction.objects.filter(membership=membership, cycle_number=current_cycle, status='successful').exists():
            return Response({"detail": f"You have already contributed for Cycle #{current_cycle}."}, status=status.HTTP_400_BAD_REQUEST)

        if wallet.balance < amount_due:
            return Response(
                {"detail": f"Insufficient funds. You need ₦{amount_due} but your balance is ₦{wallet.balance}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        wallet.balance -= amount_due
        wallet.save(update_fields=['balance'])

        ref = f"AJO-WALLET-{group.id}-C{current_cycle}-U{request.user.id}-{uuid.uuid4().hex[:6].upper()}"
        new_tx = Transaction.objects.create(
            membership=membership,
            user=request.user,
            amount=amount_due,
            cycle_number=current_cycle,
            reference=ref,
            status='successful',
            notes=f"Cycle #{current_cycle} contribution for {group.name}"
        )

        # Trigger notification for user activity feed
        try:
            from apps.users.models import Notification
            Notification.objects.create(
                user=request.user,
                title="Contribution Settled ✓",
                message=f"Your dues for Cycle #{current_cycle} in '{group.name}' were successfully paid from your wallet."
            )
        except Exception:
            pass

        paid_members_count = Transaction.objects.filter(
            membership__group=group,
            cycle_number=current_cycle,
            status='successful'
        ).count()

        pot_disbursed = False

        if paid_members_count >= group.max_members:
            schedule = PayoutSchedule.objects.select_for_update().filter(
                group=group,
                cycle_number=current_cycle,
                status='pending'
            ).first()

            if schedule:
                winner = schedule.member.user
                winner_wallet = Wallet.objects.select_for_update().get(user=winner)
                alajo_wallet = Wallet.objects.select_for_update().get(user=group.creator)

                gross_pot = group.amount * group.max_members
                alajo_fee = (gross_pot * Decimal('0.02')).quantize(Decimal('0.01'))
                net_payout = gross_pot - alajo_fee

                # Disburse 98% to Turn Winner
                winner_wallet.balance += net_payout
                winner_wallet.save(update_fields=['balance'])

                schedule.status = 'paid'
                schedule.payout_amount = net_payout
                schedule.save(update_fields=['status', 'payout_amount'])

                Transaction.objects.create(
                    membership=schedule.member,
                    user=winner,
                    amount=net_payout,
                    cycle_number=current_cycle,
                    reference=f"POT-{group.id}-C{current_cycle}-{uuid.uuid4().hex[:6].upper()}",
                    status='successful',
                    notes=f"Net Ajo Pot Payout (Cycle #{current_cycle}) - 2% Alajo fee deducted"
                )

                # Notify winner of payout
                try:
                    Notification.objects.create(
                        user=winner,
                        title="Payout Received 🎉",
                        message=f"Your payout of ₦{net_payout} for Cycle #{current_cycle} in '{group.name}' has been processed."
                    )
                except Exception:
                    pass

                # Disburse 2% to Alajo (Creator)
                alajo_wallet.balance += alajo_fee
                alajo_wallet.save(update_fields=['balance'])

                Transaction.objects.create(
                    user=group.creator,
                    amount=alajo_fee,
                    cycle_number=current_cycle,
                    reference=f"ALAJO-FEE-{group.id}-C{current_cycle}-{uuid.uuid4().hex[:6].upper()}",
                    status='successful',
                    notes=f"2% Alajo commission for {group.name} (Cycle #{current_cycle})"
                )

                pot_disbursed = True

            # Advance all memberships to the next cycle
            for m in group.memberships.filter(is_active=True):
                m.advance_to_next_cycle()

            if current_cycle >= group.max_members:
                group.is_active = False
                group.save(update_fields=['is_active'])

        return Response({
            "detail": "Contribution successful!",
            "new_wallet_balance": str(wallet.balance),
            "transaction_reference": new_tx.reference,
            "pot_disbursed": pot_disbursed,
            "current_cycle_number": membership.current_cycle_number
        }, status=status.HTTP_200_OK)


class ProcessPayoutView(APIView):
    permission_classes = [IsAuthenticated]

    @db_transaction.atomic
    def post(self, request, payout_id):
        payout = get_object_or_404(PayoutSchedule.objects.select_for_update(), id=payout_id)
        group = payout.group

        if payout.status == 'paid':
            return Response({"detail": "This payout has already been processed."}, status=status.HTTP_400_BAD_REQUEST)

        winner_wallet = Wallet.objects.select_for_update().get(user=payout.member.user)
        alajo_wallet = Wallet.objects.select_for_update().get(user=group.creator)

        gross_pot = group.amount * group.max_members
        alajo_fee = (gross_pot * Decimal('0.02')).quantize(Decimal('0.01'))
        net_payout = gross_pot - alajo_fee

        winner_wallet.balance += net_payout
        winner_wallet.save(update_fields=['balance'])

        alajo_wallet.balance += alajo_fee
        alajo_wallet.save(update_fields=['balance'])

        payout.status = 'paid'
        payout.payout_amount = net_payout
        payout.save(update_fields=['status', 'payout_amount'])

        Transaction.objects.create(
            membership=payout.member,
            user=payout.member.user,
            amount=net_payout,
            cycle_number=payout.cycle_number,
            reference=f"POT-MANUAL-{group.id}-C{payout.cycle_number}-{uuid.uuid4().hex[:6].upper()}",
            status='successful',
            notes="Ajo Pot Disbursed (95%)"
        )

        Transaction.objects.create(
            user=group.creator,
            amount=alajo_fee,
            cycle_number=payout.cycle_number,
            reference=f"ALAJO-MANUAL-{group.id}-C{payout.cycle_number}-{uuid.uuid4().hex[:6].upper()}",
            status='successful',
            notes=f"Alajo 5% fee for {group.name}"
        )

        return Response({
            "detail": f"Successfully disbursed ₦{net_payout} to {payout.member.user.email} (₦{alajo_fee} fee credited to Alajo).",
            "winner_balance": str(winner_wallet.balance),
            "alajo_balance": str(alajo_wallet.balance)
        }, status=status.HTTP_200_OK)


class MyMembershipsView(generics.ListAPIView):
    """
    Returns only truly active circles. Completed circles are excluded.
    Attaches the user's scheduled collection turn to every membership.
    """
    serializer_class = GroupMembershipSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = GroupMembership.objects.filter(
            user=user,
            is_active=True,
            group__is_active=True
        ).select_related('group')

        active_membership_ids = []
        for membership in qs:
            group = membership.group
            max_rounds = group.max_members or 5
            current_round = getattr(group, 'current_cycle_number', membership.current_cycle_number)
            if current_round <= max_rounds:
                active_membership_ids.append(membership.id)

        return qs.filter(id__in=active_membership_ids).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Augment items with collection turn metadata and dues progress
        for item in response.data:
            membership_id = item.get('id')
            payout = PayoutSchedule.objects.filter(member_id=membership_id).first()
            item['assigned_cycle_number'] = payout.cycle_number if payout else item.get('current_cycle_number', 1)
        return response


class UserMembershipsAPIView(MyMembershipsView):
    pass


class MyTransactionsView(generics.ListAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return Transaction.objects.filter(
            Q(user=user) | Q(membership__user=user)
        ).select_related('membership', 'membership__group').order_by('-created_at')


class PaymentWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        paystack_signature = request.headers.get('x-paystack-signature')
        secret = settings.PAYSTACK_SECRET_KEY.encode('utf-8')
        computed_signature = hmac.new(secret, request.body, hashlib.sha512).hexdigest()

        if not hmac.compare_digest(paystack_signature, computed_signature):
            return Response({"detail": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)

        payload = json.loads(request.body.decode('utf-8'))
        event = payload.get('event')

        if event == 'charge.success':
            data = payload.get('data', {})
            reference = data.get('reference')
            amount_paid = Decimal(str(data.get('amount', 0))) / 100

            try:
                tx = Transaction.objects.get(reference=reference)
                if tx.status != 'successful':
                    tx.status = 'successful'
                    tx.save()

                    wallet, _ = Wallet.objects.get_or_create(user=tx.user)
                    wallet.balance = (wallet.balance or Decimal('0.00')) + amount_paid
                    wallet.save()
            except Transaction.DoesNotExist:
                pass

        return Response(status=status.HTTP_200_OK)


class InitializePaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount = request.data.get('amount')
        if not amount or float(amount) <= 0:
            return Response({"detail": "Enter a valid amount."}, status=status.HTTP_400_BAD_REQUEST)

        amount_kobo = int(Decimal(str(amount)) * 100)
        user_email = request.user.email or f"{request.user.username}@ajomobile.local"

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        }
        
        # Paystack requires an https:// callback URL, not a direct custom scheme.
        # It will redirect to this backend endpoint upon success, which bounces back to the app.
        callback_url = "https://roscavault-api.onrender.com/api/contributions/payment/callback/"

        # Paystack requires a valid email format with an existing domain structure (not .local)
        user_email = (
            request.user.email
            if getattr(request.user, "email", None)
            else f"user_{request.user.phone_number or request.user.id}@roscavault.com"
        )

        payload = {
            "email": user_email,
            "amount": int(amount_kobo),
            "callback_url": callback_url,
            "metadata": {
                "app_name": "ROSCAVault",
                "user_id": str(request.user.id),
                "custom_fields": [
                    {
                        "display_name": "User Phone",
                        "variable_name": "phone",
                        "value": str(getattr(request.user, "phone_number", "")),
                    }
                ],
            },
        }

        try:
            paystack_res = requests.post(
                "https://api.paystack.co/transaction/initialize",
                json=payload,
                headers=headers,
                timeout=10
            ).json()

            if not paystack_res.get('status'):
                return Response({"detail": paystack_res.get('message', 'Payment init failed')}, status=status.HTTP_400_BAD_REQUEST)

            data = paystack_res['data']

            Transaction.objects.create(
                user=request.user,
                amount=Decimal(str(amount)),
                reference=data['reference'],
                status='pending'
            )

            return Response({
                "authorization_url": data['authorization_url'],
                "reference": data['reference'],
                "access_code": data['access_code']
            }, status=status.HTTP_200_OK)

        except requests.exceptions.RequestException as e:
            return Response({"detail": f"Paystack connection error: {str(e)}"}, status=status.HTTP_502_BAD_GATEWAY)

class PaymentCallbackView(APIView):
    permission_classes = []  # Public access for Paystack browser redirect

    def get(self, request):
        reference = request.GET.get('reference') or request.GET.get('trxref')
        
        if not reference:
            return Response({"detail": "No reference provided."}, status=400)

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        }
        
        try:
            res = requests.get(f"https://api.paystack.co/transaction/verify/{reference}", headers=headers, timeout=10).json()
            
            if res.get('status') and res['data']['status'] == 'success':
                transaction = Transaction.objects.filter(reference=reference).first()
                
                if transaction and transaction.status != 'successful':
                    # 1. Update transaction status
                    transaction.status = 'successful'
                    transaction.save()
                    
                    # 2. Credit the user's wallet
                    wallet, created = Wallet.objects.get_or_create(user=transaction.user)
                    wallet.balance += transaction.amount
                    wallet.save()
                
                # 3. Return clean success page
                return HttpResponse("""
                    <html>
                        <head>
                            <meta name="viewport" content="width=device-width, initial-scale=1.0">
                            <title>Payment Successful</title>
                        </head>
                        <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 60px; background-color: #f8fafc;">
                            <div style="max-width: 400px; margin: auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                                <h1 style="color: #15803D; margin-bottom: 10px;">Payment Successful!</h1>
                                <p style="color: #475569; font-size: 16px;">Your transaction was verified and your wallet has been funded successfully.</p>
                                <p style="color: #94a3b8; font-size: 14px; margin-top: 20px;">You can now close this tab and return to ROSCAVault.</p>
                            </div>
                        </body>
                    </html>
                """)
            else:
                return HttpResponse("Payment verification failed.", status=400)
                
        except Exception as e:
            return HttpResponse(f"Error: {str(e)}", status=500)
class MyPayoutsView(APIView):
    """
    Returns all scheduled payouts for the authenticated user's active circles.
    Auto-populates turns if missing.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        payouts = PayoutSchedule.objects.filter(
            member__user=user,
            status='pending',
            group__is_active=True
        ).select_related('group').order_by('cycle_number')

        if not payouts.exists():
            active_memberships = GroupMembership.objects.filter(
                user=user,
                is_active=True,
                group__is_active=True
            ).select_related('group')

            for membership in active_memberships:
                group = membership.group
                target_members = group.max_members or 5
                interval_days = 1 if group.cycle_frequency == 'daily' else (7 if group.cycle_frequency == 'weekly' else 30)

                assigned_turn = membership.current_cycle_number or 1
                base_date = group.created_at.date() if hasattr(group, 'created_at') and group.created_at else timezone.now().date()
                expected_date = base_date + timezone.timedelta(days=assigned_turn * interval_days)

                pot_gross = group.amount * target_members
                net_pot = pot_gross * Decimal('0.95')

                PayoutSchedule.objects.get_or_create(
                    group=group,
                    member=membership,
                    cycle_number=assigned_turn,
                    defaults={
                        'payout_amount': net_pot,
                        'expected_payout_date': expected_date,
                        'status': 'pending',
                        'investment_percentage': 0,
                    }
                )

            payouts = PayoutSchedule.objects.filter(
                member__user=user,
                status='pending',
                group__is_active=True
            ).select_related('group').order_by('expected_payout_date')

        results = []
        for p in payouts:
            results.append({
                "id": str(p.id),
                "group": {
                    "id": p.group.id,
                    "name": p.group.name,
                    "amount": str(p.group.amount),
                    "cycle_frequency": p.group.cycle_frequency,
                },
                "cycle_number": p.cycle_number,
                "payout_amount": str(p.payout_amount),
                "expected_payout_date": p.expected_payout_date.strftime("%d %b %Y") if hasattr(p.expected_payout_date, 'strftime') else str(p.expected_payout_date),
                "status": p.status,
                "investment_percentage": float(p.investment_percentage),
            })

        return Response(results, status=status.HTTP_200_OK)


MyPayoutsListView = MyPayoutsView


class MyStatsView(APIView):
    """
    Computes real-time Trust Score, Reliability Grade, and Ajo statistics.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        successful_txs = Transaction.objects.filter(
            Q(user=user) | Q(membership__user=user),
            status='successful'
        )

        total_contributed = successful_txs.filter(
            notes__icontains='contribution'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        total_received = successful_txs.filter(
            notes__icontains='payout'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        active_groups_count = GroupMembership.objects.filter(
            user=user,
            is_active=True,
            group__is_active=True
        ).count()

        completed_cycles = PayoutSchedule.objects.filter(
            member__user=user,
            status='paid'
        ).count()

        successful_dues_count = successful_txs.filter(
            Q(notes__icontains='Auto-deduction') | Q(notes__icontains='Paid via internal wallet')
        ).count()

        failed_dues_count = Transaction.objects.filter(
            Q(user=user) | Q(membership__user=user),
            status='failed'
        ).count()

        fine_count = Transaction.objects.filter(
            Q(user=user) | Q(membership__user=user),
            reference__startswith='FINE'
        ).count()

        total_defaults = failed_dues_count + fine_count

        # Score calculation (baseline: 70)
        score = 70 + (successful_dues_count * 5) + (completed_cycles * 10) - (total_defaults * 15)
        score = max(10, min(100, score))

        if score >= 90:
            grade = 'A'
            rating_label = 'Elite Saver'
            max_circle_limit = Decimal('500000.00')
        elif score >= 70:
            grade = 'B'
            rating_label = 'Reliable'
            max_circle_limit = Decimal('100000.00')
        elif score >= 60:
            grade = 'C'
            rating_label = 'Moderate'
            max_circle_limit = Decimal('50000.00')
        elif score >= 45:
            grade = 'D'
            rating_label = 'High Risk'
            max_circle_limit = Decimal('10000.00')
        else:
            grade = 'E'
            rating_label = 'Critical Risk'
            max_circle_limit = Decimal('0.00')

        total_attempts = successful_dues_count + total_defaults
        reliability_rate = (
            round((successful_dues_count / total_attempts) * 100, 1)
            if total_attempts > 0 else 100.0
        )

        return Response({
            "trust_score": score,
            "grade": grade,
            "rating_label": rating_label,
            "max_circle_limit": str(max_circle_limit),
            "reliability_rate": reliability_rate,
            "total_contributed": str(total_contributed),
            "total_received": str(total_received),
            "active_groups_count": active_groups_count,
            "completed_cycles": completed_cycles,
            "on_time_payments": successful_dues_count,
            "default_count": total_defaults,
        }, status=status.HTTP_200_OK)


class ExploreGroupsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ContributionGroupSerializer

    def get_queryset(self):
        return ContributionGroup.objects.filter(is_active=True).order_by('-created_at')


class TriggerAutomatedCycleView(APIView):
    """
    Executes automatic wallet dues deductions, processes past-due payouts, 
    and acts as a keep-alive endpoint for cron-job.org.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        """Lightweight keep-alive ping and automated trigger for cron-job.org GET requests."""
        cron_key = request.headers.get('X-CRON-KEY') or request.GET.get('key')
        expected_key = getattr(settings, 'CRON_SECRET_KEY', getattr(settings, 'SECRET_KEY', None))

        if cron_key and expected_key and cron_key == expected_key:
            results = self._process_cycles_logic()
            return Response({"status": "executed", "results": results}, status=status.HTTP_200_OK)

        return Response({
            "status": "active",
            "message": "ROSCAVault background worker is awake and online."
        }, status=status.HTTP_200_OK)

    def post(self, request):
        """Executes circle deductions and handles background tasks via POST."""
        cron_key = request.headers.get('X-CRON-KEY')
        expected_key = getattr(settings, 'CRON_SECRET_KEY', getattr(settings, 'SECRET_KEY', None))

        if cron_key and expected_key and cron_key != expected_key:
            return Response({"detail": "Unauthorized cron trigger."}, status=status.HTTP_403_FORBIDDEN)

        results = self._process_cycles_logic()
        return Response(results, status=status.HTTP_200_OK)

    @db_transaction.atomic
    def _process_cycles_logic(self):
        """
        1. Auto-deducts dues from member wallets if deadline has passed.
        2. Disburses payouts when cycle dues are complete or payout date is reached.
        """
        now = timezone.now()
        today = now.date()
        deductions_count = 0
        payouts_count = 0
        details_log = []

        active_groups = ContributionGroup.objects.filter(is_active=True)

        for group in active_groups:
            try:
                active_memberships = group.memberships.filter(is_active=True, status='approved')
                
                # Step A: Auto-deduct dues for members who haven't paid for their current cycle
                for membership in active_memberships:
                    has_paid = Transaction.objects.filter(
                        membership=membership,
                        cycle_number=membership.current_cycle_number,
                        status='successful'
                    ).exists()

                    if not has_paid:
                        if membership.next_deadline and membership.next_deadline <= now:
                            user = membership.user
                            try:
                                wallet = Wallet.objects.select_for_update().get(user=user)
                                amount_due = group.amount

                                if wallet.balance >= amount_due:
                                    wallet.balance -= amount_due
                                    wallet.save(update_fields=['balance'])

                                    ref = f"AUTO-AJO-{group.id}-C{membership.current_cycle_number}-U{user.id}-{uuid.uuid4().hex[:6].upper()}"
                                    Transaction.objects.create(
                                        membership=membership,
                                        user=user,
                                        amount=amount_due,
                                        cycle_number=membership.current_cycle_number,
                                        reference=ref,
                                        status='successful',
                                        notes=f"Automatic wallet deduction for Cycle #{membership.current_cycle_number} in {group.name}"
                                    )

                                    try:
                                        from apps.users.models import Notification
                                        Notification.objects.create(
                                            user=user,
                                            title="Automatic Dues Deduction ✓",
                                            message=f"₦{amount_due} was automatically deducted from your wallet for Cycle #{membership.current_cycle_number} in '{group.name}'."
                                        )
                                    except Exception:
                                        pass

                                    deductions_count += 1
                                    details_log.append(f"Auto-deducted ₦{amount_due} from {user.email} for group {group.name}")
                            except Wallet.DoesNotExist:
                                pass

                # Step B: Process Payout Schedule for current or past-due cycles
                pending_schedules = PayoutSchedule.objects.filter(
                    group=group,
                    status='pending',
                    expected_payout_date__lte=today
                ).select_related('member', 'member__user')

                for pending_schedule in pending_schedules:
                    cycle_num = pending_schedule.cycle_number
                    
                    paid_count = Transaction.objects.filter(
                        membership__group=group,
                        cycle_number=cycle_num,
                        status='successful'
                    ).count()

                    # Disburse if all members have paid their dues
                    if paid_count >= active_memberships.count():
                        winner = pending_schedule.member.user
                        winner_wallet = Wallet.objects.select_for_update().get(user=winner)
                        alajo_wallet = Wallet.objects.select_for_update().get(user=group.creator)

                        gross_pot = group.amount * group.max_members
                        alajo_fee = (gross_pot * Decimal('0.02')).quantize(Decimal('0.01'))
                        net_payout = gross_pot - alajo_fee

                        invest_pct = Decimal(str(getattr(pending_schedule, 'investment_percentage', 0))) / Decimal('100')

                        # Handle vault investment vs wallet payout
                        if invest_pct > 0:
                            vault_amount = net_payout * invest_pct
                            wallet_amount = net_payout - vault_amount
                            
                            if wallet_amount > 0:
                                winner_wallet.balance += wallet_amount
                                winner_wallet.save(update_fields=['balance'])

                            try:
                                from apps.vault.models import VaultAccount
                                vault_acc, _ = VaultAccount.objects.get_or_create(user=winner)
                                vault_acc.balance += vault_amount
                                vault_acc.save(update_fields=['balance'])
                            except Exception:
                                winner_wallet.balance += vault_amount
                                winner_wallet.save(update_fields=['balance'])
                        else:
                            winner_wallet.balance += net_payout
                            winner_wallet.save(update_fields=['balance'])

                        pending_schedule.status = 'paid'
                        pending_schedule.payout_amount = net_payout
                        pending_schedule.save(update_fields=['status', 'payout_amount'])

                        Transaction.objects.create(
                            membership=pending_schedule.member,
                            user=winner,
                            amount=net_payout,
                            cycle_number=cycle_num,
                            reference=f"POT-{group.id}-C{cycle_num}-{uuid.uuid4().hex[:6].upper()}",
                            status='successful',
                            notes=f"Net Pot Payout (Cycle #{cycle_num}) - 2% Alajo fee deducted"
                        )

                        alajo_wallet.balance += alajo_fee
                        alajo_wallet.save(update_fields=['balance'])

                        Transaction.objects.create(
                            user=group.creator,
                            amount=alajo_fee,
                            cycle_number=cycle_num,
                            reference=f"ALAJO-FEE-{group.id}-C{cycle_num}-{uuid.uuid4().hex[:6].upper()}",
                            status='successful',
                            notes=f"2% Alajo commission for {group.name} (Cycle #{cycle_num})"
                        )

                        try:
                            from apps.users.models import Notification
                            Notification.objects.create(
                                user=winner,
                                title="Payout Disbursed 🎉",
                                message=f"Your payout of ₦{net_payout} for Cycle #{cycle_num} in '{group.name}' has been processed."
                            )
                        except Exception:
                            pass

                        for m in active_memberships:
                            m.advance_to_next_cycle()

                        if cycle_num >= group.max_members:
                            group.is_active = False
                            group.save(update_fields=['is_active'])

                        payouts_count += 1
                        details_log.append(f"Disbursed pot for Cycle #{cycle_num} in group {group.name}")

            except Exception as e:
                details_log.append(f"Error processing group {group.name}: {str(e)}")

        return {
            "detail": f"Cycle check executed. Deductions: {deductions_count}, Payouts: {payouts_count}.",
            "deductions_count": deductions_count,
            "payouts_count": payouts_count,
            "logs": details_log
        }

class ConfigurePayoutInvestmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, payout_id):
        payout = get_object_or_404(
            PayoutSchedule,
            id=payout_id,
            member__user=request.user
        )

        if payout.status == 'paid':
            return Response(
                {"detail": "Cannot modify investment settings on an already disbursed payout."},
                status=status.HTTP_400_BAD_REQUEST
            )

        percentage = request.data.get('investment_percentage')
        if percentage is None:
            return Response({"detail": "investment_percentage is required."}, status=status.HTTP_400_BAD_REQUEST)

        percentage_dec = Decimal(str(percentage))
        if percentage_dec < 0 or percentage_dec > 100:
            return Response({"detail": "Percentage must be between 0 and 100."}, status=status.HTTP_400_BAD_REQUEST)

        payout.investment_percentage = percentage_dec
        payout.save(update_fields=['investment_percentage'])

        return Response({
            "detail": f"Payout investment allocation updated to {percentage_dec}%.",
            "investment_percentage": str(payout.investment_percentage)
        }, status=status.HTTP_200_OK)
        
class ConfigurePayoutDestinationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, payout_id):
        try:
            payout = get_object_or_404(PayoutSchedule, id=payout_id, user=request.user)
            destination = request.data.get('destination', 'wallet')
            percentage = float(request.data.get('percentage', 100))

            payout.destination = destination
            payout.investment_percentage = percentage
            payout.status = 'configured'
            payout.save()

            return Response(
                {"detail": f"Payout successfully routed {percentage}% to your {destination}."},
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class UserStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        next_payout = (
            PayoutSchedule.objects.filter(
                member__user=user,
                status='pending',
                group__is_active=True,
                expected_payout_date__gte=timezone.now().date()
            )
            .select_related('group')
            .order_by('expected_payout_date')
            .first()
        )

        upcoming_payout_data = None
        if next_payout:
            upcoming_payout_data = {
                "id": str(next_payout.id),
                "circle_name": next_payout.group.name,
                "amount": str(next_payout.payout_amount),
                "expected_date": next_payout.expected_payout_date.strftime("%d %b %Y"),
                "cycle_number": next_payout.cycle_number,
                "investment_percentage": float(next_payout.investment_percentage),
            }

        active_circles_count = GroupMembership.objects.filter(
            user=user,
            is_active=True,
            group__is_active=True
        ).count()

        return Response({
            "trust_score": user.trust_score,
            "grade": user.trust_grade,
            "rating_label": user.trust_rating_label,
            "active_circles_count": active_circles_count,
            "upcoming_payout": upcoming_payout_data,
        }, status=status.HTTP_200_OK)
        
class VerifyPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        }
        
        try:
            paystack_res = requests.get(
                f"https://api.paystack.co/transaction/verify/{reference}",
                headers=headers,
                timeout=10
            ).json()
        except requests.exceptions.RequestException as e:
            return Response({"detail": f"Paystack connection error: {str(e)}"}, status=status.HTTP_502_BAD_GATEWAY)

        if not paystack_res.get('status'):
            return Response({"detail": "Verification failed at gateway."}, status=status.HTTP_400_BAD_REQUEST)

        data = paystack_res.get('data', {})
        gateway_status = data.get('status') # 'success', 'failed', 'abandoned'

        try:
            tx = Transaction.objects.get(reference=reference)
        except Transaction.DoesNotExist:
            return Response({"detail": "Transaction not found."}, status=status.HTTP_404_NOT_FOUND)

        if gateway_status == 'success':
            if tx.status != 'successful':
                tx.status = 'successful'
                tx.save(update_fields=['status'])

                amount_paid = Decimal(str(data.get('amount', 0))) / Decimal('100')
                wallet, _ = Wallet.objects.get_or_create(user=request.user)
                wallet.balance = (wallet.balance or Decimal('0.00')) + amount_paid
                wallet.save(update_fields=['balance'])

            return Response({
                "detail": "Payment verified and wallet funded successfully!",
                "status": "successful",
                "wallet_balance": str(wallet.balance)
            }, status=status.HTTP_200_OK)

        elif gateway_status == 'failed':
            tx.status = 'failed'
            tx.save(update_fields=['status'])
            return Response({"detail": "Payment failed.", "status": "failed"}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"detail": "Payment is still processing.", "status": "pending"}, status=status.HTTP_200_OK)
    
    class GroupApplicantsListView(APIView):
        permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        group = get_object_or_404(ContributionGroup, id=group_id)

        if group.creator != request.user:
            return Response(
                {"detail": "Only the circle creator can review membership applicants."},
                status=status.HTTP_403_FORBIDDEN
            )

        applicants = GroupMembership.objects.filter(
            group=group, 
            status='pending'
        ).select_related('user').order_by('-created_at')

        serializer = MembershipApplicantSerializer(applicants, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ReviewApplicantActionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, membership_id):
        membership = get_object_or_404(GroupMembership, id=membership_id)
        group = membership.group

        if group.creator != request.user:
            return Response(
                {"detail": "You do not have permission to review applicants for this circle."},
                status=status.HTTP_403_FORBIDDEN
            )

        action = request.data.get('action')

        if action == 'approve':
            approved_count = GroupMembership.objects.filter(group=group, status='approved').count()
            if approved_count >= group.max_members:
                return Response(
                    {"detail": f"Circle has reached its capacity limit of {group.max_members} members."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            membership.status = 'approved'
            membership.is_active = True
            membership.save()
            return Response({"detail": "Member approved successfully.", "status": "approved"})

        elif action == 'reject':
            membership.status = 'rejected'
            membership.is_active = False
            membership.save()
            return Response({"detail": "Member request rejected.", "status": "rejected"})

        return Response(
            {"detail": "Invalid action. Must be 'approve' or 'reject'."},
            status=status.HTTP_400_BAD_REQUEST
        )
        
class CreateContributionGroupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            user = request.user
            
            # 1. Fetch Tier & Score Values
            user_tier = getattr(user, 'tier', 0)
            user_kyc_tier = getattr(user, 'kyc_tier', 0)
            trust_score = getattr(user, 'trust_score', 0) or 100
            is_verified = getattr(user, 'is_kyc_verified', False)

            kyc_record_tier = 0
            try:
                profile_obj = UserKYCProfile.objects.filter(user=user).first()
                if profile_obj:
                    kyc_record_tier = int(getattr(profile_obj, 'tier', 0) or 0)
            except Exception:
                pass

            effective_tier = max(
                int(user_tier or 0), 
                int(user_kyc_tier or 0), 
                int(kyc_record_tier or 0)
            )

            if effective_tier < 2:
                return Response(
                    {
                        "detail": f"Creator Eligibility: Circle creators must be in Tier 2 (Verified Residential Address). Your account is currently Tier {effective_tier}.",
                        "requires_tier2": True,
                        "current_tier": effective_tier
                    },
                    status=status.HTTP_403_FORBIDDEN
                )

            if int(trust_score or 0) < 70:
                return Response(
                    {
                        "detail": f"Creator Eligibility: A minimum Trust Score of 70 is required to manage an Ajo circle. Your current score is {trust_score}%.",
                        "requires_trust_score": True,
                        "current_score": trust_score
                    },
                    status=status.HTTP_403_FORBIDDEN
                )

            name = request.data.get('name', '').strip()
            amount = request.data.get('amount')
            max_members = int(request.data.get('max_members', 5))
            cycle_frequency = request.data.get('cycle_frequency', 'monthly')
            join_as_member = request.data.get('join_as_member', False)
            pot_number = int(request.data.get('pot_number', 1))
            
            start_date_str = request.data.get('start_date')
            if start_date_str:
                try:
                    start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                except ValueError:
                    return Response(
                        {"detail": "Invalid start_date format. Use YYYY-MM-DD."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                start_date = timezone.now().date()

            if not name or not amount:
                return Response(
                    {"detail": "Circle name and contribution amount are required."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            with db_transaction.atomic():
                group = ContributionGroup.objects.create(
                    name=name,
                    creator=user,
                    amount=amount,
                    max_members=max_members,
                    cycle_frequency=cycle_frequency,
                    start_date=start_date,
                    is_active=True
                )
                
                # If creator joins as a member, respect their chosen pot_number (slot)
                # If they manage without joining, we assign role admin
                creator_slot = pot_number if join_as_member else 1

                GroupMembership.objects.create(
                    user=user,
                    group=group,
                    role='admin',
                    status='approved',
                    is_active=True,
                    current_cycle_number=creator_slot
                )

            return Response(
                {
                    "detail": "Circle created successfully.",
                    "group_id": group.id,
                    "start_date": str(group.start_date)
                },
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print("SERVER 500 ERROR TRACEBACK:\n", error_trace)
            return Response(
                {"detail": str(e), "traceback": error_trace},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class CreatorGroupManagementDetailsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        try:
            group = get_object_or_404(ContributionGroup, id=group_id)
            
            # Verify creator or admin access
            is_host = group.creator == request.user or GroupMembership.objects.filter(
                group=group, user=request.user, role='admin'
            ).exists()

            if not is_host:
                return Response(
                    {"detail": "Permission denied. Only circle creators can manage this group."},
                    status=status.HTTP_403_FORBIDDEN
                )

            members = GroupMembership.objects.filter(group=group, status='approved')
            pending_applicants = GroupMembership.objects.filter(group=group, status='pending')

            serializer = GroupMembershipSerializer(members, many=True)
            pending_serializer = GroupMembershipSerializer(pending_applicants, many=True)

            data = {
                "group": ContributionGroupSerializer(group).data,
                "members": serializer.data,
                "pending_applicants": pending_serializer.data,
                "pending_applicants_count": pending_applicants.count()
            }
            return Response(data, status=status.HTTP_200_OK)

        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print("--- CREATOR MANAGEMENT 500 ERROR TRACEBACK ---")
            print(error_trace)
            print("---------------------------------------------")
            return Response(
                {"detail": str(e), "traceback": error_trace},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
class CircleApplicantsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id):
        try:
            group = get_object_or_404(ContributionGroup, id=group_id)
            
            # Fetch pending applicants
            pending_applicants = GroupMembership.objects.filter(group=group, status='pending')
            serializer = GroupMembershipSerializer(pending_applicants, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request, group_id):
        """Approve or reject an applicant"""
        try:
            group = get_object_or_404(ContributionGroup, id=group_id)
            membership_id = request.data.get('membership_id') or request.data.get('id')
            action = request.data.get('action', 'approve')

            if not membership_id:
                return Response({"detail": "Membership ID is required."}, status=status.HTTP_400_BAD_REQUEST)

            membership = get_object_or_404(GroupMembership, id=membership_id, group=group)

            if action == 'approve':
                membership.status = 'approved'
                membership.save(update_fields=['status'])
                
                # Notify user
                try:
                    from apps.users.models import Notification
                    Notification.objects.create(
                        user=membership.user,
                        title="Circle Application Approved! 🎉",
                        message=f"Your request to join '{group.name}' has been approved by the host."
                    )
                except Exception:
                    pass

                return Response({"detail": "Applicant approved successfully."}, status=status.HTTP_200_OK)
            elif action == 'reject':
                membership.delete()
                return Response({"detail": "Applicant rejected."}, status=status.HTTP_200_OK)
            
            return Response({"detail": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)