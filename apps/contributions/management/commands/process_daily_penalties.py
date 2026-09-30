from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from decimal import Decimal
import uuid

# Ensure these match your actual models
from apps.contributions.models import Transaction

class Command(BaseCommand):
    help = 'Runs daily to levy recurring fines on members who have unresolved arrears.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS("--- Running Daily Penalty Sweeper ---"))

        # 1. Find all failed transactions (these represent missed deadlines)
        failed_transactions = Transaction.objects.filter(status='failed')

        if not failed_transactions.exists():
            self.stdout.write("No outstanding defaults found. Exiting.")
            return

        for failed_tx in failed_transactions:
            membership = failed_tx.membership
            group = membership.group
            user = membership.user
            cycle = failed_tx.cycle_number

            # 2. Check if they eventually made a successful payment for this cycle.
            # We match the amount to the group's base amount to ensure we don't 
            # accidentally mistake a past fine payment for clearing the main debt.
            has_cleared_debt = Transaction.objects.filter(
                membership=membership,
                cycle_number=cycle,
                status='successful',
                amount=group.amount
            ).exists()

            if has_cleared_debt:
                # Debt is cleared, skip fining them for this cycle
                continue
            
            # 3. Calculate the dynamic recurring fine from the database rules
            fine_percentage = group.daily_fine_percentage / Decimal('100.00')
            daily_fine_amount = group.amount * fine_percentage

            self.stdout.write(f"⏳ Processing recurring fine for {user.email} (Unpaid Cycle {cycle})...")

            try:
                with db_transaction.atomic():
                    wallet = user.wallet
                    
                    # Deduct the fine directly from their wallet 
                    # (This will safely push their balance into negative numbers to track total debt)
                    wallet.balance -= daily_fine_amount
                    wallet.save()

                    # Record the fine in their transaction history
                    Transaction.objects.create(
                        membership=membership,
                        amount=daily_fine_amount,
                        reference=f"FINE-{uuid.uuid4().hex[:8].upper()}",
                        status='successful', 
                        cycle_number=cycle,
                        notes=f"Recurring daily penalty ({group.daily_fine_percentage}%) for unpaid Cycle {cycle}"
                    )
                
                self.stdout.write(self.style.WARNING(f"⚠️ Levied recurring ₦{daily_fine_amount} fine on {user.email}"))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"❌ Error applying fine for {user.email}: {str(e)}"))

        self.stdout.write(self.style.SUCCESS("--- Daily Penalty Sweeper Complete ---"))