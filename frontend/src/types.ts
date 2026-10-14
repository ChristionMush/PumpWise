export type Role = "platform_admin" | "owner" | "manager" | "attendant";
export interface User { id: string; role: Role; full_name: string; email: string | null; phone: string | null; must_change_password: boolean; tenant_id: string | null; }
export interface TenantInfo { id: string; name: string; slug: string; status: "active" | "suspended" | "cancelled"; is_demo?: boolean; trial_expires_at?: string | null; }
export interface Subscription { status: "active" | "past_due" | "suspended" | "cancelled"; plan: string; paid_through: string | null; }
export interface Owner { id: string; full_name: string; email: string | null; phone: string | null; }
export type AttendantsMode = "single" | "multiple";
export type AssignmentMode = "shared" | "assigned";
export interface Station { id: string; name: string; address: string; status: string; attendants_mode?: AttendantsMode; assignment_mode?: AssignmentMode; }
export interface Features { multi_attendant_enabled: boolean; pump_assignment_enabled: boolean; }
/** Chosen by the platform when it creates a business: shared pumps, or each attendant works their own assigned pumps. */
export type PumpSystem = "shared" | "assigned";
export interface TenantSummary {
  id: string; name: string; slug: string; status: TenantInfo["status"]; created_at: string;
  subscription: Subscription | null; station_count: number; owner: Owner | null; stations?: Station[];
  features?: Features; pump_system?: PumpSystem; is_demo?: boolean; trial_expires_at?: string | null;
}
export interface Credentials { login: string; temporary_password: string; }
export interface Page<T> { count: number; next: string | null; results: T[]; }
