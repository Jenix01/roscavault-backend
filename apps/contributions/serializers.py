from rest_framework import serializers
from .models import ContributionGroup, GroupMembership, Transaction, PayoutSchedule


class ContributionGroupSerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()

    class Meta:
        model = ContributionGroup
        fields = [
            'id',
            'name',
            'description',
            'amount',
            'max_members',
            'cycle_frequency',
            'is_active',
            'members_count',
            'created_at',
        ]

    def get_members_count(self, obj):
        return obj.memberships.count()


class GroupMembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.ReadOnlyField(source='user.email')
    group_name = serializers.ReadOnlyField(source='group.name')
    group = ContributionGroupSerializer(read_only=True)
    
    # Collection turn milestones
    assigned_cycle_number = serializers.SerializerMethodField()
    expected_collection_date = serializers.SerializerMethodField()
    dues_paid_count = serializers.SerializerMethodField()

    class Meta:
        model = GroupMembership
        fields = [
            'id',
            'group',
            'role',
            'current_cycle_number',
            'assigned_cycle_number',
            'expected_collection_date',
            'dues_paid_count',
            'next_deadline',
            'is_active',
            'user_email',
            'group_name',
        ]

    def get_assigned_cycle_number(self, obj):
        payout = PayoutSchedule.objects.filter(member=obj).first()
        if payout:
            return payout.cycle_number
        return obj.current_cycle_number or 1

    def get_expected_collection_date(self, obj):
        payout = PayoutSchedule.objects.filter(member=obj).first()
        if payout and payout.expected_payout_date:
            return payout.expected_payout_date.strftime("%d %b %Y")
        return None

    def get_dues_paid_count(self, obj):
        # Count successful contributions made by this user for this group to advance rounds
        from .models import ContributionTransaction
        return ContributionTransaction.objects.filter(
            user=obj.user,
            group=obj.group,
            status='successful'
        ).count()


class TransactionSerializer(serializers.ModelSerializer):
    user_email = serializers.SerializerMethodField()
    group_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = [
            'id',
            'amount',
            'reference',
            'status',
            'created_at',
            'user_email',
            'group_name',
            'description',
        ]

    def get_user_email(self, obj):
        if obj.membership and obj.membership.user:
            return obj.membership.user.email
        if hasattr(obj, 'user') and obj.user:
            return obj.user.email
        return None

    def get_group_name(self, obj):
        if obj.membership and obj.membership.group:
            return obj.membership.group.name
        return "Wallet Funding"

    def get_description(self, obj):
        if obj.membership and obj.membership.group:
            return f"Contribution - {obj.membership.group.name}"
        return "Direct Wallet Deposit"


class PayoutScheduleSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source='group.name', read_only=True)

    class Meta:
        model = PayoutSchedule
        fields = [
            'id',
            'group',
            'group_name',
            'expected_payout_date',
            'payout_amount',
            'status',
            'cycle_number',
            'investment_percentage',
        ]
        
class MembershipApplicantSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_identifier = serializers.SerializerMethodField()
    trust_score = serializers.SerializerMethodField()

    class Meta:
        model = GroupMembership
        fields = [
            'id', 
            'user', 
            'user_name', 
            'user_identifier', 
            'trust_score', 
            'status', 
            'role', 
            'created_at'
        ]

    def get_user_name(self, obj):
        full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
        return full_name or "Anonymous Saver"

    def get_user_identifier(self, obj):
        return obj.user.phone_number or obj.user.email

    def get_trust_score(self, obj):
        # Read from obj.user.trust_score or fallback default
        return getattr(obj.user, 'trust_score', 95)