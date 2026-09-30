from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone
import uuid
from decimal import Decimal

from apps.contributions.models import GroupMembership, Transaction


class Command(BaseCommand):
    help = "Sweeps wallets based on the specific deadline time set by the Alajo."

    def handle(self, *args, **kwargs):
        now = timezone.now()
        self.stdout.write(self.style.SUCCESS(f"--- Running Deadline Check at {now.strftime('%Y-%m-%d %H:%M')} ---"))

        # Find memberships whose deadline has arrived and are marked active
        memberships_due = GroupMembership.objects.filter(
            next_deadline__lte=now,
            is_active=True
        ).select_related('group', 'user')

        if not memberships_due.exists():
            self.stdout.write("No deadlines have passed right now. Exiting.")
            return

        for membership in memberships_due:
            user = membership.user
            group = membership.group
            wallet = user.wallet
            amount_due = group.amount
            current_cycle = membership.current_cycle_number

            has_paid = Transaction.objects.filter(
                membership=membership,
                cycle_number=current_cycle,
                status='successful'
            ).exists()

            if has_paid:
                self.stdout.write(self.style.SUCCESS(f"⏭️ Skipped {user.email}: Already paid manually."))
                membership.advance_to_next_cycle()
                continue

            self.stdout.write(f"⏳ Executing auto-deduction for {user.email} (Cycle {current_cycle})...")

            if wallet.balance >= amount_due:
                try:
                    with db_transaction.atomic():
                        # 1. Deduct dues from member wallet
                        wallet.balance -= Decimal(amount_due)
                        wallet.save(update_fields=['balance'])

                        # 2. Record transaction with explicit user mapping
                        Transaction.objects.create(
                            membership=membership,
                            user=user,  # Required for HistoryScreen queries
                            amount=amount_due,
                            reference=f"AUTO-DUE-{group.id}-C{current_cycle}-{uuid.uuid4().hex[:6].upper()}",
                            status='successful',
                            cycle_number=current_cycle,
                            notes=f"Auto-deduction for {group.name} (Round #{current_cycle})"
                        )

                        # 3. Advance to the next cycle and update deadline
                        membership.advance_to_next_cycle()

                    self.stdout.write(self.style.SUCCESS(f"✅ Success: Auto-deducted ₦{amount_due} from {user.email}."))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"❌ Error for {user.email}: {str(e)}"))
            else:
                self.stdout.write(self.style.WARNING(f"⚠️ Failed: {user.email} has insufficient funds!"))

                # Record failed attempt with explicit user mapping
                Transaction.objects.create(
                    membership=membership,
                    user=user,  # Required for HistoryScreen queries
                    amount=amount_due,
                    reference=f"FAIL-DUE-{group.id}-C{current_cycle}-{uuid.uuid4().hex[:6].upper()}",
                    status='failed',
                    cycle_number=current_cycle,
                    notes=f"Failed auto-deduction: Insufficient funds for {group.name} (Round #{current_cycle})"
                )

                # Advance cycle so schedule progression continues
                membership.advance_to_next_cycle()

        self.stdout.write(self.style.SUCCESS("--- Deadline Processing Complete ---"))