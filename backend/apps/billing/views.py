from decimal import Decimal

from django.db.models import Sum
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwner, IsOwnerOrManager, IsPlatformAdmin
from apps.audit import services as audit
from apps.core.db import set_db_context
from apps.stations.models import Station
from apps.tenants.models import Subscription

from . import services
from .models import Payment, PricingPlan, Reseller
from .providers import InvalidWebhook, get_provider


class WebhookView(APIView):
    """Payment company -> us. Signature verified, then processed exactly once (idempotent on the event id)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider):
        prov = get_provider(provider)
        if prov is None:
            raise NotFound()
        try:
            event = prov.parse_webhook(request.headers, request.body)
        except InvalidWebhook:
            return Response({"detail": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST)  # same answer for every failure
        set_db_context(platform_admin=True)  # the webhook acts as the platform, never as a tenant
        return Response(services.process_event(event, provider))


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = PricingPlan
        fields = ["id", "code", "name", "currency", "setup_fee_per_station", "monthly_fee_per_station", "multi_station_discounts", "addons", "active"]
        read_only_fields = ["id"]

    def validate_multi_station_discounts(self, v):
        if not all(isinstance(t, dict) and {"min_stations", "percent"} <= set(t) and 0 <= float(t["percent"]) <= 100 for t in v):
            raise serializers.ValidationError('Use [{"min_stations": 3, "percent": 10}].')
        return v

    def validate_addons(self, v):
        if not all(isinstance(a, dict) and {"name", "monthly_fee"} <= set(a) for a in v.values()):
            raise serializers.ValidationError('Use {"code": {"name": "...", "monthly_fee": "5.00"}}.')
        return v


class ResellerSerializer(serializers.ModelSerializer):
    total_share = serializers.SerializerMethodField()

    class Meta:
        model = Reseller
        fields = ["id", "name", "contact", "revenue_share_pct", "active", "total_share"]
        read_only_fields = ["id", "total_share"]

    def get_total_share(self, obj):
        return str(Payment.objects.filter(reseller=obj, status="succeeded").aggregate(s=Sum("reseller_share"))["s"] or Decimal(0))

    def validate_revenue_share_pct(self, v):
        if not 0 <= v <= 100:
            raise serializers.ValidationError("Enter a percentage from 0 to 100.")
        return v


class PaymentSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = Payment
        fields = ["id", "tenant", "tenant_name", "provider", "status", "amount", "currency", "months", "paid_through_after", "reseller", "reseller_share", "created_at"]


class PlanListView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response(PlanSerializer(PricingPlan.objects.all().order_by("code"), many=True).data)

    def post(self, request):
        s = PlanSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        plan = s.save()
        audit.log("plan_created", actor=request.user, obj=plan, changes={"code": plan.code}, request=request)
        return Response(PlanSerializer(plan).data, status=status.HTTP_201_CREATED)


class PlanDetailView(APIView):
    permission_classes = [IsPlatformAdmin]

    def patch(self, request, pk):
        plan = PricingPlan.objects.filter(pk=pk).first()
        if not plan:
            raise NotFound()
        s = PlanSerializer(plan, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        audit.log("plan_updated", actor=request.user, obj=plan, changes=request.data, request=request)
        return Response(PlanSerializer(plan).data)


class QuoteView(APIView):
    permission_classes = [IsPlatformAdmin]

    def post(self, request):
        plan = PricingPlan.objects.filter(code=request.data.get("plan")).first()
        if not plan:
            raise ValidationError({"plan": "Unknown plan."})
        try:
            stations, months = int(request.data.get("stations", 1)), int(request.data.get("months", 1))
            if stations < 1 or months < 1:
                raise ValueError
            return Response(services.quote(plan, stations, request.data.get("addons", []), months))
        except (ValueError, TypeError) as exc:
            raise ValidationError({"detail": str(exc) or "stations and months must be positive whole numbers."})


class ResellerListView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response(ResellerSerializer(Reseller.objects.all().order_by("name"), many=True).data)

    def post(self, request):
        s = ResellerSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        r = s.save()
        audit.log("reseller_created", actor=request.user, obj=r, changes={"name": r.name, "share": str(r.revenue_share_pct)}, request=request)
        return Response(ResellerSerializer(r).data, status=status.HTTP_201_CREATED)


class PlatformPaymentListView(ListAPIView):
    permission_classes = [IsPlatformAdmin]
    serializer_class = PaymentSerializer

    def get_queryset(self):
        qs = Payment.objects.select_related("tenant")
        for key in ("tenant", "reseller", "status"):
            if self.request.query_params.get(key):
                qs = qs.filter(**{key if key == "status" else f"{key}_id": self.request.query_params[key]})
        return qs


class BillingStatusView(APIView):
    """What the owner/manager sees: banner, subscription, recent payments. Works while locked out."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        sub = Subscription.objects.filter(tenant_id=request.user.tenant_id).first()
        tenant = request.user.tenant
        return Response({
            "banner": services.banner_for(sub, tenant),
            "subscription": {"status": sub.status, "plan": sub.plan, "paid_through": sub.paid_through} if sub else None,
            "tenant_status": tenant.status,
            "payments": PaymentSerializer(Payment.objects.filter(tenant=tenant)[:12], many=True).data,
        })


class CheckoutView(APIView):
    """'Pay to reactivate': the provider's payment link for what this business owes this month."""

    permission_classes = [IsOwner]

    def post(self, request):
        sub = Subscription.objects.filter(tenant_id=request.user.tenant_id).first()
        plan = PricingPlan.objects.filter(code=sub.plan).first() if sub else None
        if not plan:
            raise ValidationError({"detail": "No plan is set for this business yet. Please contact Tendeka & Elichi."})
        q = services.quote(plan, max(Station.objects.count(), 1), sub.addons, 1)
        from django.conf import settings

        tenant = request.user.tenant
        if tenant.is_demo:  # demo and trial businesses only ever see the payment provider's sandbox
            url = f"{settings.BILLING_SANDBOX_CHECKOUT_URL}?tenant={tenant.pk}&amount={q['monthly_total']}&currency={q['currency']}&return={settings.FRONTEND_BASE_URL}"
        else:
            provider = get_provider(settings.BILLING_DEFAULT_PROVIDER)
            url = provider.create_checkout(tenant=tenant, amount=q["monthly_total"], currency=q["currency"], return_url=settings.FRONTEND_BASE_URL)
        audit.log("checkout_started", actor=request.user, obj=sub, changes={"amount": q["monthly_total"], "sandbox": tenant.is_demo}, request=request)
        return Response({"url": url, "amount": q["monthly_total"], "currency": q["currency"], "sandbox": tenant.is_demo})
