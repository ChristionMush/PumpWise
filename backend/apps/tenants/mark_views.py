"""Platform endpoints for modules and Marks (docs/modules.md). Platform owner only."""
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsPlatformAdmin

from . import marks, modules
from .models import Mark, Tenant


def _module(m):
    return {"key": m.key, "label": m.label, "help": m.help, "kind": "on_off" if m.on_off else "choice",
            "choices": [{"value": v, "label": label} for v, label in m.choices], "needs": list(m.needs), "default": m.default}


def _mark(mark, tenants=None):
    out = {"id": str(mark.pk), "name": mark.name, "description": mark.description, "modules": mark.values(),
           "based_on": {"id": str(mark.based_on_id), "name": mark.based_on.name} if mark.based_on_id else None, "created_at": mark.created_at}
    if tenants is not None:
        out["tenants"] = tenants
    return out


def _get_mark(pk):
    try:
        mark = Mark.objects.select_related("based_on").filter(pk=pk).first()
    except Exception:  # malformed UUID
        mark = None
    if mark is None:
        raise NotFound("Mark not found.")
    return mark


class MarkInput(serializers.Serializer):
    name = serializers.CharField(max_length=80, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    modules = serializers.DictField(required=False)
    duplicate_of = serializers.UUIDField(required=False)


class PlatformModulesView(APIView):
    """Every module, as the platform screens list them to tick."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response({"modules": [_module(m) for m in modules.MODULES]})


class PlatformMarkListView(APIView):
    """The Marks, each with the businesses on it. POST: a new Mark, or a copy of one (duplicate_of) to change."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        on = {}
        for t in Tenant.objects.exclude(mark=None).order_by("name").values("mark_id", "id", "name"):
            on.setdefault(t["mark_id"], []).append({"id": str(t["id"]), "name": t["name"]})
        return Response({"marks": [_mark(m, on.get(m.pk, [])) for m in Mark.objects.select_related("based_on")]})

    def post(self, request):
        data = MarkInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        if "duplicate_of" in d:
            mark = marks.duplicate(_get_mark(d["duplicate_of"]), name=d.get("name", ""), actor=request.user, request=request)
        else:
            mark = marks.create_mark(name=d.get("name", ""), values=d.get("modules", {}), description=d.get("description", ""),
                                     actor=request.user, request=request)
        return Response(_mark(mark, []), status=status.HTTP_201_CREATED)


class PlatformMarkDetailView(APIView):
    """One Mark. PATCH changes its name, description or modules; businesses on it keep their own modules."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        mark = _get_mark(pk)
        return Response(_mark(mark, [{"id": str(t.pk), "name": t.name} for t in Tenant.objects.filter(mark=mark).order_by("name")]))

    def patch(self, request, pk):
        data = MarkInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        mark = marks.update_mark(_get_mark(pk), name=d.get("name"), description=d.get("description"), values=d.get("modules"),
                                 actor=request.user, request=request)
        return Response(_mark(mark))


class PlatformTenantMarkView(APIView):
    """Link a business to a Mark ({"mark": id} or {"mark": null}). Its modules stay as they are."""

    permission_classes = [IsPlatformAdmin]

    def patch(self, request, pk):
        from .views import _get_tenant, summarize

        tenant = _get_tenant(pk)
        mark_id = request.data.get("mark")
        marks.set_tenant_mark(tenant=tenant, mark=_get_mark(mark_id) if mark_id else None, actor=request.user, request=request)
        return Response(summarize(_get_tenant(pk), with_stations=True))


def mark_summary(tenant):
    """The business's Mark, modules, and where they differ from the Mark (for the platform's business screens)."""
    return {"mark": {"id": str(tenant.mark_id), "name": tenant.mark.name} if tenant.mark_id else None,
            "modules": modules.of(tenant).as_dict(), "mark_differences": marks.differences(tenant)}

