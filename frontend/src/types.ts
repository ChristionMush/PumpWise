export type Role = "platform_admin" | "owner" | "manager" | "attendant";
export interface User { id: string; role: Role; full_name: string; email: string | null; phone: string | null; must_change_password: boolean; tenant_id: string | null; }
export interface TenantInfo { id: string; name: string; slug: string; status: "active" | "suspended" | "cancelled"; is_demo?: boolean; trial_expires_at?: string | null;
  expenses_enabled?: boolean; // the server still sends it for phones on the version before modules; read useModule("expenses")
  modules?: Modules;
}
/** The business's modules (backend apps/tenants/modules.py). Screens ask useModule("expenses"), never a client or a Mark. */
export interface Modules {
  pump_allocation: "shared" | "shared_multi" | "assigned" | "pick";
  expenses: boolean;
  dip_at_shift_end: boolean;
  attendants_see_dip_litres: boolean;
  day_close: "manager" | "07:00" | "18:00";
}
export interface Subscription { status: "active" | "past_due" | "suspended" | "cancelled"; plan: string; paid_through: string | null; }
export interface Owner { id: string; full_name: string; email: string | null; phone: string | null; }
export type AttendantsMode = "single" | "multiple";
export type AssignmentMode = "shared" | "assigned";
export interface Station { id: string; name: string; address: string; status: string; attendants_mode?: AttendantsMode; assignment_mode?: AssignmentMode; }
export interface Features { multi_attendant_enabled: boolean; pump_assignment_enabled: boolean; expenses_enabled?: boolean;
  attendants_pick_pumps?: boolean; dip_at_shift_end?: boolean; attendants_see_dip_litres?: boolean; close_day_at_change?: "" | "07:00" | "18:00"; }
/** Chosen by the platform when it creates a business: shared pumps with one or several attendants, or each attendant works their own assigned pumps. */
export type PumpSystem = "shared" | "shared_multi" | "assigned";
export interface TenantSummary {
  id: string; name: string; slug: string; status: TenantInfo["status"]; created_at: string;
  subscription: Subscription | null; station_count: number; owner: Owner | null; stations?: Station[];
  features?: Features; pump_system?: PumpSystem; is_demo?: boolean; trial_expires_at?: string | null;
  mark?: { id: string; name: string } | null; modules?: Modules; mark_differences?: Partial<Record<keyof Modules, [unknown, unknown]>>;
}
/** One module as the platform lists it to tick (GET /api/platform/modules/). */
export interface ModuleInfo {
  key: keyof Modules; label: string; help: string; kind: "on_off" | "choice";
  choices: { value: string; label: string }[]; needs: (keyof Modules)[]; default: string | boolean;
}
/** A Mark: a named set of modules a new business starts from (GET /api/platform/marks/). */
export interface MarkInfo {
  id: string; name: string; description: string; modules: Modules; based_on: { id: string; name: string } | null;
  tenants?: { id: string; name: string }[];
}
export interface Credentials { login: string; temporary_password: string; }
export interface Page<T> { count: number; next: string | null; results: T[]; }
