"""Modules: the parts of PumpWise a business can have, each described once (docs/modules.md).

A business is set up from a Mark (Mark 1: the Fuel Zone way, Mark 2: the PESNAT way), then modules are ticked on or
off, borrowing from the other Mark where it fits. The code only ever asks about modules, never about a Mark or a client:

    mods = modules.of(tenant)
    if mods.expenses: ...
    if mods.pump_allocation == "pick": ...

Each module reads the switches already on the Tenant row, so asking costs no database query and a business works
exactly as it did before modules. Saving a change still goes through apps/tenants/features.py, which guards open shifts
and writes the audit log.

To add a module: add a Module to MODULES (and its starting value to each Mark), then write its code to ask
`modules.of(tenant).<key>`. A new way of doing something an existing module does is a new choice on that module.
"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    help: str
    read: object  # tenant -> the module's value, from the Tenant's own columns
    choices: tuple = ()  # ((value, label), ...) for a pick-one module; empty for on/off
    needs: tuple = ()  # on/off modules that must be on for this one to count (off otherwise)
    default: object = None  # a new business with no Mark: the system as it always was

    @property
    def on_off(self):
        return not self.choices


def _pump_allocation(t):
    # "Attendants pick" only counts where the manager would otherwise give out the pumps (services.attendants_pick).
    if t.pump_system == "assigned":
        return "pick" if t.attendants_pick_pumps else "assigned"
    return t.pump_system


MODULES = (
    Module(
        "pump_allocation", "Pump allocation", "Who works which pump.", _pump_allocation,
        choices=(("shared", "Shared pumps, one attendant per shift"), ("shared_multi", "Shared pumps, several attendants"),
                 ("assigned", "Assigned pumps, the manager gives them out"), ("pick", "Assigned pumps, attendants pick theirs when they start")),
        default="shared",
    ),
    Module("expenses", "Expenses", "Attendants and managers record what they paid out; it comes off the cash to hand in.",
           lambda t: t.expenses_enabled, default=False),
    Module("dip_at_shift_end", "Attendants dip at the end of a shift", "The outgoing attendant dips the tanks of their pumps.",
           lambda t: t.dip_at_shift_end, default=False),
    Module("attendants_see_dip_litres", "Attendants see the dip litres",
           "The attendant's phone shows the litres from the tank's chart as the mm is typed (an exception to rule 11).",
           lambda t: t.attendants_see_dip_litres, needs=("dip_at_shift_end",), default=False),
    Module(
        "day_close", "Day close", "When the trading day ends.", lambda t: t.close_day_at_change or "manager",
        choices=(("manager", "The manager closes it on the Dip screen, from 21:00"), ("07:00", "At the 07:00 handover (04:00 to 09:00)"),
                 ("18:00", "At the 18:00 handover (15:00 to 20:00)")),
        default="manager",
    ),
)
BY_KEY = MappingProxyType({m.key: m for m in MODULES})

# Where each Mark starts. Ticking modules on a business starts from one of these.
MARKS = MappingProxyType({
    "mark_1": MappingProxyType({"pump_allocation": "assigned", "expenses": False, "dip_at_shift_end": False,
                                "attendants_see_dip_litres": False, "day_close": "manager"}),
    "mark_2": MappingProxyType({"pump_allocation": "pick", "expenses": True, "dip_at_shift_end": True,
                                "attendants_see_dip_litres": True, "day_close": "18:00"}),
})


class Modules:
    """One business's modules, read once from its Tenant row: `of(tenant).expenses`."""

    __slots__ = ("_values",)

    def __init__(self, values):
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getattr__(self, key):
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(f"No module called {key!r} (apps/tenants/modules.py)") from None

    def __setattr__(self, key, value):
        raise AttributeError("Modules are read-only; change them through apps/tenants/features.py")

    def as_dict(self):
        return dict(self._values)


def resolve(values):
    """Values for every module, with a module whose needs are off reading as off."""
    out = {m.key: values.get(m.key, m.default) for m in MODULES}
    for m in MODULES:
        if m.on_off and out[m.key] and not all(out[n] for n in m.needs):
            out[m.key] = False
    return out


def of(tenant):
    """The business's modules. No database query: the Tenant's own columns."""
    return Modules(resolve({m.key: m.read(tenant) for m in MODULES}))


def handover(mods):
    """The handover the day closes at ("07:00", "18:00"), or "" when the manager closes it from 21:00."""
    return "" if mods.day_close == "manager" else mods.day_close


def closing_at_a_handover(through=""):
    """A database filter for businesses whose day closes at a handover (module "day_close"), for a query over many
    stations at once; `through` is the path to the Tenant, e.g. "tenant__"."""
    from django.db.models import Q

    return ~Q(**{f"{through}close_day_at_change": ""})


def problems(values):
    """What is wrong with a set of module values (unknown modules, choices that do not exist, needs not met); [] if fine."""
    out = [f"No module called {k!r}." for k in values if k not in BY_KEY]
    for m in MODULES:
        if m.key not in values:
            continue
        v = values[m.key]
        if m.on_off and not isinstance(v, bool):
            out.append(f"{m.label}: on or off.")
        elif m.choices and v not in dict(m.choices):
            out.append(f"{m.label}: choose one of {', '.join(dict(m.choices))}.")
        elif m.on_off and v:
            out += [f"{m.label} needs {BY_KEY[n].label}." for n in m.needs if not values.get(n, BY_KEY[n].default)]
    return out
