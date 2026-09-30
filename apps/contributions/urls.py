from django.urls import path
from .views import (
    ConfigurePayoutInvestmentView,
    CreateContributionGroupView,
    CreatorGroupManagementDetailsView,
    ContributionGroupListCreateView, 
    JoinContributionGroupView, 
    CreateTransactionView,
    MyTransactionsView,
    ProcessPayoutView,
    PaymentWebhookView,
    InitializePaymentView,
    TriggerAutomatedCycleView,
    UserStatsView,
    VerifyPaymentView,
    WalletContributionView,
    UserMembershipsAPIView,
    MyStatsView,
    PaymentCallbackRedirectView,
    ExploreGroupsView,
    MyPayoutsView
)

app_name = 'contributions'

urlpatterns = [
    # Gated Circle Creation View (Strict Tier 2 & Trust Score Enforcement)
    path('create/', CreateContributionGroupView.as_view(), name='create-circle'),
    
    # Creator Management Details
    path('groups/<int:group_id>/details/', CreatorGroupManagementDetailsView.as_view(), name='creator-group-details'),
    
    # Group Listing & Memberships
    path('groups/', ContributionGroupListCreateView.as_view(), name='groups-list-create'),
    path('my-groups/', UserMembershipsAPIView.as_view(), name='my-groups'),
    path('my-transactions/', MyTransactionsView.as_view(), name='my-transactions'),
    path('my-payouts/', MyPayoutsView.as_view(), name='my-payouts'),
    path('my-stats/', MyStatsView.as_view(), name='my-stats'),
    path('explore/', ExploreGroupsView.as_view(), name='explore-groups'),
    path('explore-groups/', ExploreGroupsView.as_view(), name='explore-groups-alias'),
    path('payment/callback/', PaymentCallbackRedirectView.as_view(), name='payment-callback-redirect'),
    
    # Group Actions (Direct and Prefixed Routes)
    path('<uuid:group_id>/join/', JoinContributionGroupView.as_view(), name='join-group-direct'),
    path('groups/<uuid:group_id>/join/', JoinContributionGroupView.as_view(), name='join-group'),
    path('<int:group_id>/join/', JoinContributionGroupView.as_view(), name='join-group-direct-int'),
    path('groups/<int:group_id>/join/', JoinContributionGroupView.as_view(), name='join-group-int'),

    path('<uuid:group_id>/pay-from-wallet/', WalletContributionView.as_view(), name='wallet-contribution-direct'),
    path('groups/<uuid:group_id>/pay-from-wallet/', WalletContributionView.as_view(), name='wallet-contribution'),
    path('<uuid:group_id>/contribute/', CreateTransactionView.as_view(), name='contribute-to-group-direct'),
    path('groups/<uuid:group_id>/contribute/', CreateTransactionView.as_view(), name='contribute-to-group'),

    # Background Tasks / Cron
    path('cron/process-cycles/', TriggerAutomatedCycleView.as_view(), name='process-cycles-cron'),

    # Payment Gateway Handlers
    path('payment/initialize/', InitializePaymentView.as_view(), name='payment-initialize'),
    path('payment/verify/<str:reference>/', VerifyPaymentView.as_view(), name='verify-payment'),
    path('payment/webhook/', PaymentWebhookView.as_view(), name='payment-webhook'),

    # Payouts & Investments
    path('payouts/<int:payout_id>/configure-investment/', ConfigurePayoutInvestmentView.as_view(), name='configure-payout-investment'),
    path('payouts/<uuid:payout_id>/process/', ProcessPayoutView.as_view(), name='process-payout'),
]