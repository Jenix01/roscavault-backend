from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone
from django.db.models import Sum
from apps.contributions.models import PayoutSchedule, Transaction, GroupMembership
import uuid
from decimal import Decimal

# Try importing InvestmentVault if created; fallback safely if not migrated yet
try:
    from apps.contributions.models import InvestmentVault
except ImportError:
    InvestmentVault = None


class Command(BaseCommand):
    help = 'Distributes the collected Ajo pot (95% net: liquid/investment split, 5% Alajo fee) and penalizes defaulters.'

    def handle(self, *args, **kwargs):
        now = timezone.now().date()
        self.stdout.write(self.style.SUCCESS(f"--- Running Payout Engine for {now} ---"))

        pending_payouts = PayoutSchedule.objects.filter(
            expected_payout_date__lte=now,
            status='pending'
        ).select_related('group', 'group__creator', 'member', 'member__user')

        if not pending_payouts.exists():
            self.stdout.write("No pending payouts due at this time. Exiting.")
            return

        for payout in pending_payouts:
            group = payout.group
            member = payout.member
            user = member.user
            wallet = user.wallet
            alajo = group.creator
            alajo_wallet = alajo.wallet
            cycle = payout.cycle_number

            # 1. Calculate ACTUAL amount collected for this cycle
            successful_txs = Transaction.objects.filter(
                membership__group=group,
                cycle_number=cycle,
                status='successful'
            )

            actual_collected_dict = successful_txs.aggregate(total=Sum('amount'))
            actual_collected = actual_collected_dict['total'] or Decimal('0.00')

            # 2. Identify Defaulters
            all_members = GroupMembership.objects.filter(group=group, is_active=True)
            paid_member_ids = successful_txs.values_list('membership_id', flat=True)
            defaulters = all_members.exclude(id__in=paid_member_ids)

            self.stdout.write(
                f"💸 Processing payout for {user.email} (Cycle {cycle}). "
                f"Expected: ₦{payout.payout_amount}, Collected: ₦{actual_collected}"
            )

            try:
                with db_transaction.atomic():
                    # --- EXECUTE 95% POT & 5% ALAJO SPLIT ---
                    if actual_collected > 0:
                        # 1. 5% Management Commission to Alajo
                        alajo_fee = (actual_collected * Decimal('0.05')).quantize(Decimal('0.01'))
                        net_winner_payout = actual_collected - alajo_fee

                        # 2. Investment vs Liquid Wallet Split
                        invest_percent = getattr(payout, 'investment_percentage', Decimal('0.00')) or Decimal('0.00')
                        invest_fraction = (invest_percent / Decimal('100.00')).quantize(Decimal('0.0001'))
                        amount_to_invest = (net_winner_payout * invest_fraction).quantize(Decimal('0.01'))
                        liquid_wallet_payout = net_winner_payout - amount_to_invest

                        # A. Credit Liquid Portion to User Wallet (if any)
                        if liquid_wallet_payout > Decimal('0.00'):
                            wallet.balance += liquid_wallet_payout
                            wallet.save(update_fields=['balance'])

                            Transaction.objects.create(
                                membership=member,
                                user=user,
                                amount=liquid_wallet_payout,
                                cycle_number=cycle,
                                reference=f"PAYOUT-LIQ-{uuid.uuid4().hex[:8].upper()}",
                                status='successful',
                                notes=f"Liquid Ajo payout for Cycle {cycle} ({100 - invest_percent}% of net pot)"
                            )

                        # B. Credit Investment Vault Portion (if elected)
                        if amount_to_invest > Decimal('0.00'):
                            if InvestmentVault:
                                InvestmentVault.objects.create(
                                    user=user,
                                    payout_source=payout,
                                    principal_amount=amount_to_invest,
                                    interest_rate_annual=Decimal('12.00'),
                                    lock_duration_days=90,
                                    is_matured=False
                                )

                            Transaction.objects.create(
                                membership=member,
                                user=user,
                                amount=amount_to_invest,
                                cycle_number=cycle,
                                reference=f"INVEST-{uuid.uuid4().hex[:8].upper()}",
                                status='successful',
                                notes=f"Reinvested {invest_percent}% of Cycle {cycle} payout in 12% APY Vault"
                            )

                            if hasattr(payout, 'invested_amount'):
                                payout.invested_amount = amount_to_invest

                        # C. Credit 5% Fee to Alajo
                        alajo_wallet.balance += alajo_fee
                        alajo_wallet.save(update_fields=['balance'])

                        Transaction.objects.create(
                            user=alajo,
                            amount=alajo_fee,
                            cycle_number=cycle,
                            reference=f"ALAJO-FEE-{uuid.uuid4().hex[:8].upper()}",
                            status='successful',
                            notes=f"5% Alajo commission for {group.name} (Cycle {cycle})"
                        )

                        self.stdout.write(
                            self.style.SUCCESS(
                                f"✅ Credited {user.email}: ₦{liquid_wallet_payout} liquid | "
                                f"₦{amount_to_invest} invested ({invest_percent}%) | "
                                f"₦{alajo_fee} commission to Alajo ({alajo.email})"
                            )
                        )

                    # Mark the schedule as paid
                    payout.status = 'paid'
                    payout.payout_amount = actual_collected
                    save_fields = ['status', 'payout_amount']
                    if hasattr(payout, 'invested_amount'):
                        save_fields.append('invested_amount')
                    payout.save(update_fields=save_fields)

                    # Advance active members to their next cycle deadline
                    for active_member in all_members:
                        active_member.advance_to_next_cycle()

                    # Complete circle if final cycle reached
                    if cycle >= group.max_members:
                        group.is_active = False
                        group.save(update_fields=['is_active'])

                    # --- LEVY FINES ON DEFAULTERS ---
                    fine_percentage = group.daily_fine_percentage / Decimal('100.00')
                    daily_fine_amount = (group.amount * fine_percentage).quantize(Decimal('0.01'))

                    for defaulter in defaulters:
                        defaulter_wallet = defaulter.user.wallet
                        defaulter_wallet.balance -= daily_fine_amount
                        defaulter_wallet.save(update_fields=['balance'])

                        Transaction.objects.create(
                            membership=defaulter,
                            user=defaulter.user,
                            amount=daily_fine_amount,
                            cycle_number=cycle,
                            reference=f"FINE-{uuid.uuid4().hex[:8].upper()}",
                            status='successful',
                            notes=f"Day 1 late fine ({group.daily_fine_percentage}%) for Cycle {cycle}"
                        )
                        self.stdout.write(
                            self.style.WARNING(
                                f"⚠️ Levied ₦{daily_fine_amount} fine on defaulter: {defaulter.user.email}"
                            )
                        )

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"❌ Critical Error paying {user.email}: {str(e)}"))

        self.stdout.write(self.style.SUCCESS("--- Payout Processing Complete ---"))