"""Marks (docs/modules.md): named sets of module values a new business starts from. Platform owner only.

* Editing a Mark changes only the Mark: a business keeps its own modules, so Fuel Zone is never changed by an edit to
  Mark 1. A new Mark is made by duplicating one and changing its modules; the original stays as it was.
* A business's link to its Mark is a label and a starting point. Where its modules differ from the Mark's, the
  platform shows it (differences()).
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.audit import services as audit

from . import modules
from .models import Mark


def _check(values):
    """A complete, valid set of module values, or a ValidationError naming what is wrong."""
    wrong = modules.problems(values) + [f"{m.label}: choose a value." for m in modules.MODULES if m.key not in values]
    if wrong:
        raise ValidationError({"modules": wrong})
    return {m.key: values[m.key] for m in modules.MODULES}


def _clean_name(name, *, exclude=None):
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": "Give the Mark a name."})
    if Mark.objects.filter(name__iexact=name).exclude(pk=exclude).exists():
        raise ValidationError({"name": f"There is already a Mark called {name}."})
    return name


def ensure_starting_marks():
    """Mark 1 and Mark 2 as the code describes them, if missing (the database has them from migration 0019)."""
    for key, name in (("mark_1", "Mark 1"), ("mark_2", "Mark 2")):
        Mark.objects.get_or_create(name=name, defaults={"modules": dict(modules.MARKS[key])})


def create_mark(*, name, values, description="", based_on=None, actor=None, request=None):
    with transaction.atomic():
        mark = Mark.objects.create(name=_clean_name(name), description=description.strip(), modules=_check(values), based_on=based_on)
        audit.log("mark_created", actor=actor, obj=mark, request=request,
                  changes={"name": mark.name, "based_on": based_on.name if based_on else None, "modules": mark.modules})
    return mark


def duplicate(mark, *, name, actor=None, request=None):
    """A new Mark with the same modules, to change into a new way of working. The original is untouched."""
    return create_mark(name=name, values=mark.values(), description=mark.description, based_on=mark, actor=actor, request=request)


def update_mark(mark, *, name=None, description=None, values=None, actor=None, request=None):
    """Change a Mark's name, description or modules. Businesses on it keep their own modules."""
    before = {"name": mark.name, "description": mark.description, "modules": mark.values()}
    after = dict(before)
    if name is not None:
        after["name"] = _clean_name(name, exclude=mark.pk)
    if description is not None:
        after["description"] = description.strip()
    if values is not None:
        after["modules"] = _check({**before["modules"], **values})
    changes = audit.diff(before, after)
    if not changes:
        return mark
    with transaction.atomic():
        mark.name, mark.description, mark.modules = after["name"], after["description"], after["modules"]
        mark.save(update_fields=["name", "description", "modules", "updated_at"])
        audit.log("mark_updated", actor=actor, obj=mark, request=request, changes=changes)
    return mark


def set_tenant_mark(*, tenant, mark, actor=None, request=None):
    """Link a business to a Mark (its label). Its modules stay as they are."""
    if tenant.mark_id == (mark.pk if mark else None):
        return
    with transaction.atomic():
        before = tenant.mark.name if tenant.mark_id else None
        tenant.mark = mark
        tenant.save(update_fields=["mark", "updated_at"])
        audit.log("tenant_mark_set", actor=actor, tenant_id=tenant.id, obj=tenant, request=request,
                  changes={"mark": [before, mark.name if mark else None]})


def differences(tenant):
    """{module key: [the Mark's value, the business's value]} where the business differs from its Mark."""
    if not tenant.mark_id:
        return {}
    have = modules.of(tenant).as_dict()
    return {k: [v, have[k]] for k, v in tenant.mark.values().items() if have[k] != v}
