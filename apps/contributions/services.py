import uuid
from decimal import Decimal
from django.utils import timezone
from django.db import transaction as db_transaction
from django.contrib.auth import get_user_model

from .models import ContributionGroup, GroupMembership, Transaction, PayoutSchedule
from users.models import Wallet

User = get_user_model()


def process_due_circle_deductions():
    """
    Automated engine that:
    1. Finds memberships with deadlines due (next_deadline <= now).
    2. Auto-debits member wallet balance.
    3. If insufficient funds, logs defaulter penalty using group.daily_fine_percentage.
    4. When all round dues are settled, auto-disburses the net pot (95%) and Alajo fee (5%).
    """
    now = timezone.now()
    
    # 1. Fetch active memberships with past or current deadlines
    due_memberships = GroupMembership.objects.filter(
        is_active=True,
        group__is_active=True,
        next_deadline__lte=now
    ).select_related('group', 'user')

    processed_count = 0
    defaulter_count = 0
    payout_triggered_count = 0

    for membership in due_memberships:
        group = membership.group
        current_cycle = membership.current_cycle_number

        # Skip if dues for current cycle have already been successfully processed
        already_paid = Transaction.objects.filter(
            membership=membership,
            cycle_number=current_cycle,
            status='successful'
        ).exists()

        if already_paid:
            continue

        with db_transaction.atomic():
            try:
                wallet = Wallet.objects.select_for_update().get(user=membership.user)
            except Wallet.DoesNotExist:
                continue

            amount_due = group.amount

            # 2. Check balance: deduct if funded, penalize if insufficient
            if wallet.balance >= amount_due:
                wallet.balance -= amount_due
                wallet.save(update_fields=['balance'])

                ref = f"AUTO-DUE-{group.id}-C{current_cycle}-M{membership.id}-{uuid.uuid4().hex[:6].upper()}"
                Transaction.objects.create(
                    membership=membership,
                    user=membership.user,
                    amount=amount_due,
                    cycle_number=current_cycle,
                    reference=ref,
                    status='successful',
                    notes=f"Automated due-date deduction for {group.name} (Cycle #{current_cycle})"
                )
                processed_count += 1
            else:
                # Member has defaulted: record fine transaction based on group's daily_fine_percentage
                fine_rate = (group.daily_fine_percentage or Decimal('2.00')) / Decimal('100.00')
                fine_amount = (amount_due * fine_rate).quantize(Decimal('0.01'))

                Transaction.objects.create(
                    membership=membership,
                    user=membership.user,
                    amount=fine_amount,
                    cycle_number=current_cycle,
                    reference=f"FINE-{group.id}-C{current_cycle}-M{membership.id}-{uuid.uuid4().hex[:6].upper()}",
                    status='pending',
                    notes=f"Defaulter fine levied ({group.daily_fine_percentage}% daily fine)"
                )
                defaulter_count += 1
                continue  # Skip round evaluation since not all members paid yet

            # 3. Check if all circle members have settled this round
            paid_count = Transaction.objects.filter(
                membership__group=group,
                cycle_number=current_cycle,
                status='successful'
            ).count()

            if paid_count >= group.max_members:
                # 4. Find scheduled recipient for this cycle
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
                    alajo_fee = (gross_pot * Decimal('0.05')).quantize(Decimal('0.01'))
                    net_payout = gross_pot - alajo_fee

                    # Disburse 95% net pot to winner
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
                        reference=f"AUTO-POT-{group.id}-C{current_cycle}-{uuid.uuid4().hex[:6].upper()}",
                        status='successful',
                        notes=f"Automated Ajo Net Pot Disbursed (Cycle #{current_cycle})"
                    )

                    # Disburse 5% management fee to Alajo
                    alajo_wallet.balance += alajo_fee
                    alajo_wallet.save(update_fields=['balance'])

                    Transaction.objects.create(
                        user=group.creator,
                        amount=alajo_fee,
                        cycle_number=current_cycle,
                        reference=f"AUTO-ALAJO-{group.id}-C{current_cycle}-{uuid.uuid4().hex[:6].upper()}",
                        status='successful',
                        notes=f"Automated 5% Alajo Management Fee for {group.name}"
                    )

                    payout_triggered_count += 1

                # 5. Advance all circle memberships to next round
                for m in group.memberships.filter(is_active=True):
                    m.advance_to_next_cycle()

                if current_cycle >= group.max_members:
                    group.is_active = False
                    group.save(update_fields=['is_active'])

    return {
        "processed_deductions": processed_count,
        "defaulters_flagged": defaulter_count,
        "payouts_completed": payout_triggered_count,
    }